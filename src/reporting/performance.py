"""PerformanceReporter — pyfolio tearsheets from trade outcomes.

Phase 4 (P4.5).
Queries trade_outcomes from PostgreSQL, converts R-multiples to a daily
portfolio-return Series, and generates pyfolio/empyrical analytics.

Architecture ref: APEX_V4_STRATEGY.md Section 8, Phase 4.
Library ref: pyfolio-reloaded 0.9.x, empyrical-reloaded 0.5.x.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

# NumPy 2.0 removed np.NINF / np.PINF — empyrical-reloaded still uses them.
if not hasattr(np, "NINF"):
    np.NINF = -np.inf  # type: ignore[attr-defined]
if not hasattr(np, "PINF"):
    np.PINF = np.inf  # type: ignore[attr-defined]

import empyrical as ep  # noqa: E402  — must import after numpy compat shim
import matplotlib  # noqa: E402
import pandas as pd  # noqa: E402
import structlog  # noqa: E402
from sqlalchemy import and_  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from db.models import TradeOutcome, make_session_factory  # noqa: E402

matplotlib.use("Agg")  # non-interactive backend — no GUI required

logger = structlog.get_logger(__name__)

# Default risk fraction per trade (1% of portfolio equity).
# Converts R-multiples to portfolio return: return = R × risk_fraction.
_DEFAULT_RISK_FRACTION = 0.01

# Minimum trades required for meaningful statistics.
_MIN_TRADES_FOR_STATS = 5


class PerformanceReporter:
    """Generate performance analytics from trade outcomes.

    Parameters
    ----------
    session_factory
        SQLAlchemy sessionmaker.  When *None*, one is created from
        ``APEX_DATABASE_URL``.
    risk_fraction
        Fraction of portfolio risked per trade.  Used to convert
        R-multiples into portfolio returns.  Default 0.01 (1%).
    """

    def __init__(
        self,
        session_factory: Any = None,
        risk_fraction: float = _DEFAULT_RISK_FRACTION,
    ) -> None:
        if session_factory is not None:
            self._sf = session_factory
        else:
            self._sf = make_session_factory()
        self._risk_fraction = risk_fraction

    # ── query ─────────────────────────────────────────────────────────

    def _query_outcomes(
        self,
        *,
        strategy: str | None = None,
        regime: str | None = None,
        session: str | None = None,
        pair: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[TradeOutcome]:
        """Fetch trade outcomes with optional filters.

        Returns rows ordered by ``closed_at`` ascending.
        """
        filters = []
        if strategy is not None:
            filters.append(TradeOutcome.strategy == strategy)
        if regime is not None:
            filters.append(TradeOutcome.regime == regime)
        if session is not None:
            filters.append(TradeOutcome.session == session)
        if pair is not None:
            filters.append(TradeOutcome.pair == pair)
        if start is not None:
            filters.append(TradeOutcome.closed_at >= start)
        if end is not None:
            filters.append(TradeOutcome.closed_at <= end)

        try:
            with self._sf() as db:  # type: Session
                query = (
                    db.query(TradeOutcome)
                    .filter(and_(*filters) if filters else True)
                    .order_by(TradeOutcome.closed_at.asc())
                )
                return query.all()
        except Exception:
            logger.critical("query_outcomes_failed", exc_info=True)
            return []

    # ── conversion ────────────────────────────────────────────────────

    def _outcomes_to_returns(
        self, outcomes: list[TradeOutcome],
    ) -> pd.Series:
        """Convert trade outcomes to a daily portfolio-return Series.

        Each trade contributes ``r_multiple × risk_fraction`` to the
        day it closed.  Multiple trades on the same day are summed.
        Days with no trades are filled with 0.0 (no return).

        Returns a tz-naive DatetimeIndex Series suitable for empyrical/pyfolio.
        """
        if not outcomes:
            return pd.Series(dtype=float, name="returns")

        records = []
        for t in outcomes:
            close_date = t.closed_at.date()
            daily_return = t.r_multiple * self._risk_fraction
            records.append({"date": close_date, "return": daily_return})

        df = pd.DataFrame(records)
        daily = df.groupby("date")["return"].sum()

        # Fill business-day gaps with 0.0 (no trading = no return).
        idx = pd.bdate_range(start=daily.index.min(), end=daily.index.max())
        returns = daily.reindex(idx, fill_value=0.0)
        returns.index.name = None
        returns.name = "returns"
        return returns

    # ── metrics ───────────────────────────────────────────────────────

    def get_stats(self, **filters: Any) -> dict[str, Any] | None:
        """Compute key performance metrics.

        Accepts the same keyword filters as ``_query_outcomes``.

        Returns
        -------
        dict | None
            Dict of metrics, or None if fewer than ``_MIN_TRADES_FOR_STATS``
            trades exist in the filtered set.

        Keys: total_trades, winning_trades, losing_trades, win_rate,
              sharpe_ratio, sortino_ratio, max_drawdown, cagr,
              calmar_ratio, avg_r_multiple, total_return, annual_volatility,
              best_day, worst_day, profit_factor.
        """
        outcomes = self._query_outcomes(**filters)

        if len(outcomes) < _MIN_TRADES_FOR_STATS:
            logger.info(
                "insufficient_trades_for_stats",
                trade_count=len(outcomes),
                min_required=_MIN_TRADES_FOR_STATS,
            )
            return None

        returns = self._outcomes_to_returns(outcomes)
        if returns.empty:
            return None

        # Trade-level stats (from raw outcomes, not daily returns).
        r_multiples = np.array([t.r_multiple for t in outcomes])
        wins = int(np.sum(r_multiples > 0))
        losses = int(np.sum(r_multiples <= 0))
        gross_profit = float(np.sum(r_multiples[r_multiples > 0]))
        gross_loss = float(np.abs(np.sum(r_multiples[r_multiples <= 0])))
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )

        # empyrical portfolio-level stats.
        sharpe = ep.sharpe_ratio(returns)
        sortino = ep.sortino_ratio(returns)
        max_dd = ep.max_drawdown(returns)
        cagr = ep.cagr(returns, period=ep.DAILY)
        calmar = ep.calmar_ratio(returns, period=ep.DAILY)
        annual_vol = ep.annual_volatility(returns, period=ep.DAILY)
        total_return = float(ep.cum_returns_final(returns))

        stats = {
            "total_trades": len(outcomes),
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": wins / len(outcomes),
            "avg_r_multiple": float(np.mean(r_multiples)),
            "profit_factor": profit_factor,
            "sharpe_ratio": float(sharpe),
            "sortino_ratio": float(sortino),
            "max_drawdown": float(max_dd),
            "cagr": float(cagr),
            "calmar_ratio": float(calmar) if np.isfinite(calmar) else None,
            "annual_volatility": float(annual_vol),
            "total_return": total_return,
            "best_day": float(returns.max()),
            "worst_day": float(returns.min()),
        }

        logger.info(
            "performance_stats_computed",
            total_trades=stats["total_trades"],
            sharpe=round(stats["sharpe_ratio"], 4),
            max_drawdown=round(stats["max_drawdown"], 4),
            win_rate=round(stats["win_rate"], 4),
        )

        return stats

    # ── monthly returns ───────────────────────────────────────────────

    def get_monthly_returns(self, **filters: Any) -> pd.DataFrame | None:
        """Return a year × month DataFrame of aggregate returns.

        Returns None if insufficient data.
        """
        outcomes = self._query_outcomes(**filters)
        if len(outcomes) < _MIN_TRADES_FOR_STATS:
            return None

        returns = self._outcomes_to_returns(outcomes)
        if returns.empty:
            return None

        monthly = ep.aggregate_returns(returns, "monthly")
        # Pivot into year × month table.
        table = monthly.unstack().round(6)
        table.index.name = "Year"
        table.columns = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ][: table.shape[1]]
        return table

    # ── rolling sharpe ────────────────────────────────────────────────

    def get_rolling_sharpe(
        self, window: int = 63, **filters: Any,
    ) -> pd.Series | None:
        """Return rolling Sharpe ratio (default 63 business days ≈ 3 months).

        Returns None if insufficient data.
        """
        outcomes = self._query_outcomes(**filters)
        if len(outcomes) < _MIN_TRADES_FOR_STATS:
            return None

        returns = self._outcomes_to_returns(outcomes)
        if len(returns) < window:
            return None

        rolling = ep.roll_sharpe_ratio(returns, window=window)
        rolling.name = "rolling_sharpe"
        return rolling

    # ── tearsheet ─────────────────────────────────────────────────────

    def generate_tearsheet(
        self,
        output_dir: str | Path = "reports",
        filename: str = "tearsheet.png",
        **filters: Any,
    ) -> Path | None:
        """Generate a pyfolio returns tearsheet and save to disk.

        Parameters
        ----------
        output_dir
            Directory to write the image to (created if missing).
        filename
            Output filename (PNG).
        **filters
            Passed through to ``_query_outcomes``.

        Returns
        -------
        Path | None
            Path to the saved image, or None on failure / insufficient data.
        """
        import pyfolio as pf

        outcomes = self._query_outcomes(**filters)
        if len(outcomes) < _MIN_TRADES_FOR_STATS:
            logger.info(
                "insufficient_trades_for_tearsheet",
                trade_count=len(outcomes),
            )
            return None

        returns = self._outcomes_to_returns(outcomes)
        if returns.empty:
            return None

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        dest = out / filename

        try:
            import matplotlib.pyplot as plt

            fig = pf.create_returns_tear_sheet(returns, return_fig=True)
            fig.savefig(str(dest), dpi=150, bbox_inches="tight")
            plt.close(fig)

            logger.info("tearsheet_saved", path=str(dest))
            return dest

        except Exception:
            logger.critical("tearsheet_generation_failed", exc_info=True)
            return None

    # ── equity curve ──────────────────────────────────────────────────

    def get_equity_curve(self, **filters: Any) -> pd.Series | None:
        """Return cumulative returns (equity curve starting at 1.0).

        Returns None if insufficient data.
        """
        outcomes = self._query_outcomes(**filters)
        if len(outcomes) < _MIN_TRADES_FOR_STATS:
            return None

        returns = self._outcomes_to_returns(outcomes)
        if returns.empty:
            return None

        equity = ep.cum_returns(returns, starting_value=1.0)
        equity.name = "equity"
        return equity
