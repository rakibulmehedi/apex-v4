"""PerformanceDatabase — PostgreSQL segment lookup for calibration.

Phase 3 (P3.1).
Segments keyed by (strategy × regime × session).
Minimum 30-trade gate before segment goes live (ADR-002).
Rolling 90-day window.

Architecture ref: APEX_V4_STRATEGY.md Section 5 / Section 7.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from db.models import TradeOutcome, make_session_factory

logger = structlog.get_logger(__name__)

# Minimum trades required before a segment is considered valid (ADR-002).
_MIN_SEGMENT_TRADES = 30

# Rolling window for segment statistics.
_LOOKBACK_DAYS = 90


class PerformanceDatabase:
    """Query and update trade-outcome segments in PostgreSQL.

    Parameters
    ----------
    session_factory
        A ``sqlalchemy.orm.sessionmaker`` instance.  When *None*,
        one is created using ``APEX_DATABASE_URL``.
    """

    def __init__(self, session_factory: Any = None) -> None:
        if session_factory is not None:
            self._sf = session_factory
        else:
            self._sf = make_session_factory()

    # ── query ─────────────────────────────────────────────────────────

    def get_segment_stats(
        self,
        strategy: str,
        regime: str,
        session: str,
    ) -> dict[str, Any] | None:
        """Return win-rate stats for a (strategy, regime, session) segment.

        Queries ``trade_outcomes`` for the last 90 days.
        Returns *None* if fewer than 30 trades exist (ADR-002).

        Returns
        -------
        dict with keys: win_rate, avg_R, trade_count, last_updated
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)

        try:
            with self._sf() as db:  # type: Session
                rows = (
                    db.query(
                        func.count(TradeOutcome.id).label("trade_count"),
                        func.avg(
                            case(
                                (TradeOutcome.won == True, 1),  # noqa: E712
                                else_=0,
                            )
                        ).label("win_rate"),
                        func.avg(TradeOutcome.r_multiple).label("avg_r"),
                        func.max(TradeOutcome.closed_at).label("last_updated"),
                    )
                    .filter(
                        TradeOutcome.strategy == strategy,
                        TradeOutcome.regime == regime,
                        TradeOutcome.session == session,
                        TradeOutcome.closed_at >= cutoff,
                    )
                    .one()
                )

                trade_count = rows.trade_count or 0

                if trade_count < _MIN_SEGMENT_TRADES:
                    logger.info(
                        "segment_insufficient",
                        strategy=strategy,
                        regime=regime,
                        session=session,
                        trade_count=trade_count,
                        min_required=_MIN_SEGMENT_TRADES,
                    )
                    return None

                return {
                    "win_rate": float(rows.win_rate),
                    "avg_R": float(rows.avg_r),
                    "trade_count": trade_count,
                    "last_updated": rows.last_updated,
                }

        except Exception:
            logger.critical(
                "get_segment_stats failed",
                strategy=strategy,
                regime=regime,
                session=session,
                exc_info=True,
            )
            return None

    # ── 7-day win rate (for observability gauge) ──────────────────────

    def get_7d_win_rate(self) -> float | None:
        """Return win rate across all segments over the last 7 days.

        Returns
        -------
        float | None
            Win rate as a fraction (0.0–1.0), or None if no trades in window.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)

        try:
            with self._sf() as db:  # type: Session
                row = (
                    db.query(
                        func.count(TradeOutcome.id).label("total"),
                        func.sum(
                            case(
                                (TradeOutcome.won == True, 1),  # noqa: E712
                                else_=0,
                            )
                        ).label("wins"),
                    )
                    .filter(TradeOutcome.closed_at >= cutoff)
                    .one()
                )

                total = row.total or 0
                if total == 0:
                    return None

                wins = row.wins or 0
                return wins / total

        except Exception:
            logger.critical("get_7d_win_rate_failed", exc_info=True)
            return None

    # ── insert ────────────────────────────────────────────────────────

    def update_segment(self, outcome: dict[str, Any]) -> None:
        """Insert a single trade outcome into ``trade_outcomes``."""
        try:
            with self._sf() as db:  # type: Session
                row = TradeOutcome(
                    pair=outcome["pair"],
                    strategy=outcome["strategy"],
                    regime=outcome["regime"],
                    session=outcome["session"],
                    direction=outcome["direction"],
                    entry_price=outcome["entry_price"],
                    exit_price=outcome["exit_price"],
                    r_multiple=outcome["r_multiple"],
                    won=outcome["won"],
                    fill_id=outcome.get("fill_id"),
                    opened_at=outcome["opened_at"],
                    closed_at=outcome["closed_at"],
                )
                db.add(row)
                db.commit()
                logger.info(
                    "trade_outcome_inserted",
                    pair=outcome["pair"],
                    strategy=outcome["strategy"],
                    regime=outcome["regime"],
                    session=outcome["session"],
                )
        except Exception:
            logger.critical(
                "update_segment failed",
                pair=outcome.get("pair"),
                exc_info=True,
            )

    # ── bootstrap ─────────────────────────────────────────────────────

    def bootstrap_from_v3(self, v3_data: list[dict[str, Any]]) -> int:
        """Bulk-import V3 historical trades into ``trade_outcomes``.

        Each dict in *v3_data* must contain the standard outcome keys.
        A ``mode`` field of ``"v3_historical"`` is logged but not stored
        in the table (the table schema has no mode column — provenance
        is tracked by the absence of a ``fill_id``).

        Returns
        -------
        int
            Number of rows successfully inserted.
        """
        inserted = 0
        try:
            with self._sf() as db:  # type: Session
                for record in v3_data:
                    row = TradeOutcome(
                        pair=record["pair"],
                        strategy=record["strategy"],
                        regime=record["regime"],
                        session=record["session"],
                        direction=record["direction"],
                        entry_price=record["entry_price"],
                        exit_price=record["exit_price"],
                        r_multiple=record["r_multiple"],
                        won=record["won"],
                        fill_id=None,  # V3 trades have no fill tracking
                        opened_at=record["opened_at"],
                        closed_at=record["closed_at"],
                    )
                    db.add(row)
                    inserted += 1
                db.commit()
                logger.info(
                    "v3_bootstrap_complete",
                    rows_inserted=inserted,
                    mode="v3_historical",
                )
        except Exception:
            logger.critical(
                "bootstrap_from_v3 failed",
                rows_attempted=len(v3_data),
                rows_inserted=inserted,
                exc_info=True,
            )
            inserted = 0
        return inserted
