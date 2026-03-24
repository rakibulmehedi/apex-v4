"""KillSwitch — Three-level emergency circuit breaker (ADR-005).

Phase 3 (P3.4).
Levels:
  SOFT      → no new signals allowed, log warning
  HARD      → flatten all open positions via MT5, log critical
  EMERGENCY → disconnect MT5, write state to disk, fire alert, log critical

Rules:
  - State managed with asyncio.Lock — not a plain boolean
  - Only escalate — never auto-de-escalate (HARD → SOFT is forbidden)
  - Persist every state change to Redis AND PostgreSQL immediately
  - On startup: read state from PostgreSQL (survives process restart)
  - Manual reset requires exact string: "I CONFIRM SYSTEM IS SAFE"

Architecture ref: APEX_V4_STRATEGY.md Section 4, ADR-005
"""
from __future__ import annotations

import asyncio
import json
import time
from enum import IntEnum
from pathlib import Path
from typing import Any

import structlog

from src.market.mt5_client import MT5Client

logger = structlog.get_logger(__name__)

# ── confirmation string for manual reset ───────────────────────────────

_RESET_CONFIRMATION = "I CONFIRM SYSTEM IS SAFE"

# ── state dump path ────────────────────────────────────────────────────

_EMERGENCY_DUMP_DIR = Path("data/emergency")


class KillLevel(IntEnum):
    """Kill switch severity.  Higher value = more severe.

    IntEnum ensures SOFT < HARD < EMERGENCY, so escalation is a simple ``>``
    comparison.  This prevents accidental de-escalation.
    """

    NONE = 0
    SOFT = 1
    HARD = 2
    EMERGENCY = 3


# Map string labels to levels and back.
_LABEL_TO_LEVEL = {
    "SOFT": KillLevel.SOFT,
    "HARD": KillLevel.HARD,
    "EMERGENCY": KillLevel.EMERGENCY,
}

_LEVEL_TO_LABEL = {v: k for k, v in _LABEL_TO_LEVEL.items()}


class KillSwitch:
    """Three-level kill switch with asyncio-safe state management.

    Parameters
    ----------
    redis_client
        A ``redis.Redis`` instance for fast state cache.
    session_factory
        SQLAlchemy sessionmaker for durable audit trail.
    mt5_client
        MT5Client for position flattening (HARD) and disconnect (EMERGENCY).
    alert_callback
        Optional async callable fired on EMERGENCY (e.g. send Slack/email).
    dump_dir
        Directory for EMERGENCY state dump (default ``data/emergency/``).
    """

    def __init__(
        self,
        redis_client: Any,
        session_factory: Any,
        mt5_client: MT5Client | None = None,
        alert_callback: Any = None,
        dump_dir: Path = _EMERGENCY_DUMP_DIR,
    ) -> None:
        self._redis = redis_client
        self._sf = session_factory
        self._mt5 = mt5_client
        self._alert_cb = alert_callback
        self._dump_dir = dump_dir

        self._level: KillLevel = KillLevel.NONE
        self._lock = asyncio.Lock()

    # ── properties ─────────────────────────────────────────────────

    @property
    def level(self) -> KillLevel:
        return self._level

    @property
    def label(self) -> str | None:
        """Current level as a string, or None if NONE."""
        return _LEVEL_TO_LABEL.get(self._level)

    @property
    def is_active(self) -> bool:
        """True if any kill level is engaged."""
        return self._level > KillLevel.NONE

    def allows_new_signals(self) -> bool:
        """True only when no kill level is active."""
        return self._level == KillLevel.NONE

    # ── startup recovery ───────────────────────────────────────────

    async def recover_from_db(self) -> None:
        """Read latest kill switch state from PostgreSQL on startup.

        Must be called before the first trading loop iteration.
        If a SOFT/HARD/EMERGENCY state exists in the DB, it is restored
        without re-executing side effects (positions already flat, etc.).
        """
        async with self._lock:
            label = await asyncio.to_thread(self._read_latest_state_from_db)
            if label is not None and label in _LABEL_TO_LEVEL:
                self._level = _LABEL_TO_LEVEL[label]
                # Mirror to Redis for fast reads by other processes.
                self._redis.set("kill_switch", label)
                logger.critical(
                    "kill_switch_recovered",
                    level=label,
                    source="postgresql",
                )
            else:
                self._level = KillLevel.NONE
                logger.info("kill_switch_clear_on_startup")

    def _read_latest_state_from_db(self) -> str | None:
        """Sync helper — query the most recent kill_switch_events row."""
        from db.models import KillSwitchEvent

        try:
            with self._sf() as db:
                row = (
                    db.query(KillSwitchEvent)
                    .order_by(KillSwitchEvent.timestamp_ms.desc())
                    .first()
                )
                if row is None:
                    return None
                return str(row.new_state)
        except Exception:
            logger.critical("kill_switch_db_read_failed", exc_info=True)
            return None

    # ── trigger ────────────────────────────────────────────────────

    async def trigger(self, level_label: str, reason: str) -> bool:
        """Escalate (or set) the kill switch.

        Parameters
        ----------
        level_label
            One of ``"SOFT"``, ``"HARD"``, ``"EMERGENCY"``.
        reason
            Human-readable explanation for the audit trail.

        Returns
        -------
        bool
            True if the state changed, False if already at or above
            the requested level.
        """
        requested = _LABEL_TO_LEVEL.get(level_label)
        if requested is None:
            raise ValueError(f"Invalid kill level: {level_label!r}")

        async with self._lock:
            if requested <= self._level:
                logger.info(
                    "kill_switch_already_at_or_above",
                    current=self.label,
                    requested=level_label,
                )
                return False

            previous = self.label or "NONE"
            self._level = requested

            # ── persist to Redis + PostgreSQL ─────────────────────
            self._persist_to_redis(level_label)
            await asyncio.to_thread(
                self._persist_to_db, previous, level_label, reason,
            )

            # ── level-specific actions ────────────────────────────
            if requested == KillLevel.SOFT:
                logger.warning(
                    "kill_switch_SOFT",
                    reason=reason,
                    action="no new signals",
                )

            elif requested == KillLevel.HARD:
                logger.critical(
                    "kill_switch_HARD",
                    reason=reason,
                    action="flatten all positions",
                )
                await self._flatten_positions()

            elif requested == KillLevel.EMERGENCY:
                logger.critical(
                    "kill_switch_EMERGENCY",
                    reason=reason,
                    action="disconnect MT5, dump state, fire alert",
                )
                await self._emergency_shutdown(reason)

            return True

    # ── manual reset ───────────────────────────────────────────────

    async def manual_reset(self, confirmation: str, operator: str = "unknown") -> None:
        """Reset the kill switch to NONE.

        Parameters
        ----------
        confirmation
            Must be exactly ``"I CONFIRM SYSTEM IS SAFE"``.
        operator
            Name/ID of the person performing the reset (audit trail).

        Raises
        ------
        PermissionError
            If the confirmation string does not match.
        """
        if confirmation != _RESET_CONFIRMATION:
            raise PermissionError(
                f"Invalid confirmation string. "
                f"Expected: {_RESET_CONFIRMATION!r}"
            )

        async with self._lock:
            previous = self.label or "NONE"
            self._level = KillLevel.NONE

            reason = f"Manual reset by {operator}"
            self._persist_to_redis_clear()
            await asyncio.to_thread(
                self._persist_to_db, previous, "NONE", reason,
            )

            logger.warning(
                "kill_switch_RESET",
                previous=previous,
                operator=operator,
            )

    # ── persistence helpers ────────────────────────────────────────

    def _persist_to_redis(self, level_label: str) -> None:
        """Set kill_switch key in Redis (no TTL)."""
        try:
            self._redis.set("kill_switch", level_label)
        except Exception:
            logger.critical("kill_switch_redis_persist_failed", exc_info=True)

    def _persist_to_redis_clear(self) -> None:
        """Delete kill_switch key from Redis on reset."""
        try:
            self._redis.delete("kill_switch")
        except Exception:
            logger.critical("kill_switch_redis_clear_failed", exc_info=True)

    def _persist_to_db(
        self, previous: str, new: str, reason: str,
    ) -> None:
        """Insert a row into kill_switch_events (sync, called via to_thread)."""
        from db.models import KillSwitchEvent

        try:
            now_ms = int(time.time() * 1000)
            with self._sf() as db:
                row = KillSwitchEvent(
                    timestamp_ms=now_ms,
                    level=new if new != "NONE" else "SOFT",  # DB enum has no NONE
                    previous_state=previous,
                    new_state=new,
                    reason=reason,
                    broker_state_mismatch=False,
                )
                db.add(row)
                db.commit()
        except Exception:
            logger.critical(
                "kill_switch_db_persist_failed",
                previous=previous,
                new=new,
                exc_info=True,
            )

    # ── side-effect actions ────────────────────────────────────────

    async def _flatten_positions(self) -> None:
        """Close all open positions via MT5 (HARD action)."""
        if self._mt5 is None:
            logger.warning("kill_switch_flatten_skipped", reason="no MT5 client")
            return

        try:
            positions = self._mt5.positions_get()
            if not positions:
                logger.info("kill_switch_flatten_no_positions")
                return

            for pos in positions:
                # Build a close request for each position.
                close_request = {
                    "action": 1,  # TRADE_ACTION_DEAL
                    "position": pos.ticket,
                    "symbol": pos.symbol,
                    "volume": pos.volume,
                    "type": 1 if pos.type == 0 else 0,  # reverse direction
                }
                result = self._mt5.order_send(close_request)
                if result is not None:
                    logger.info(
                        "kill_switch_position_closed",
                        ticket=pos.ticket,
                        symbol=pos.symbol,
                    )
                else:
                    logger.critical(
                        "kill_switch_flatten_failed",
                        ticket=pos.ticket,
                        symbol=pos.symbol,
                    )
        except Exception:
            logger.critical("kill_switch_flatten_error", exc_info=True)

    async def _emergency_shutdown(self, reason: str) -> None:
        """EMERGENCY: disconnect MT5, dump state, fire alert."""
        # 1. Disconnect MT5.
        if self._mt5 is not None:
            try:
                self._mt5.shutdown()
                logger.critical("kill_switch_mt5_disconnected")
            except Exception:
                logger.critical("kill_switch_mt5_disconnect_failed", exc_info=True)

        # 2. Dump full state to disk as JSON.
        await self._dump_state_to_disk(reason)

        # 3. Fire alert callback.
        if self._alert_cb is not None:
            try:
                await self._alert_cb(reason)
            except Exception:
                logger.critical("kill_switch_alert_failed", exc_info=True)

    async def _dump_state_to_disk(self, reason: str) -> None:
        """Write a JSON state dump for post-incident analysis."""
        try:
            self._dump_dir.mkdir(parents=True, exist_ok=True)

            ts = int(time.time())
            dump_path = self._dump_dir / f"emergency_{ts}.json"

            # Collect what we can from Redis.
            redis_state: dict[str, Any] = {}
            try:
                kill_val = self._redis.get("kill_switch")
                redis_state["kill_switch"] = kill_val
                pos_raw = self._redis.get("open_positions")
                redis_state["open_positions"] = (
                    json.loads(pos_raw) if pos_raw else []
                )
            except Exception:
                redis_state["error"] = "redis_read_failed"

            dump = {
                "timestamp": ts,
                "reason": reason,
                "level": "EMERGENCY",
                "redis_state": redis_state,
            }

            await asyncio.to_thread(
                dump_path.write_text, json.dumps(dump, indent=2),
            )
            logger.critical("kill_switch_state_dumped", path=str(dump_path))
        except Exception:
            logger.critical("kill_switch_dump_failed", exc_info=True)
