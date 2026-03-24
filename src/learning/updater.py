"""KellyInputUpdater — Segment stats recalculator + Redis cache updater.

Phase 4 (P4.4).
After each trade outcome is recorded:
  - Recalculate p_win, avg_R for the affected segment (rolling 90-day window)
  - Cache result in Redis key ``segment:{strategy}:{regime}:{session}``, TTL 3600s
  - Log warning if segment count drops below 30 (ADR-002)

Architecture ref: APEX_V4_STRATEGY.md Section 5 / Section 7
"""
from __future__ import annotations

import json
from typing import Any

import structlog

from src.calibration.history import PerformanceDatabase
from src.observability.metrics import WIN_RATE_7D

logger = structlog.get_logger(__name__)

# Redis key format and TTL for segment stats cache.
_SEGMENT_KEY_FMT = "segment:{strategy}:{regime}:{session}"
_SEGMENT_TTL = 3600  # 1 hour

# Minimum trades for a segment to be considered valid.
_MIN_SEGMENT_TRADES = 30


class KellyInputUpdater:
    """Recalculate and cache segment statistics after each trade.

    Parameters
    ----------
    perf_db
        PerformanceDatabase for segment queries.
    redis_client
        A ``redis.Redis`` instance for caching segment stats.
    """

    def __init__(
        self,
        perf_db: PerformanceDatabase,
        redis_client: Any,
    ) -> None:
        self._perf_db = perf_db
        self._redis = redis_client

    def update_segment(
        self,
        strategy: str,
        regime: str,
        session: str,
    ) -> dict[str, Any] | None:
        """Recalculate segment stats and update Redis cache.

        Parameters
        ----------
        strategy
            Strategy label (e.g. "MOMENTUM").
        regime
            Regime label (e.g. "TRENDING_UP").
        session
            Session label (e.g. "LONDON").

        Returns
        -------
        dict | None
            Updated stats dict on success, None if insufficient data.
        """
        stats = self._perf_db.get_segment_stats(strategy, regime, session)

        key = _SEGMENT_KEY_FMT.format(
            strategy=strategy, regime=regime, session=session,
        )

        if stats is None:
            # Segment has < 30 trades or DB error — clear cache.
            try:
                self._redis.delete(key)
            except Exception:
                logger.critical(
                    "updater_redis_delete_failed",
                    key=key,
                    exc_info=True,
                )
            logger.warning(
                "segment_below_minimum",
                strategy=strategy,
                regime=regime,
                session=session,
                min_required=_MIN_SEGMENT_TRADES,
            )
            return None

        # Cache the stats as JSON with TTL.
        cache_payload = {
            "win_rate": stats["win_rate"],
            "avg_R": stats["avg_R"],
            "trade_count": stats["trade_count"],
        }

        try:
            self._redis.set(key, json.dumps(cache_payload), ex=_SEGMENT_TTL)
        except Exception:
            logger.critical(
                "updater_redis_set_failed",
                key=key,
                exc_info=True,
            )

        logger.info(
            "segment_updated",
            strategy=strategy,
            regime=regime,
            session=session,
            win_rate=round(stats["win_rate"], 4),
            avg_R=round(stats["avg_R"], 4),
            trade_count=stats["trade_count"],
        )

        if stats["trade_count"] < _MIN_SEGMENT_TRADES:
            # Should not happen (get_segment_stats returns None below 30),
            # but guard defensively.
            logger.warning(
                "segment_below_minimum_unexpected",
                strategy=strategy,
                regime=regime,
                session=session,
                trade_count=stats["trade_count"],
            )

        # ── update 7-day win rate gauge ─────────────────────────
        win_rate_7d = self._perf_db.get_7d_win_rate()
        if win_rate_7d is not None:
            WIN_RATE_7D.set(win_rate_7d)

        return stats
