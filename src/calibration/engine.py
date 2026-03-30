"""CalibrationEngine — edge calculation + position sizing.

Phase 3 (P3.2).
Exact Section 7.1 formulas — no deviation:

  f*        = (p_win × avg_R − (1 − p_win)) / avg_R
  f_quarter = f* × 0.25
  f_final   = min(f_quarter, 0.02)

  dd_scalar:
    current_dd < 0.02  → 1.0
    current_dd < 0.05  → 0.5
    current_dd ≥ 0.05  → None (no trade)

  correlation_scalar:
    same_currency_count ≥ 2 → 0.5
    otherwise              → 1.0

  final_size = f_final × dd_scalar × correlation_scalar

Output: CalibratedTradeIntent | None
Architecture ref: APEX_V4_STRATEGY.md Section 7.1
"""

from __future__ import annotations

from typing import Any

import structlog

from src.calibration.history import PerformanceDatabase
from src.market.schemas import (
    AlphaHypothesis,
    CalibratedTradeIntent,
)

logger = structlog.get_logger(__name__)


class CalibrationEngine:
    """Compute edge and position size from historical segment data.

    Parameters
    ----------
    perf_db
        PerformanceDatabase for segment lookups.
    min_live_trades_for_edge
        Minimum live trades (fill_id IS NOT NULL) before trusting edge
        calculation.  Below this threshold and with *bootstrap_mode_fallback*
        enabled, the engine returns minimum position size instead of rejecting.
    bootstrap_mode_fallback
        When True and live trade count < *min_live_trades_for_edge*, return
        the minimum position size (0.001 equity fraction → 0.01 lots via
        gateway clamp) instead of rejecting on negative bootstrap edge.
    """

    # Minimum suggested_size during bootstrap fallback.  The execution
    # gateway clamps volume to 0.01 lots, so this fraction guarantees
    # the smallest tradeable position for any practical equity level.
    _BOOTSTRAP_MIN_SIZE: float = 0.001

    def __init__(
        self,
        perf_db: PerformanceDatabase,
        capital_allocation_pct: float = 1.0,
        min_live_trades_for_edge: int = 10,
        bootstrap_mode_fallback: bool = True,
    ) -> None:
        self._perf_db = perf_db
        self._capital_allocation_pct = capital_allocation_pct
        self._min_live_trades = min_live_trades_for_edge
        self._bootstrap_fallback = bootstrap_mode_fallback

    # ── public API ─────────────────────────────────────────────────

    def calibrate(
        self,
        hypothesis: AlphaHypothesis,
        session_label: str,
        current_dd: float,
        open_positions: list[dict[str, Any]] | None = None,
    ) -> CalibratedTradeIntent | None:
        """Size a trade using Kelly criterion + scalars.

        Parameters
        ----------
        hypothesis
            AlphaHypothesis from an alpha engine.
        session_label
            Trading session (from FeatureVector.session).
        current_dd
            Current portfolio drawdown as a fraction (0.0 = no drawdown,
            0.05 = 5% drawdown).
        open_positions
            List of open position dicts, each with a ``"pair"`` key
            (e.g. ``"EURUSD"``).  Used for correlation scaling.

        Returns
        -------
        CalibratedTradeIntent | None
            None when the trade should be rejected (logged with reason).
        """
        pair = hypothesis.pair
        strategy = hypothesis.strategy.value
        regime = hypothesis.regime.value

        # ── 1. drawdown gate (before DB hit) ───────────────────────
        dd_scalar = self._dd_scalar(current_dd)
        if dd_scalar is None:
            logger.warning(
                "calibration_rejected",
                reason="drawdown >= 5%",
                pair=pair,
                current_dd=current_dd,
            )
            return None

        # ── 2. segment lookup ──────────────────────────────────────
        stats = self._perf_db.get_segment_stats(strategy, regime, session_label)
        if stats is None:
            logger.warning(
                "calibration_rejected",
                reason="no segment data (< 30 trades or missing)",
                pair=pair,
                strategy=strategy,
                regime=regime,
                session=session_label,
            )
            return None

        p_win: float = stats["win_rate"]
        avg_r: float = stats["avg_R"]
        trade_count: int = stats["trade_count"]

        # ── 3. edge calculation ────────────────────────────────────
        edge = p_win * avg_r - (1.0 - p_win)

        # ── 3a. live-data-first gate ─────────────────────────────
        live_count = self._perf_db.get_live_trade_count(
            strategy, regime, session_label,
        )

        if live_count < self._min_live_trades and self._bootstrap_fallback:
            # Insufficient live data — use minimum size, do NOT reject
            # on bootstrap edge.  This lets the system build real data.
            logger.info(
                "insufficient_live_data",
                reason="using minimum size",
                pair=pair,
                strategy=strategy,
                regime=regime,
                session=session_label,
                live_count=live_count,
                min_required=self._min_live_trades,
                bootstrap_edge=round(edge, 4),
                bootstrap_p_win=round(p_win, 4),
                suggested_size=self._BOOTSTRAP_MIN_SIZE,
            )
            return CalibratedTradeIntent(
                p_win=p_win,
                expected_R=avg_r,
                edge=edge,
                suggested_size=self._BOOTSTRAP_MIN_SIZE,
                segment_count=trade_count,
            )

        if edge <= 0:
            logger.warning(
                "calibration_rejected",
                reason="edge <= 0",
                pair=pair,
                p_win=p_win,
                avg_r=avg_r,
                edge=edge,
                live_count=live_count,
            )
            return None

        # ── 4. Kelly criterion (Section 7.1) ──────────────────────
        f_star = edge / avg_r
        f_quarter = f_star * 0.25
        f_final = min(f_quarter, 0.02)

        # ── 5. correlation scalar ─────────────────────────────────
        corr_scalar = self._correlation_scalar(pair, open_positions)

        # ── 6. final size ─────────────────────────────────────────
        final_size = f_final * dd_scalar * corr_scalar * self._capital_allocation_pct

        logger.info(
            "calibration_complete",
            pair=pair,
            strategy=strategy,
            regime=regime,
            session=session_label,
            p_win=round(p_win, 4),
            avg_r=round(avg_r, 4),
            edge=round(edge, 4),
            f_star=round(f_star, 6),
            f_quarter=round(f_quarter, 6),
            f_final=round(f_final, 6),
            dd_scalar=dd_scalar,
            corr_scalar=corr_scalar,
            capital_allocation_pct=self._capital_allocation_pct,
            final_size=round(final_size, 6),
            trade_count=trade_count,
        )

        return CalibratedTradeIntent(
            p_win=p_win,
            expected_R=avg_r,
            edge=edge,
            suggested_size=final_size,
            segment_count=trade_count,
        )

    # ── private helpers ────────────────────────────────────────────

    @staticmethod
    def _dd_scalar(current_dd: float) -> float | None:
        """Drawdown scalar per Section 7.1.

        Returns None when drawdown >= 5% (no new trades allowed).
        """
        if current_dd < 0.02:
            return 1.0
        if current_dd < 0.05:
            return 0.5
        return None

    @staticmethod
    def _correlation_scalar(
        pair: str,
        open_positions: list[dict[str, Any]] | None,
    ) -> float:
        """Halve size when 2+ open positions share a currency with *pair*."""
        if not open_positions:
            return 1.0

        base = pair[:3]
        quote = pair[3:]
        same_currency_count = 0

        for pos in open_positions:
            other = pos.get("pair", "")
            if len(other) < 6:
                continue
            other_base = other[:3]
            other_quote = other[3:]
            if base in (other_base, other_quote) or quote in (other_base, other_quote):
                same_currency_count += 1

        if same_currency_count >= 2:
            return 0.5
        return 1.0
