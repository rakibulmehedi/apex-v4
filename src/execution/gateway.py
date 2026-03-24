"""ExecutionGateway — Pre-flight checks + MT5 order execution.

Phase 4 (P4.1).
Only reached after RiskDecision = APPROVE from the risk governor.

Pre-flight checks before every order:
  1. kill_switch.allows_new_signals() must be True
  2. decision.decision must be APPROVE
  3. decision.final_size > 0
  4. entry prices, stop_loss, take_profit all non-zero
  5. RiskDecision age < 2000ms (reject stale approvals)

Volume calculation:
  volume = round(final_size × portfolio_equity / 100_000, 2)
  volume = clamp(volume, 0.01, 100.0)

Paper trading mode (settings.yaml system.mode: "paper"):
  - Skips mt5.order_send() entirely
  - Simulates fill at current ask/bid with slippage = 0
  - Logs "PAPER TRADE: {pair} {direction} {volume} lots"

Architecture ref: APEX_V4_STRATEGY.md Section 5, Phase 4 (P4.1)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from src.market.mt5_client import MT5Client
from src.market.mt5_types import TRADE_RETCODE_DONE
from src.market.schemas import (
    AlphaHypothesis,
    Decision,
    Direction,
    RiskDecision,
)
from src.risk.kill_switch import KillSwitch

logger = structlog.get_logger(__name__)

# ── constants ────────────────────────────────────────────────────────

_MAX_APPROVAL_AGE_MS = 2000   # reject approvals older than 2 seconds
_MIN_VOLUME = 0.01            # MT5 minimum lot size
_MAX_VOLUME = 100.0           # MT5 maximum lot size
_LOT_SIZE = 100_000           # standard forex lot


@dataclass(frozen=True, slots=True)
class FillRecord:
    """Represents a confirmed (or paper-simulated) fill.

    Passed downstream to FillTracker for slippage measurement
    and PostgreSQL persistence.
    """

    order_id: int
    pair: str
    direction: str
    strategy: str
    regime: str
    requested_price: float
    fill_price: float
    requested_volume: float
    filled_volume: float
    slippage_points: float
    is_paper: bool
    filled_at_ms: int


class ExecutionGateway:
    """Pre-flight checks → MT5 order execution → FillRecord.

    Parameters
    ----------
    mt5_client
        MT5Client instance (real or stub).
    kill_switch
        KillSwitch instance for gate checks.
    paper_mode
        When True, skip ``mt5.order_send()`` and simulate fills.
    """

    def __init__(
        self,
        mt5_client: MT5Client,
        kill_switch: KillSwitch,
        paper_mode: bool = True,
    ) -> None:
        self._mt5 = mt5_client
        self._ks = kill_switch
        self._paper = paper_mode

    # ── public API ──────────────────────────────────────────────────

    def execute(
        self,
        hypothesis: AlphaHypothesis,
        decision: RiskDecision,
        portfolio_equity: float,
        approval_timestamp_ms: int,
    ) -> FillRecord | None:
        """Execute a trade after pre-flight validation.

        Parameters
        ----------
        hypothesis
            The AlphaHypothesis that generated the signal.
        decision
            RiskDecision from the risk governor (must be APPROVE).
        portfolio_equity
            Current portfolio equity in account currency.
        approval_timestamp_ms
            Unix ms when the RiskDecision was created.
            Used for staleness check.

        Returns
        -------
        FillRecord | None
            Fill details on success, None on any rejection or failure.
        """
        pair = hypothesis.pair

        # ── Pre-flight 1: Kill switch ───────────────────────────────
        if not self._ks.allows_new_signals():
            logger.warning(
                "gateway_rejected",
                pair=pair,
                reason="kill_switch_active",
            )
            return None

        # ── Pre-flight 2: Decision must be APPROVE ──────────────────
        if decision.decision != Decision.APPROVE:
            logger.warning(
                "gateway_rejected",
                pair=pair,
                reason=f"decision_not_approve: {decision.decision.value}",
            )
            return None

        # ── Pre-flight 3: Size must be positive ─────────────────────
        if decision.final_size <= 0:
            logger.warning(
                "gateway_rejected",
                pair=pair,
                reason="final_size_zero_or_negative",
                final_size=decision.final_size,
            )
            return None

        # ── Pre-flight 4: Price sanity ──────────────────────────────
        if hypothesis.stop_loss == 0 or hypothesis.take_profit == 0:
            logger.warning(
                "gateway_rejected",
                pair=pair,
                reason="zero_sl_or_tp",
                stop_loss=hypothesis.stop_loss,
                take_profit=hypothesis.take_profit,
            )
            return None

        entry_lo, entry_hi = hypothesis.entry_zone
        if entry_lo == 0 or entry_hi == 0:
            logger.warning(
                "gateway_rejected",
                pair=pair,
                reason="zero_entry_price",
                entry_zone=hypothesis.entry_zone,
            )
            return None

        # ── Pre-flight 5: Staleness ─────────────────────────────────
        now_ms = int(time.time() * 1000)
        age_ms = now_ms - approval_timestamp_ms
        if age_ms > _MAX_APPROVAL_AGE_MS:
            logger.warning(
                "gateway_rejected",
                pair=pair,
                reason="stale_approval",
                age_ms=age_ms,
                max_age_ms=_MAX_APPROVAL_AGE_MS,
            )
            return None

        # ── Volume calculation ──────────────────────────────────────
        raw_volume = decision.final_size * portfolio_equity / _LOT_SIZE
        volume = round(raw_volume, 2)
        volume = max(_MIN_VOLUME, min(volume, _MAX_VOLUME))

        # ── Get current tick price ──────────────────────────────────
        tick = self._mt5.symbol_info_tick(pair)
        if tick is None:
            logger.error(
                "gateway_rejected",
                pair=pair,
                reason="no_tick_data",
            )
            return None

        if hypothesis.direction == Direction.LONG:
            requested_price = tick.ask
            order_type = 0  # ORDER_TYPE_BUY
        else:
            requested_price = tick.bid
            order_type = 1  # ORDER_TYPE_SELL

        # ── Paper trading mode ──────────────────────────────────────
        if self._paper:
            return self._paper_fill(
                hypothesis=hypothesis,
                volume=volume,
                fill_price=requested_price,
            )

        # ── Live execution ──────────────────────────────────────────
        request = {
            "action": 1,  # TRADE_ACTION_DEAL
            "symbol": pair,
            "volume": volume,
            "type": order_type,
            "price": requested_price,
            "sl": hypothesis.stop_loss,
            "tp": hypothesis.take_profit,
            "magic": 4,  # APEX V4 magic number
            "comment": f"APEX_V4_{hypothesis.strategy.value}",
        }

        result = self._mt5.order_send(request)

        if result is None:
            logger.error(
                "gateway_order_send_failed",
                pair=pair,
                reason="order_send_returned_none",
            )
            return None

        if result.retcode != TRADE_RETCODE_DONE:
            logger.error(
                "gateway_order_send_failed",
                pair=pair,
                retcode=result.retcode,
                comment=result.comment,
            )
            return None

        # ── Build FillRecord from confirmed order ───────────────────
        slippage = abs(result.price - requested_price)
        fill_ts = int(time.time() * 1000)

        fill = FillRecord(
            order_id=result.order,
            pair=pair,
            direction=hypothesis.direction.value,
            strategy=hypothesis.strategy.value,
            regime=hypothesis.regime.value,
            requested_price=requested_price,
            fill_price=result.price,
            requested_volume=volume,
            filled_volume=result.volume,
            slippage_points=slippage,
            is_paper=False,
            filled_at_ms=fill_ts,
        )

        logger.info(
            "gateway_fill_confirmed",
            pair=pair,
            order_id=result.order,
            direction=hypothesis.direction.value,
            volume=result.volume,
            fill_price=result.price,
            slippage=round(slippage, 6),
        )

        return fill

    # ── paper trading helper ────────────────────────────────────────

    def _paper_fill(
        self,
        hypothesis: AlphaHypothesis,
        volume: float,
        fill_price: float,
    ) -> FillRecord:
        """Simulate a fill without touching MT5 order_send.

        Paper fills have slippage = 0 and a synthetic order_id
        based on the current timestamp.
        """
        fill_ts = int(time.time() * 1000)
        order_id = fill_ts  # synthetic ticket

        logger.info(
            "PAPER TRADE",
            pair=hypothesis.pair,
            direction=hypothesis.direction.value,
            volume=volume,
            fill_price=fill_price,
        )

        return FillRecord(
            order_id=order_id,
            pair=hypothesis.pair,
            direction=hypothesis.direction.value,
            strategy=hypothesis.strategy.value,
            regime=hypothesis.regime.value,
            requested_price=fill_price,
            fill_price=fill_price,
            requested_volume=volume,
            filled_volume=volume,
            slippage_points=0.0,
            is_paper=True,
            filled_at_ms=fill_ts,
        )
