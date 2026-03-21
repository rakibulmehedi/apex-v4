"""Redis TTL state manager + async PostgreSQL WAL writer.

Architecture ref: APEX_V4_STRATEGY.md Section 5 (State Architecture).
Phase: P1.5.

Rule: Redis is always derived from PostgreSQL.
On restart, Redis is populated from PostgreSQL — never the reverse.

All connection details come from environment variables:
  - ``APEX_REDIS_URL``  (default ``redis://localhost:6379/0``)
  - ``APEX_DATABASE_URL`` (default ``postgresql://localhost:5432/apex_v4``)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import redis
import structlog
from sqlalchemy.orm import Session

from db.models import (
    FeatureVector as FeatureVectorRow,
    KillSwitchEvent as KillSwitchEventRow,
    TradeOutcome as TradeOutcomeRow,
    make_session_factory,
)
from src.market.schemas import FeatureVector

logger = structlog.get_logger(__name__)

# ── TTL constants (strategy spec) ────────────────────────────────────────

_FV_TTL = 300           # fv:{pair}        — 5 minutes
_POSITIONS_TTL = 60     # open_positions    — 1 minute
_NEWS_KEY_PREFIX = "news_blackout_"


# ═════════════════════════════════════════════════════════════════════════
# RedisStateManager
# ═════════════════════════════════════════════════════════════════════════

class RedisStateManager:
    """TTL-managed state cache backed by Redis.

    Parameters
    ----------
    client : redis.Redis | None
        Pre-built Redis client.  When *None*, a client is created from
        the ``APEX_REDIS_URL`` environment variable.
    """

    def __init__(self, client: redis.Redis | None = None) -> None:
        if client is not None:
            self._r = client
        else:
            url = os.environ.get("APEX_REDIS_URL", "redis://localhost:6379/0")
            self._r = redis.Redis.from_url(url, decode_responses=True)

    # ── feature vectors ──────────────────────────────────────────────

    def store_feature_vector(self, fv: FeatureVector) -> None:
        """Store a FeatureVector as JSON under ``fv:{pair}`` with TTL 300 s."""
        key = f"fv:{fv.pair}"
        self._r.set(key, fv.model_dump_json(), ex=_FV_TTL)

    def get_feature_vector(self, pair: str) -> FeatureVector | None:
        """Retrieve a cached FeatureVector, or *None* if expired / missing."""
        raw = self._r.get(f"fv:{pair}")
        if raw is None:
            return None
        return FeatureVector.model_validate_json(raw)

    # ── open positions ───────────────────────────────────────────────

    def store_open_positions(self, positions: list[dict[str, Any]]) -> None:
        """Store open-position list as JSON under ``open_positions``, TTL 60 s."""
        self._r.set("open_positions", json.dumps(positions), ex=_POSITIONS_TTL)

    def get_open_positions(self) -> list[dict[str, Any]]:
        """Retrieve open positions, or empty list if expired / missing."""
        raw = self._r.get("open_positions")
        if raw is None:
            return []
        return json.loads(raw)

    # ── kill switch ──────────────────────────────────────────────────

    def set_kill_switch(self, level: str) -> None:
        """Persist kill-switch level (no TTL — survives restarts)."""
        self._r.set("kill_switch", level)

    def get_kill_switch(self) -> str | None:
        """Return current kill-switch level, or *None* if not set."""
        return self._r.get("kill_switch")

    # ── news blackout ────────────────────────────────────────────────

    def set_news_blackout(
        self, pair: str, active: bool, duration_minutes: int = 30,
    ) -> None:
        """Set or clear a news-blackout flag for *pair*.

        When *active* is True the key is set with a TTL of
        *duration_minutes*.  When False the key is deleted.
        """
        key = f"{_NEWS_KEY_PREFIX}{pair}"
        if active:
            self._r.set(key, "1", ex=duration_minutes * 60)
        else:
            self._r.delete(key)


# ═════════════════════════════════════════════════════════════════════════
# PostgresWriter
# ═════════════════════════════════════════════════════════════════════════

class PostgresWriter:
    """Async, non-blocking PostgreSQL writer.

    Every public method is a coroutine that offloads the actual
    SQLAlchemy work to a thread via ``asyncio.to_thread`` so the
    event loop is never blocked.

    On any write error the exception is logged at CRITICAL level
    but **never** re-raised — the pipeline must not crash due to
    a database hiccup.

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

    # ── feature vector ───────────────────────────────────────────────

    async def write_feature_vector(self, fv: FeatureVector) -> None:
        """Insert a feature vector row (non-blocking)."""
        await asyncio.to_thread(self._sync_write_fv, fv)

    def _sync_write_fv(self, fv: FeatureVector) -> None:
        try:
            with self._sf() as session:  # type: Session
                row = FeatureVectorRow(
                    pair=fv.pair,
                    timestamp_ms=fv.timestamp,
                    atr_14=fv.atr_14,
                    adx_14=fv.adx_14,
                    ema_200=fv.ema_200,
                    bb_upper=fv.bb_upper,
                    bb_lower=fv.bb_lower,
                    bb_mid=fv.bb_mid,
                    session=fv.session.value,
                    spread_ok=fv.spread_ok,
                    news_blackout=fv.news_blackout,
                )
                session.add(row)
                session.commit()
        except Exception:
            logger.critical(
                "postgres write_feature_vector failed",
                pair=fv.pair,
                exc_info=True,
            )

    # ── trade outcome ────────────────────────────────────────────────

    async def write_trade_outcome(self, outcome: dict[str, Any]) -> None:
        """Insert a trade outcome row (non-blocking).

        *outcome* is a dict with keys matching the ``trade_outcomes``
        table columns.
        """
        await asyncio.to_thread(self._sync_write_outcome, outcome)

    def _sync_write_outcome(self, outcome: dict[str, Any]) -> None:
        try:
            with self._sf() as session:  # type: Session
                row = TradeOutcomeRow(
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
                session.add(row)
                session.commit()
        except Exception:
            logger.critical(
                "postgres write_trade_outcome failed",
                pair=outcome.get("pair"),
                exc_info=True,
            )

    # ── kill switch event ────────────────────────────────────────────

    async def write_kill_switch_event(
        self, level: str, reason: str,
    ) -> None:
        """Insert a kill-switch audit event (non-blocking)."""
        await asyncio.to_thread(self._sync_write_ks, level, reason)

    def _sync_write_ks(self, level: str, reason: str) -> None:
        try:
            now_ms = int(time.time() * 1000)
            with self._sf() as session:  # type: Session
                row = KillSwitchEventRow(
                    timestamp_ms=now_ms,
                    level=level,
                    previous_state="UNKNOWN",
                    new_state=level,
                    reason=reason,
                    broker_state_mismatch=False,
                )
                session.add(row)
                session.commit()
        except Exception:
            logger.critical(
                "postgres write_kill_switch_event failed",
                level=level,
                exc_info=True,
            )
