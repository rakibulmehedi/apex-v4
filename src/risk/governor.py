"""RiskGovernor — 7-gate sequential risk governor.

Phase 3 (P3.6).
Gates (fail-fast — first failure returns immediately):
  Gate 1: Kill switch active → REJECT
  Gate 2: Data freshness (snapshot.is_stale) → REJECT
  Gate 3: Signal sanity (SL/TP geometry) → REJECT
  Gate 4: Net directional exposure > 40% → REDUCE 50%
  Gate 5: Portfolio VaR 99% > 5% → REJECT, > 3% → SOFT kill switch
  Gate 6: Covariance condition Φ(κ) == 0 → HARD + REJECT, else scale
  Gate 7: Drawdown > 8% → HARD + REJECT, > 5% → REDUCE 50%

Every gate evaluation is logged with gate number and outcome.
Output: RiskDecision (APPROVE | REJECT | REDUCE)

Architecture ref: APEX_V4_STRATEGY.md Section 4, 7.4, 7.5
"""
from __future__ import annotations

from typing import Any

import structlog

from src.market.schemas import (
    AlphaHypothesis,
    CalibratedTradeIntent,
    Decision,
    Direction,
    MarketSnapshot,
    RiskDecision,
    RiskState,
)
from src.risk.covariance import EWMACovarianceMatrix
from src.risk.kill_switch import KillSwitch

logger = structlog.get_logger(__name__)

# ── thresholds ─────────────────────────────────────────────────────────

_NET_EXPOSURE_LIMIT = 0.40      # Gate 4: 40% net USD exposure
_NET_EXPOSURE_REDUCE = 0.50     # Gate 4: reduce by 50%
_VAR_HARD_LIMIT = 0.05          # Gate 5: 5% → REJECT
_VAR_SOFT_LIMIT = 0.03          # Gate 5: 3% → SOFT kill switch
_DD_HARD_LIMIT = 0.08           # Gate 7: 8% → HARD + REJECT
_DD_REDUCE_LIMIT = 0.05         # Gate 7: 5% → REDUCE 50%
_DD_REDUCE_FACTOR = 0.50        # Gate 7: reduce by 50%


class RiskGovernor:
    """Sequential 7-gate risk governor.

    Parameters
    ----------
    kill_switch
        KillSwitch instance for gate 1 checks and gate 5/6/7 triggers.
    covariance
        EWMACovarianceMatrix for gate 5 (VaR) and gate 6 (Φ(κ)).
    """

    def __init__(
        self,
        kill_switch: KillSwitch,
        covariance: EWMACovarianceMatrix,
    ) -> None:
        self._ks = kill_switch
        self._cov = covariance

    async def evaluate(
        self,
        hypothesis: AlphaHypothesis,
        intent: CalibratedTradeIntent,
        snapshot: MarketSnapshot,
        portfolio_value: float,
        current_dd: float,
        open_positions: list[dict[str, Any]] | None = None,
    ) -> RiskDecision:
        """Run all 7 gates in order.  Fail-fast on first rejection.

        Parameters
        ----------
        hypothesis
            AlphaHypothesis from alpha engine.
        intent
            CalibratedTradeIntent from calibration engine.
        snapshot
            Current MarketSnapshot (for staleness check).
        portfolio_value
            Total portfolio equity.
        current_dd
            Current drawdown as fraction (0.0 = none, 0.08 = 8%).
        open_positions
            List of open position dicts with ``"pair"`` and ``"direction"`` keys.

        Returns
        -------
        RiskDecision
            APPROVE, REJECT, or REDUCE with reason and gate_failed.
        """
        pair = hypothesis.pair
        size = intent.suggested_size
        risk_state = RiskState.NORMAL

        # ── Gate 1: Kill switch ────────────────────────────────────
        if not self._ks.allows_new_signals():
            logger.info("gate_1_REJECT", pair=pair, reason="kill_switch_active")
            return RiskDecision(
                decision=Decision.REJECT,
                final_size=0.0,
                reason="kill_switch_active",
                risk_state=RiskState.HARD_STOP,
                gate_failed=1,
            )
        logger.info("gate_1_PASS", pair=pair)

        # ── Gate 2: Data freshness ────────────────────────────────
        if snapshot.is_stale:
            logger.info("gate_2_REJECT", pair=pair, reason="stale_data")
            return RiskDecision(
                decision=Decision.REJECT,
                final_size=0.0,
                reason="stale_data",
                risk_state=RiskState.NORMAL,
                gate_failed=2,
            )
        logger.info("gate_2_PASS", pair=pair)

        # ── Gate 3: Signal sanity ─────────────────────────────────
        sl = hypothesis.stop_loss
        tp = hypothesis.take_profit
        entry_lo, entry_hi = hypothesis.entry_zone

        invalid = False
        if sl <= 0 or tp <= 0:
            invalid = True
        elif hypothesis.direction == Direction.LONG and sl >= entry_lo:
            invalid = True
        elif hypothesis.direction == Direction.SHORT and sl <= entry_hi:
            invalid = True

        if invalid:
            logger.info(
                "gate_3_REJECT", pair=pair, reason="invalid_signal_geometry",
                sl=sl, tp=tp, entry_zone=hypothesis.entry_zone,
                direction=hypothesis.direction.value,
            )
            return RiskDecision(
                decision=Decision.REJECT,
                final_size=0.0,
                reason="invalid_signal_geometry",
                risk_state=RiskState.NORMAL,
                gate_failed=3,
            )
        logger.info("gate_3_PASS", pair=pair)

        # ── Gate 4: Net directional exposure ──────────────────────
        net_exposure = self._net_usd_exposure(pair, hypothesis.direction, open_positions)
        if net_exposure > _NET_EXPOSURE_LIMIT:
            size *= _NET_EXPOSURE_REDUCE
            risk_state = RiskState.THROTTLE
            logger.info(
                "gate_4_REDUCE", pair=pair,
                net_exposure=round(net_exposure, 4),
                new_size=round(size, 6),
            )
        else:
            logger.info("gate_4_PASS", pair=pair, net_exposure=round(net_exposure, 4))

        # ── Gate 5: Portfolio VaR ─────────────────────────────────
        weights = self._build_weights(pair, size, open_positions)
        var_99 = self._cov.portfolio_var(weights, portfolio_value)
        var_pct = var_99 / portfolio_value if portfolio_value > 0 else 0.0

        if var_pct > _VAR_HARD_LIMIT:
            logger.info(
                "gate_5_REJECT", pair=pair, reason="var_limit_breached",
                var_pct=round(var_pct, 4),
            )
            return RiskDecision(
                decision=Decision.REJECT,
                final_size=0.0,
                reason="var_limit_breached",
                risk_state=RiskState.HARD_STOP,
                gate_failed=5,
            )

        if var_pct > _VAR_SOFT_LIMIT:
            await self._ks.trigger("SOFT", f"VaR {var_pct:.2%} > 3% soft limit")
            risk_state = RiskState.THROTTLE
            logger.info(
                "gate_5_SOFT_TRIGGER", pair=pair,
                var_pct=round(var_pct, 4),
            )
        else:
            logger.info("gate_5_PASS", pair=pair, var_pct=round(var_pct, 4))

        # ── Gate 6: Covariance condition number ───────────────────
        phi = self._cov.decay_multiplier()

        if phi == 0.0:
            await self._ks.trigger("HARD", "correlation_crisis: Φ(κ)=0")
            logger.info("gate_6_REJECT", pair=pair, reason="correlation_crisis")
            return RiskDecision(
                decision=Decision.REJECT,
                final_size=0.0,
                reason="correlation_crisis",
                risk_state=RiskState.HARD_STOP,
                gate_failed=6,
            )

        size *= phi
        logger.info("gate_6_PASS", pair=pair, phi=round(phi, 4), size=round(size, 6))

        # ── Gate 7: Drawdown state ────────────────────────────────
        if current_dd > _DD_HARD_LIMIT:
            await self._ks.trigger("HARD", f"max_drawdown: {current_dd:.2%}")
            logger.info(
                "gate_7_REJECT", pair=pair, reason="max_drawdown",
                current_dd=round(current_dd, 4),
            )
            return RiskDecision(
                decision=Decision.REJECT,
                final_size=0.0,
                reason="max_drawdown",
                risk_state=RiskState.HARD_STOP,
                gate_failed=7,
            )

        if current_dd > _DD_REDUCE_LIMIT:
            size *= _DD_REDUCE_FACTOR
            risk_state = RiskState.THROTTLE
            logger.info(
                "gate_7_REDUCE", pair=pair,
                current_dd=round(current_dd, 4),
                new_size=round(size, 6),
            )
        else:
            logger.info("gate_7_PASS", pair=pair, current_dd=round(current_dd, 4))

        # ── All gates passed ──────────────────────────────────────
        # If any REDUCE happened, return REDUCE decision.
        if size < intent.suggested_size:
            logger.info(
                "governor_REDUCE", pair=pair,
                original=round(intent.suggested_size, 6),
                final=round(size, 6),
                risk_state=risk_state.value,
            )
            # Determine which gate caused the reduction for gate_failed.
            # Use the last REDUCE gate (highest number).
            gate = self._last_reduce_gate(
                net_exposure > _NET_EXPOSURE_LIMIT,
                phi < 1.0,
                current_dd > _DD_REDUCE_LIMIT,
            )
            return RiskDecision(
                decision=Decision.REDUCE,
                final_size=size,
                reason="size_reduced_by_risk_gates",
                risk_state=risk_state,
                gate_failed=gate,
            )

        logger.info(
            "governor_APPROVE", pair=pair,
            final_size=round(size, 6),
            risk_state=risk_state.value,
        )
        return RiskDecision(
            decision=Decision.APPROVE,
            final_size=size,
            reason="all_gates_passed",
            risk_state=risk_state,
        )

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _net_usd_exposure(
        pair: str,
        direction: Direction,
        open_positions: list[dict[str, Any]] | None,
    ) -> float:
        """Compute net directional USD exposure as a count-based fraction.

        Counts how many open positions share the same directional bias
        on USD (long USD vs short USD).  Returns the fraction that the
        dominant direction represents.

        This is a simplified proxy — the full version would use notional
        values.  For the gate, a count ratio suffices.
        """
        if not open_positions:
            return 0.0

        usd_long = 0
        usd_short = 0

        for pos in open_positions:
            p = pos.get("pair", "")
            d = pos.get("direction", "")
            if len(p) < 6:
                continue
            base, quote = p[:3], p[3:]

            if base == "USD":
                if d == "LONG":
                    usd_long += 1
                elif d == "SHORT":
                    usd_short += 1
            elif quote == "USD":
                if d == "LONG":
                    usd_short += 1   # buying EUR/selling USD
                elif d == "SHORT":
                    usd_long += 1    # selling EUR/buying USD

        total = len(open_positions)
        if total == 0:
            return 0.0

        dominant = max(usd_long, usd_short)
        return dominant / total

    @staticmethod
    def _build_weights(
        new_pair: str,
        new_size: float,
        open_positions: list[dict[str, Any]] | None,
    ) -> dict[str, float]:
        """Build position weight dict for VaR calculation."""
        weights: dict[str, float] = {}

        if open_positions:
            for pos in open_positions:
                p = pos.get("pair", "")
                s = pos.get("size", 0.0)
                if p:
                    weights[p] = weights.get(p, 0.0) + s

        weights[new_pair] = weights.get(new_pair, 0.0) + new_size
        return weights

    @staticmethod
    def _last_reduce_gate(
        gate4_reduced: bool,
        gate6_reduced: bool,
        gate7_reduced: bool,
    ) -> int:
        """Return the highest-numbered gate that caused a reduction."""
        if gate7_reduced:
            return 7
        if gate6_reduced:
            return 6
        return 4
