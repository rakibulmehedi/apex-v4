"""StateReconciler — 5-second heartbeat state reconciliation (ADR-004).

Phase 3 (P3.5).
Each cycle:
  1. Call mt5.positions_get() — ground truth
  2. Read open_positions from Redis
  3. Diff broker_tickets vs redis_tickets
  4. On mismatch:
     - log critical with full diff
     - write to reconciliation_log table
     - trigger kill_switch HARD ("state_drift")
     - reconcile Redis to match broker (broker always wins)
  5. Update Redis open_positions and last_reconcile_ts

On mt5.positions_get() returning None:
  - trigger kill_switch EMERGENCY ("broker_disconnect")

If reconciler loop throws:
  - trigger kill_switch EMERGENCY ("reconciler_failure")
  - never die silently

Architecture ref: APEX_V4_STRATEGY.md Section 5, ADR-004
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from src.market.mt5_client import MT5Client
from src.observability.metrics import STATE_DRIFT_TOTAL
from src.risk.kill_switch import KillSwitch

logger = structlog.get_logger(__name__)

# Heartbeat interval in seconds (from config/settings.yaml).
_HEARTBEAT_SECONDS = 5

# Redis key for last reconcile timestamp.
_LAST_RECONCILE_KEY = "last_reconcile_ts"

# Seconds of feed silence during an active session that triggers EMERGENCY.
_FEED_SILENCE_SECONDS = 300


class StateReconciler:
    """Async service that diffs MT5 broker state against Redis every 5 s.

    Parameters
    ----------
    mt5_client
        MT5Client for ``positions_get()``.
    redis_client
        Redis client for open_positions read/write.
    kill_switch
        KillSwitch instance to trigger on mismatch or failure.
    session_factory
        SQLAlchemy sessionmaker for reconciliation_log writes.
    heartbeat
        Interval in seconds between cycles (default 5).
    """

    def __init__(
        self,
        mt5_client: MT5Client,
        redis_client: Any,
        kill_switch: KillSwitch,
        session_factory: Any,
        heartbeat: float = _HEARTBEAT_SECONDS,
    ) -> None:
        self._mt5 = mt5_client
        self._redis = redis_client
        self._ks = kill_switch
        self._sf = session_factory
        self._heartbeat = heartbeat
        self._running = False
        # Monotonic timestamp of the last successful MarketFeed snapshot publish.
        # None means no snapshot has been received yet (skip silence check).
        self._last_snapshot_received_at: float | None = None

    # ── public API ─────────────────────────────────────────────────

    def update_last_snapshot_time(self) -> None:
        """Record that MarketFeed just published a snapshot successfully."""
        self._last_snapshot_received_at = time.monotonic()

    async def run(self) -> None:
        """Start the infinite reconciliation loop.

        Never returns under normal operation.  If the loop body throws,
        EMERGENCY is triggered and the loop continues (never die silently).
        """
        self._running = True
        logger.info("reconciler_started", heartbeat=self._heartbeat)

        while self._running:
            try:
                await self._cycle()
            except Exception:
                logger.critical("reconciler_failure", exc_info=True)
                await self._ks.trigger(
                    "EMERGENCY",
                    "reconciler_failure",
                )
            await asyncio.sleep(self._heartbeat)

    def stop(self) -> None:
        """Signal the loop to exit after the current cycle."""
        self._running = False

    # ── single cycle ───────────────────────────────────────────────

    async def _cycle(self) -> None:
        """Execute one reconciliation heartbeat."""
        now_ms = int(time.time() * 1000)

        # ── 0. feed silence check ──────────────────────────────────
        await self._check_feed_silence()

        # ── 1. broker truth ────────────────────────────────────────
        broker_positions = self._mt5.positions_get()

        if broker_positions is None:
            logger.critical(
                "reconciler_broker_disconnect",
                action="trigger EMERGENCY",
            )
            await self._ks.trigger("EMERGENCY", "broker_disconnect")
            return

        broker_tickets = {pos.ticket for pos in broker_positions}
        broker_snapshot = [
            {
                "ticket": pos.ticket,
                "pair": pos.symbol,
                "type": pos.type,
                "volume": pos.volume,
                "price_open": pos.price_open,
                "profit": pos.profit,
            }
            for pos in broker_positions
        ]

        # ── 2. Redis state ────────────────────────────────────────
        redis_raw = self._redis.get("open_positions")
        redis_positions: list[dict[str, Any]] = json.loads(redis_raw) if redis_raw else []
        redis_tickets = {pos.get("ticket") for pos in redis_positions if pos.get("ticket") is not None}

        # ── 3. diff ───────────────────────────────────────────────
        phantom_tickets = redis_tickets - broker_tickets  # in Redis, not broker
        ghost_tickets = broker_tickets - redis_tickets  # in broker, not Redis

        mismatch = bool(phantom_tickets or ghost_tickets)

        if mismatch:
            diff = {
                "phantom": sorted(phantom_tickets),
                "ghost": sorted(ghost_tickets),
            }

            STATE_DRIFT_TOTAL.inc()
            logger.critical(
                "reconciler_state_drift",
                phantom=diff["phantom"],
                ghost=diff["ghost"],
                broker_count=len(broker_tickets),
                redis_count=len(redis_tickets),
            )

            # ── 4a. write to reconciliation_log ────────────────────
            await asyncio.to_thread(
                self._write_reconciliation_log,
                now_ms,
                redis_positions,
                broker_snapshot,
                diff,
            )

            # ── 4b. trigger HARD ───────────────────────────────────
            await self._ks.trigger("HARD", "state_drift")

            # ── 4c. reconcile Redis to match broker ────────────────
            self._redis.set(
                "open_positions",
                json.dumps(broker_snapshot),
                ex=60,
            )
            logger.info("reconciler_redis_reconciled", source="broker")
        else:
            # ── 5. no mismatch — update Redis with fresh broker data
            self._redis.set(
                "open_positions",
                json.dumps(broker_snapshot),
                ex=60,
            )

        # ── update last_reconcile_ts ───────────────────────────────
        self._redis.set(_LAST_RECONCILE_KEY, str(now_ms), ex=30)

        if not mismatch:
            logger.debug(
                "reconciler_ok",
                broker_count=len(broker_tickets),
                timestamp_ms=now_ms,
            )

    # ── feed silence detection ─────────────────────────────────────

    async def _check_feed_silence(self) -> None:
        """Trigger EMERGENCY if no snapshot received during an active session."""
        if self._last_snapshot_received_at is None:
            return

        utc_hour = datetime.now(timezone.utc).hour
        if not self._is_active_session(utc_hour):
            return

        silence_seconds = time.monotonic() - self._last_snapshot_received_at
        if silence_seconds > _FEED_SILENCE_SECONDS:
            logger.critical(
                "mt5_feed_silence",
                silence_seconds=int(silence_seconds),
            )
            await self._ks.trigger("EMERGENCY", "mt5_feed_silence")

    @staticmethod
    def _is_active_session(utc_hour: int) -> bool:
        """Return True if utc_hour falls within LONDON, OVERLAP, or NY session.

        LONDON  07-12, OVERLAP  12-16, NY  16-21 → combined: 07 ≤ hour < 21.
        """
        return 7 <= utc_hour < 21

    # ── DB write helper ────────────────────────────────────────────

    def _write_reconciliation_log(
        self,
        timestamp_ms: int,
        redis_positions: list[dict[str, Any]],
        broker_snapshot: list[dict[str, Any]],
        diff: dict[str, list],
    ) -> None:
        """Insert a reconciliation_log row (sync, called via to_thread)."""
        from db.models import ReconciliationLog

        try:
            with self._sf() as db:
                row = ReconciliationLog(
                    timestamp_ms=timestamp_ms,
                    redis_positions=json.dumps(redis_positions),
                    mt5_positions=json.dumps(broker_snapshot),
                    mismatch_detected=True,
                    positions_diverged=json.dumps(diff),
                    action_taken="HARD",
                )
                db.add(row)
                db.commit()
        except Exception:
            logger.critical(
                "reconciler_db_write_failed",
                exc_info=True,
            )
