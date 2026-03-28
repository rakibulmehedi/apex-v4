"""TradeOutcomeRecorder — Persist trade outcomes to PostgreSQL.

Phase 4 (P4.3).
Records: R-multiple, segment attribution (strategy x regime x session).
Writes to PostgreSQL trade_outcomes table via PerformanceDatabase.

Architecture ref: APEX_V4_STRATEGY.md Section 5, Phase 4 (P4.3)
"""

from __future__ import annotations

from typing import Any

import structlog

from src.calibration.history import PerformanceDatabase

logger = structlog.get_logger(__name__)


class TradeOutcomeRecorder:
    """Write trade outcomes to PostgreSQL.

    Parameters
    ----------
    perf_db
        PerformanceDatabase instance for segment inserts.
    """

    def __init__(self, perf_db: PerformanceDatabase) -> None:
        self._perf_db = perf_db

    def record(self, outcome: dict[str, Any]) -> bool:
        """Insert a trade outcome into ``trade_outcomes``.

        Parameters
        ----------
        outcome
            Dict with keys: pair, strategy, regime, session, direction,
            entry_price, exit_price, r_multiple, won, fill_id,
            opened_at, closed_at.

        Returns
        -------
        bool
            True on success, False on failure.
        """
        try:
            self._perf_db.update_segment(outcome)

            logger.info(
                "outcome_recorded",
                pair=outcome.get("pair"),
                strategy=outcome.get("strategy"),
                regime=outcome.get("regime"),
                session=outcome.get("session"),
                r_multiple=outcome.get("r_multiple"),
                won=outcome.get("won"),
            )
            return True

        except Exception:
            logger.critical(
                "outcome_record_failed",
                pair=outcome.get("pair"),
                exc_info=True,
            )
            return False
