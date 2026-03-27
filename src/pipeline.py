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
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path regardless of invocation method
# (e.g. `python src/pipeline.py` sets sys.path[0]=src/, not the project root)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import redis
import structlog
import yaml
import zmq
import zmq.asyncio

from sqlalchemy import func, inspect as sa_inspect, text

from db.models import ReconciliationLog, TradeOutcome, make_session_factory
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
from src.observability.metrics import CYCLE_DURATION_MS, start_metrics_server
from src.regime.classifier import RegimeClassifier
from src.risk.covariance import EWMACovarianceMatrix
from src.risk.governor import RiskGovernor
from src.risk.kill_switch import KillSwitch
from src.risk.reconciler import StateReconciler

logger = structlog.get_logger(__name__)

_ZMQ_ADDR = "tcp://127.0.0.1:5559"
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


# ── ANSI Colour Helpers ──────────────────────────────────────────────

_RED = "\033[91m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _red(msg: str) -> str:
    return f"{_RED}{msg}{_RESET}"


def _green(msg: str) -> str:
    return f"{_GREEN}{msg}{_RESET}"


def _yellow(msg: str) -> str:
    return f"{_YELLOW}{msg}{_RESET}"


def _bold(msg: str) -> str:
    return f"{_BOLD}{msg}{_RESET}"


# ── Pre-Flight Validation ────────────────────────────────────────────

@dataclass
class PreflightResult:
    """Outcome of a single pre-flight check."""

    name: str
    passed: bool
    detail: str = ""
    fix: str = ""


def _check_v3_data_imported(session_factory: Any) -> PreflightResult:
    """Check 1: At least 1 row in trade_outcomes with V3 provenance (fill_id IS NULL)."""
    name = "V3 data imported"
    try:
        with session_factory() as db:
            count = (
                db.query(func.count(TradeOutcome.id))
                .filter(TradeOutcome.fill_id.is_(None))
                .scalar()
            )
            if count and count > 0:
                return PreflightResult(name=name, passed=True, detail=f"{count} V3 rows found")
            return PreflightResult(
                name=name,
                passed=False,
                detail="No V3 historical trades found in trade_outcomes (fill_id IS NULL)",
                fix="Run: python scripts/migrate_v3_data.py  — imports V3 paper trades into trade_outcomes",
            )
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"Database query failed: {exc}",
            fix="Ensure PostgreSQL is running and APEX_DATABASE_URL is set correctly",
        )


_ACTIVE_STRATEGIES = ["MOMENTUM", "MEAN_REVERSION"]
_ACTIVE_REGIMES = ["TRENDING_UP", "TRENDING_DOWN", "RANGING"]  # UNDEFINED excluded
_ACTIVE_SESSIONS = ["LONDON", "NY", "ASIA", "OVERLAP"]
_MIN_SEGMENT_TRADES = 30


def _check_segment_counts(session_factory: Any) -> PreflightResult:
    """Check 2: All active trading segments have >= 30 outcomes."""
    name = "Segment trade counts"
    try:
        thin: list[str] = []
        with session_factory() as db:
            for strategy in _ACTIVE_STRATEGIES:
                for regime in _ACTIVE_REGIMES:
                    for session in _ACTIVE_SESSIONS:
                        count = (
                            db.query(func.count(TradeOutcome.id))
                            .filter(
                                TradeOutcome.strategy == strategy,
                                TradeOutcome.regime == regime,
                                TradeOutcome.session == session,
                            )
                            .scalar()
                        ) or 0
                        if count < _MIN_SEGMENT_TRADES:
                            thin.append(f"  {strategy}/{regime}/{session}: {count}/{_MIN_SEGMENT_TRADES}")

        if not thin:
            return PreflightResult(name=name, passed=True, detail="All 24 active segments have >= 30 outcomes")

        return PreflightResult(
            name=name,
            passed=False,
            detail=f"{len(thin)} segment(s) below minimum:\n" + "\n".join(thin),
            fix="Run: python scripts/migrate_v3_data.py  — or collect more paper trades until all segments reach 30",
        )
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"Database query failed: {exc}",
            fix="Ensure PostgreSQL is running and APEX_DATABASE_URL is set correctly",
        )


def _check_kill_switch(session_factory: Any) -> PreflightResult:
    """Check 3: Kill switch state is INACTIVE (NONE)."""
    name = "Kill switch INACTIVE"
    try:
        from db.models import KillSwitchEvent

        with session_factory() as db:
            row = (
                db.query(KillSwitchEvent)
                .order_by(KillSwitchEvent.timestamp_ms.desc())
                .first()
            )
            if row is None:
                return PreflightResult(name=name, passed=True, detail="No kill switch events — state is NONE")
            state = str(row.new_state)
            if state == "NONE":
                return PreflightResult(name=name, passed=True, detail="Kill switch last reset to NONE")
            return PreflightResult(
                name=name,
                passed=False,
                detail=f"Kill switch is {state} (reason: {row.reason})",
                fix='Reset via: KillSwitch.manual_reset("I CONFIRM SYSTEM IS SAFE", operator="<your_name>")',
            )
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"Database query failed: {exc}",
            fix="Ensure PostgreSQL is running and kill_switch_events table exists",
        )


def _check_redis(settings: dict[str, Any]) -> PreflightResult:
    """Check 4: Redis is reachable and responding to PING."""
    name = "Redis reachable"
    redis_url = os.environ.get("APEX_REDIS_URL", "redis://localhost:6379/0")
    try:
        r = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
        r.ping()
        return PreflightResult(name=name, passed=True, detail=f"PING OK ({redis_url})")
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"Redis unreachable at {redis_url}: {exc}",
            fix="Start Redis: redis-server  — or set APEX_REDIS_URL to the correct address",
        )


_REQUIRED_TABLES = [
    "market_snapshots", "candles", "feature_vectors", "trade_outcomes",
    "kill_switch_events", "fills", "reconciliation_log",
]


def _check_postgres(session_factory: Any) -> PreflightResult:
    """Check 5: PostgreSQL reachable and all required tables exist."""
    name = "PostgreSQL reachable + tables"
    try:
        with session_factory() as db:
            db.execute(text("SELECT 1"))

        engine = session_factory.kw.get("bind") or session_factory().get_bind()
        inspector = sa_inspect(engine)
        existing = set(inspector.get_table_names())
        missing = [t for t in _REQUIRED_TABLES if t not in existing]

        if missing:
            return PreflightResult(
                name=name,
                passed=False,
                detail=f"Missing tables: {', '.join(missing)}",
                fix="Run: python -c \"from db.models import Base, make_engine; Base.metadata.create_all(make_engine())\"",
            )
        return PreflightResult(name=name, passed=True, detail=f"Connected, all {len(_REQUIRED_TABLES)} tables present")
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"PostgreSQL unreachable: {exc}",
            fix="Start PostgreSQL and set APEX_DATABASE_URL  — e.g. postgresql://user:pass@localhost:5432/apex_v4",
        )


def _check_mt5(settings: dict[str, Any]) -> PreflightResult:
    """Check 6: MT5 connection succeeds — account_info() returns non-None."""
    name = "MT5 connection"
    try:
        mt5 = get_mt5_client(settings.get("mt5", {}).get("mode"))
        mt5.initialize()
        account = mt5.account_info()
        if account is not None:
            return PreflightResult(
                name=name, passed=True,
                detail=f"Login {account.login} on {account.server}, equity={account.equity}",
            )
        return PreflightResult(
            name=name,
            passed=False,
            detail="mt5.account_info() returned None — terminal may not be logged in",
            fix="Open MT5 terminal, log in to your account, then retry",
        )
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"MT5 initialisation failed: {exc}",
            fix="Ensure MT5 terminal is running. On Windows set mt5.mode=real in config/settings.yaml",
        )


_MIN_PAPER_DAYS = 7


def _check_paper_duration(session_factory: Any) -> PreflightResult:
    """Check 7: Paper trading ran for >= 7 days (timestamp span in trade_outcomes)."""
    name = "Paper trading >= 7 days"
    try:
        with session_factory() as db:
            row = db.query(
                func.min(TradeOutcome.opened_at).label("earliest"),
                func.max(TradeOutcome.closed_at).label("latest"),
            ).one()

            if row.earliest is None or row.latest is None:
                return PreflightResult(
                    name=name,
                    passed=False,
                    detail="No trade outcomes found — paper trading has not started",
                    fix="Run the pipeline in paper mode for at least 7 days before going live",
                )

            span = row.latest - row.earliest
            days = span.days
            if days >= _MIN_PAPER_DAYS:
                return PreflightResult(name=name, passed=True, detail=f"{days} days of paper trading history")
            return PreflightResult(
                name=name,
                passed=False,
                detail=f"Only {days} day(s) of paper history (need {_MIN_PAPER_DAYS})",
                fix=f"Continue paper trading for {_MIN_PAPER_DAYS - days} more day(s) before going live",
            )
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"Database query failed: {exc}",
            fix="Ensure PostgreSQL is running and trade_outcomes table is populated",
        )


def _check_no_state_drift(session_factory: Any) -> PreflightResult:
    """Check 8: Zero unresolved state drift events in reconciliation_log."""
    name = "No unresolved state drift"
    try:
        with session_factory() as db:
            count = (
                db.query(func.count(ReconciliationLog.id))
                .filter(ReconciliationLog.mismatch_detected.is_(True))
                .scalar()
            ) or 0

            if count == 0:
                return PreflightResult(name=name, passed=True, detail="No state drift events found")
            return PreflightResult(
                name=name,
                passed=False,
                detail=f"{count} unresolved state drift event(s) in reconciliation_log",
                fix=(
                    "Investigate each mismatch in reconciliation_log. "
                    "Resolve the root cause, then clear the table or mark them resolved. "
                    "Query: SELECT * FROM reconciliation_log WHERE mismatch_detected = true"
                ),
            )
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"Database query failed: {exc}",
            fix="Ensure PostgreSQL is running and reconciliation_log table exists",
        )


def _check_capital_allocation(settings: dict[str, Any]) -> PreflightResult:
    """Check 9: settings.yaml has capital_allocation_pct configured."""
    name = "capital_allocation_pct configured"
    risk_cfg = settings.get("risk", {})
    cap_pct = risk_cfg.get("capital_allocation_pct")
    if cap_pct is not None:
        try:
            val = float(cap_pct)
            if 0.0 < val <= 1.0:
                return PreflightResult(name=name, passed=True, detail=f"capital_allocation_pct = {val}")
            return PreflightResult(
                name=name,
                passed=False,
                detail=f"capital_allocation_pct = {val} — must be in (0.0, 1.0]",
                fix="Edit config/settings.yaml → risk.capital_allocation_pct to a value like 0.10",
            )
        except (TypeError, ValueError):
            return PreflightResult(
                name=name,
                passed=False,
                detail=f"capital_allocation_pct = {cap_pct!r} — not a valid number",
                fix="Edit config/settings.yaml → risk.capital_allocation_pct to a numeric value like 0.10",
            )

    return PreflightResult(
        name=name,
        passed=False,
        detail="capital_allocation_pct is missing from settings.yaml",
        fix="Add to config/settings.yaml under 'risk:'  →  capital_allocation_pct: 0.10",
    )


_MT5_CREDENTIAL_KEYS = ["MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"]


def _check_secrets_env(secrets_path: str | Path = "config/secrets.env") -> PreflightResult:
    """Check 10: secrets.env exists and has MT5 credentials set."""
    name = "secrets.env + MT5 credentials"
    path = Path(secrets_path)
    if not path.exists():
        return PreflightResult(
            name=name,
            passed=False,
            detail=f"{path} does not exist",
            fix=f"Create {path} with MT5_LOGIN, MT5_PASSWORD, MT5_SERVER values",
        )

    try:
        content = path.read_text()
    except Exception as exc:
        return PreflightResult(
            name=name, passed=False,
            detail=f"Cannot read {path}: {exc}",
            fix=f"Check file permissions on {path}",
        )

    # Parse key=value lines (ignore comments and blanks)
    env_vars: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env_vars[key.strip()] = value.strip()

    missing: list[str] = []
    empty: list[str] = []
    for key in _MT5_CREDENTIAL_KEYS:
        if key not in env_vars:
            missing.append(key)
        elif not env_vars[key]:
            empty.append(key)

    issues: list[str] = []
    if missing:
        issues.append(f"missing keys: {', '.join(missing)}")
    if empty:
        issues.append(f"empty values: {', '.join(empty)}")

    if issues:
        return PreflightResult(
            name=name,
            passed=False,
            detail=f"{path} — {'; '.join(issues)}",
            fix=f"Edit {path} and set: {', '.join(_MT5_CREDENTIAL_KEYS)} to your MT5 broker credentials",
        )

    return PreflightResult(name=name, passed=True, detail=f"{path} — all MT5 credentials present")


def run_preflight(
    settings: dict[str, Any],
    *,
    session_factory: Any = None,
    secrets_path: str | Path = "config/secrets.env",
    _input_fn: Any = None,
) -> float:
    """Run 9 pre-flight checks with paper trading bypass for checks 8-9.

    Checks 1-7 are hard requirements — any failure blocks startup.
    Checks 8-9 (V3 data imported, segment counts) are bypassed in paper
    mode with a yellow warning; in live mode they block startup.

    Parameters
    ----------
    settings
        Parsed ``config/settings.yaml``.
    session_factory
        SQLAlchemy sessionmaker (created from env if None).
    secrets_path
        Path to the secrets env file.
    _input_fn
        Override for ``input()`` — used by tests.

    Returns
    -------
    float
        The ``capital_allocation_pct`` confirmed by the operator.

    Raises
    ------
    SystemExit
        If any hard check fails, or bypassable checks fail in live mode,
        or the operator does not confirm.
    """
    if session_factory is None:
        session_factory = make_session_factory()

    prompt_input = _input_fn or input
    trading_mode = settings.get("system", {}).get("mode", "paper")

    # Checks 1-7: hard requirements (any failure blocks startup)
    hard_checks: list[PreflightResult] = [
        _check_redis(settings),                      # 1
        _check_postgres(session_factory),             # 2
        _check_mt5(settings),                         # 3
        _check_kill_switch(session_factory),          # 4
        _check_no_state_drift(session_factory),       # 5
        _check_capital_allocation(settings),          # 6
        _check_secrets_env(secrets_path),             # 7
    ]

    # Checks 8-9: bypassable in paper mode
    bypassable_checks: list[PreflightResult] = [
        _check_v3_data_imported(session_factory),     # 8
        _check_segment_counts(session_factory),       # 9
    ]

    all_checks = hard_checks + bypassable_checks
    hard_failed = [r for r in hard_checks if not r.passed]
    bypass_failed = [r for r in bypassable_checks if not r.passed]

    print()
    print(_bold("═══ APEX V4 — Pre-Flight Validation ═══"))
    print(f"  Trading mode: {_bold(trading_mode)}")
    print()

    for i, r in enumerate(all_checks, 1):
        is_bypassable = i >= 8
        if r.passed:
            print(f"  {_green('✓')} [{i:02d}] {r.name}: {r.detail}")
        elif is_bypassable and trading_mode == "paper":
            print(f"  {_yellow('⚠')} [{i:02d}] {r.name}")
            print(f"       {_yellow('Why:')} {r.detail}")
            print(f"       {_yellow('Bypassed:')} paper mode — see warning below")
        else:
            print(f"  {_red('✗')} [{i:02d}] {r.name}")
            print(f"       {_red('Why:')} {r.detail}")
            print(f"       {_red('Fix:')} {r.fix}")
        print()

    # Hard failures always block
    if hard_failed:
        print(_red(_bold(f"BLOCKED — {len(hard_failed)} check(s) failed. Fix them and retry.")))
        print()
        sys.exit(1)

    # Bypassable failures: block in live mode, warn in paper mode
    if bypass_failed:
        if trading_mode != "paper":
            print(_red(_bold(
                f"BLOCKED — {len(bypass_failed)} data check(s) failed. "
                "Live mode requires full trade history. Fix them and retry."
            )))
            print()
            sys.exit(1)
        else:
            print(_yellow(_bold(
                "PAPER MODE ENABLED: Insufficient segment history. "
                "Bootstrapping database natively with default minimum risk."
            )))
            print()

    # ── All passed (or bypassed) — operator confirmation ─────────────
    cap_pct = float(settings["risk"]["capital_allocation_pct"])
    passed_count = sum(1 for r in all_checks if r.passed)
    bypassed_count = len(bypass_failed)
    if bypassed_count:
        print(_green(_bold(f"{passed_count} checks passed, {bypassed_count} bypassed (paper mode).")))
    else:
        print(_green(_bold("All 9 checks passed.")))
    print()
    print(f"  Confirm startup with capital_allocation_pct = {_bold(str(cap_pct))}")
    print(f'  Type exactly: CONFIRMED {cap_pct}')
    print()

    try:
        answer = prompt_input(">>> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print(_red("Startup aborted."))
        sys.exit(1)

    expected = f"CONFIRMED {cap_pct}"
    if answer != expected:
        print()
        print(_red(f"Expected '{expected}', got '{answer}'. Startup aborted."))
        sys.exit(1)

    logger.info(
        "preflight_passed",
        capital_allocation_pct=cap_pct,
        trading_mode=trading_mode,
        bypassed=bypassed_count,
    )
    return cap_pct


# ── Context Initialisation ───────────────────────────────────────────

def init_context(
    settings: dict[str, Any],
    *,
    session_factory: Any = None,
    redis_client: Any = None,
) -> PipelineContext:
    """Build every component once.  DI overrides for tests."""
    if session_factory is None:
        session_factory = make_session_factory()
    if redis_client is None:
        redis_url = os.environ.get("APEX_REDIS_URL", "redis://localhost:6379/0")
        redis_client = redis.Redis.from_url(redis_url, decode_responses=True)

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
    cal_engine = CalibrationEngine(
        perf_db=perf_db,
        capital_allocation_pct=risk_cfg.get("capital_allocation_pct", 1.0),
    )
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

    zmq_addr = settings.get("zmq", {}).get("address", _ZMQ_ADDR)
    feed = MarketFeed(
        client=mt5,
        pairs=pairs,
        zmq_addr=zmq_addr,
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

    # Step 6: Get account state — broker is truth, skip if unavailable
    account = ctx.mt5.account_info()
    if account is None:
        logger.warning("tick_skipped", reason="account_info_unavailable", pair=snapshot.pair)
        return
    equity = account.equity
    balance = account.balance if account.balance > 0 else equity
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

    # ── Pre-flight validation — blocks startup on any failure ─────
    cap_pct = run_preflight(settings)
    logger.info(
        "capital_allocation",
        pct=cap_pct * 100,
        msg=f"Capital allocation: {cap_pct * 100:.0f}% of portfolio",
    )

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
    zmq_addr = settings.get("zmq", {}).get("address", _ZMQ_ADDR)
    zmq_ctx = zmq.asyncio.Context()
    zmq_sock = zmq_ctx.socket(zmq.PULL)
    zmq_sock.connect(zmq_addr)

    logger.info("pipeline_started", mode=settings.get("system", {}).get("mode", "paper"))

    try:
        while not is_shutting_down():
            events = await zmq_sock.poll(timeout=_ZMQ_POLL_TIMEOUT_MS)
            if events:
                cycle_start = time.monotonic()
                msg = await zmq_sock.recv_string()
                snapshot = MarketSnapshot.model_validate_json(msg)
                await process_tick(snapshot, ctx)
                CYCLE_DURATION_MS.observe(
                    (time.monotonic() - cycle_start) * 1000
                )
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
    # Windows defaults to ProactorEventLoop which lacks add_reader() needed
    # by pyzmq async sockets.  SelectorEventLoop works on all platforms.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_async_main())
