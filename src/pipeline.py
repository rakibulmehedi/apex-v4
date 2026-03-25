"""Pipeline orchestrator — wires all modules into a single trading loop.

Phase 5 (P5.4).
Flow:
  MarketSnapshot → FeatureVector → Regime → AlphaHypothesis
  → CalibratedTradeIntent → RiskDecision → Execution → FillRecord
  → TradeOutcome → SegmentUpdate

Live mode: MarketFeed publishes over ZMQ PUSH; this module pulls via ZMQ PULL.
Simulation: callers invoke ``process_tick()`` directly (no ZMQ).

Architecture ref: APEX_V4_STRATEGY.md Section 5
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml
import zmq
import zmq.asyncio

from src.alpha.mean_reversion import MeanReversionEngine
from src.alpha.momentum import MomentumEngine
from src.calibration.engine import CalibrationEngine
from src.calibration.history import PerformanceDatabase
from src.execution.fill_tracker import FillTracker
from src.execution.gateway import ExecutionGateway
from src.features.fabric import FeatureFabric
from src.features.state import PostgresWriter, RedisStateManager
from src.learning.recorder import TradeOutcomeRecorder
from src.learning.updater import KellyInputUpdater
from src.market.feed import MarketFeed
from src.market.mt5_client import MT5Client
from src.market.mt5_factory import get_mt5_client
from src.market.schemas import Decision, MarketSnapshot, Regime
from src.observability.metrics import start_metrics_server
from src.regime.classifier import RegimeClassifier
from src.risk.covariance import EWMACovarianceMatrix
from src.risk.governor import RiskGovernor
from src.risk.kill_switch import KillSwitch
from src.risk.reconciler import StateReconciler

logger = structlog.get_logger(__name__)

_ZMQ_ADDR = "ipc:///tmp/apex_market.ipc"
_ZMQ_POLL_TIMEOUT_MS = 1000


# ── PipelineContext ──────────────────────────────────────────────────

@dataclass
class PipelineContext:
    """Dependency-injection container holding all initialised components."""

    mt5: MT5Client
    feed: MarketFeed
    fabric: FeatureFabric
    state: RedisStateManager
    pg_writer: PostgresWriter
    classifier: RegimeClassifier
    momentum: MomentumEngine
    mr: MeanReversionEngine
    cal_engine: CalibrationEngine
    perf_db: PerformanceDatabase
    governor: RiskGovernor
    kill_switch: KillSwitch
    covariance: EWMACovarianceMatrix
    reconciler: StateReconciler
    gateway: ExecutionGateway
    fill_tracker: FillTracker
    recorder: TradeOutcomeRecorder
    updater: KellyInputUpdater
    settings: dict[str, Any]
    paper_positions: dict[int, dict[str, Any]] = field(default_factory=dict)


# ── Settings ─────────────────────────────────────────────────────────

def load_settings(path: str | Path = "config/settings.yaml") -> dict[str, Any]:
    """Parse the runtime settings file."""
    with open(path) as fh:
        return yaml.safe_load(fh)


# ── Context Initialisation ───────────────────────────────────────────

def init_context(
    settings: dict[str, Any],
    *,
    session_factory: Any = None,
    redis_client: Any = None,
) -> PipelineContext:
    """Build every component once.  DI overrides for tests."""
    mt5 = get_mt5_client(settings.get("mt5", {}).get("mode"))
    mt5.initialize()

    pairs = settings.get("mt5", {}).get("pairs", ["EURUSD"])
    regime_cfg = settings.get("regime", {})
    risk_cfg = settings.get("risk", {})
    alpha_cfg = settings.get("alpha", {})

    fabric = FeatureFabric(
        spread_max_points=settings.get("spread", {}).get("max_points", 0.00030),
        redis_client=redis_client,
    )
    state = RedisStateManager(client=redis_client)
    pg_writer = PostgresWriter(session_factory=session_factory)
    classifier = RegimeClassifier(
        adx_trend_threshold=regime_cfg.get("adx_trend_threshold", 31.0),
        adx_range_threshold=regime_cfg.get("adx_range_threshold", 22.0),
    )
    momentum = MomentumEngine(
        min_rr=alpha_cfg.get("min_rr_ratio", 1.8),
    )
    mr = MeanReversionEngine(
        adf_pvalue=alpha_cfg.get("adf_pvalue_threshold", 0.05),
        min_rr=alpha_cfg.get("min_rr_ratio", 1.8),
        zscore_guard=alpha_cfg.get("zscore_guard", 3.0),
        min_conviction=alpha_cfg.get("conviction_threshold", 0.65),
    )
    perf_db = PerformanceDatabase(session_factory=session_factory)
    cal_engine = CalibrationEngine(perf_db=perf_db)
    covariance = EWMACovarianceMatrix(
        pairs=pairs,
        lambda_=risk_cfg.get("ewma_lambda", 0.999),
        kappa_warn=risk_cfg.get("condition_number_warn", 15.0),
        kappa_max=risk_cfg.get("condition_number_max", 30.0),
    )
    kill_switch = KillSwitch(
        redis_client=redis_client,
        session_factory=session_factory,
        mt5_client=mt5,
    )
    governor = RiskGovernor(kill_switch=kill_switch, covariance=covariance)
    reconciler = StateReconciler(
        mt5_client=mt5,
        redis_client=redis_client,
        kill_switch=kill_switch,
        session_factory=session_factory,
        heartbeat=settings.get("reconciler", {}).get("heartbeat_seconds", 5.0),
    )

    is_paper = settings.get("system", {}).get("mode", "paper") == "paper"
    gateway = ExecutionGateway(
        mt5_client=mt5,
        kill_switch=kill_switch,
        paper_mode=is_paper,
    )
    fill_tracker = FillTracker(session_factory=session_factory)
    recorder = TradeOutcomeRecorder(perf_db=perf_db)
    updater = KellyInputUpdater(perf_db=perf_db, redis_client=redis_client)

    feed = MarketFeed(
        client=mt5,
        pairs=pairs,
        poll_interval=settings.get("mt5", {}).get("poll_interval", 5.0),
    )

    return PipelineContext(
        mt5=mt5,
        feed=feed,
        fabric=fabric,
        state=state,
        pg_writer=pg_writer,
        classifier=classifier,
        momentum=momentum,
        mr=mr,
        cal_engine=cal_engine,
        perf_db=perf_db,
        governor=governor,
        kill_switch=kill_switch,
        covariance=covariance,
        reconciler=reconciler,
        gateway=gateway,
        fill_tracker=fill_tracker,
        recorder=recorder,
        updater=updater,
        settings=settings,
    )


# ── Core Pipeline Logic ──────────────────────────────────────────────

async def process_tick(
    snapshot: MarketSnapshot,
    ctx: PipelineContext,
    *,
    approval_timestamp_ms: int | None = None,
) -> None:
    """Process one MarketSnapshot through the full pipeline.

    Parameters
    ----------
    snapshot
        Validated MarketSnapshot (from ZMQ or simulation).
    ctx
        Initialised PipelineContext.
    approval_timestamp_ms
        Override for the staleness check in ExecutionGateway.
        When None, uses ``snapshot.timestamp`` (correct for live mode).
        Simulation should pass ``int(time.time() * 1000)``.
    """
    if approval_timestamp_ms is None:
        approval_timestamp_ms = snapshot.timestamp

    # Gate 0: Kill switch
    if ctx.kill_switch.is_active:
        logger.debug("tick_skipped", reason="kill_switch_active", pair=snapshot.pair)
        return

    # Step 1: Compute features
    try:
        fv = ctx.fabric.compute(snapshot)
    except ValueError:
        logger.warning("tick_skipped", reason="insufficient_candles", pair=snapshot.pair)
        return

    ctx.state.store_feature_vector(fv)

    # Step 2: Classify regime
    close_price = snapshot.candles.H1[-1].close
    regime = ctx.classifier.classify(fv, close_price)

    # Step 3: Check paper position closes
    _check_paper_closes(snapshot, ctx)

    # Step 4: Skip signal generation if UNDEFINED
    if regime == Regime.UNDEFINED:
        logger.debug("tick_skipped", reason="regime_undefined", pair=snapshot.pair)
        return

    # Step 5: Generate alpha signals
    hypotheses = []
    mom_sig = ctx.momentum.generate(fv, regime, snapshot)
    if mom_sig is not None:
        hypotheses.append(mom_sig)
    mr_sig = ctx.mr.generate(fv, regime, snapshot)
    if mr_sig is not None:
        hypotheses.append(mr_sig)

    if not hypotheses:
        return

    # Step 6: Get account state
    account = ctx.mt5.account_info()
    equity = account.equity if account else 10_000.0
    balance = account.balance if account and account.balance > 0 else equity
    current_dd = max(0.0, 1.0 - (equity / balance))

    positions = ctx.mt5.positions_get() or []
    open_pos_dicts = [_position_to_dict(p) for p in positions]

    # Step 7: Calibrate → Risk → Execute for each hypothesis
    for hyp in hypotheses:
        intent = ctx.cal_engine.calibrate(
            hyp,
            snapshot.session.value,
            current_dd,
            open_pos_dicts,
        )
        if intent is None:
            continue

        decision = await ctx.governor.evaluate(
            hyp, intent, snapshot, equity, current_dd, open_pos_dicts,
        )
        if decision.decision != Decision.APPROVE:
            continue

        fill = ctx.gateway.execute(
            hyp, decision, equity, approval_timestamp_ms,
        )
        if fill is None:
            continue

        ctx.fill_tracker.record_fill(fill)

        # Track paper positions for SL/TP close detection
        if fill.is_paper:
            ctx.paper_positions[fill.order_id] = {
                "pair": hyp.pair,
                "direction": hyp.direction.value,
                "stop_loss": hyp.stop_loss,
                "take_profit": hyp.take_profit,
            }

        logger.info(
            "trade_opened",
            pair=hyp.pair,
            direction=hyp.direction.value,
            strategy=hyp.strategy.value,
            regime=regime.value,
            is_paper=fill.is_paper,
            order_id=fill.order_id,
        )


# ── Paper Position Close Detection ───────────────────────────────────

def _check_paper_closes(snapshot: MarketSnapshot, ctx: PipelineContext) -> None:
    """Check if any paper positions hit SL or TP on the latest candle."""
    if not ctx.paper_positions:
        return

    closed_ids: list[int] = []

    for order_id, pos in ctx.paper_positions.items():
        if pos["pair"] != snapshot.pair:
            continue

        candle = snapshot.candles.M5[-1]
        close_price: float | None = None

        if pos["direction"] == "LONG":
            if candle.low <= pos["stop_loss"]:
                close_price = pos["stop_loss"]
            elif candle.high >= pos["take_profit"]:
                close_price = pos["take_profit"]
        else:  # SHORT
            if candle.high >= pos["stop_loss"]:
                close_price = pos["stop_loss"]
            elif candle.low <= pos["take_profit"]:
                close_price = pos["take_profit"]

        if close_price is not None:
            outcome = ctx.fill_tracker.record_close(
                order_id=order_id,
                close_price=close_price,
                close_time_ms=snapshot.timestamp,
                stop_loss=pos["stop_loss"],
                session_label=snapshot.session.value,
            )
            if outcome is not None:
                ctx.recorder.record(outcome)
                ctx.updater.update_segment(
                    outcome["strategy"],
                    outcome["regime"],
                    outcome["session"],
                )
            closed_ids.append(order_id)

    for oid in closed_ids:
        del ctx.paper_positions[oid]


# ── Helpers ──────────────────────────────────────────────────────────

def _position_to_dict(pos: Any) -> dict[str, Any]:
    """Convert an MT5 Position dataclass to a dict for the risk governor."""
    return {
        "ticket": pos.ticket,
        "symbol": pos.symbol,
        "type": pos.type,
        "volume": pos.volume,
        "price_open": pos.price_open,
        "price_current": pos.price_current,
        "sl": pos.sl,
        "tp": pos.tp,
        "profit": pos.profit,
    }


# ── Live Mode ────────────────────────────────────────────────────────

async def _async_main() -> None:
    """Live pipeline loop: ZMQ PULL + background services."""
    from ops.apex_wrapper import is_shutting_down

    settings = load_settings()
    ctx = init_context(settings)

    # Start Prometheus metrics
    port = settings.get("prometheus", {}).get("port", 8000)
    start_metrics_server(port)

    # Recover kill switch from DB
    await ctx.kill_switch.recover_from_db()

    # Start background services
    feed_task = asyncio.create_task(ctx.feed.run())
    recon_task = asyncio.create_task(ctx.reconciler.run())

    # ZMQ PULL socket
    zmq_ctx = zmq.asyncio.Context()
    zmq_sock = zmq_ctx.socket(zmq.PULL)
    zmq_sock.connect(_ZMQ_ADDR)

    logger.info("pipeline_started", mode=settings.get("system", {}).get("mode", "paper"))

    try:
        while not is_shutting_down():
            events = await zmq_sock.poll(timeout=_ZMQ_POLL_TIMEOUT_MS)
            if events:
                msg = await zmq_sock.recv_string()
                snapshot = MarketSnapshot.model_validate_json(msg)
                await process_tick(snapshot, ctx)
    except Exception:
        logger.exception("pipeline_unhandled_exception")
        await ctx.kill_switch.trigger("EMERGENCY", "unhandled exception in pipeline")
        sys.exit(1)
    finally:
        logger.info("pipeline_shutting_down")
        feed_task.cancel()
        ctx.reconciler.stop()

        # Close remaining paper positions at current price
        for order_id, pos in list(ctx.paper_positions.items()):
            tick = ctx.mt5.symbol_info_tick(pos["pair"])
            if tick is not None:
                cp = tick.bid if pos["direction"] == "LONG" else tick.ask
                ctx.fill_tracker.record_close(
                    order_id, cp, int(time.time() * 1000),
                    pos["stop_loss"], "SHUTDOWN",
                )
        ctx.paper_positions.clear()

        ctx.mt5.shutdown()
        zmq_sock.close(linger=0)
        zmq_ctx.term()
        logger.info("pipeline_stopped")


def main() -> None:
    """Sync entry point called by ``ops/apex_wrapper.py``."""
    asyncio.run(_async_main())
