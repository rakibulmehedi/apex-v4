"""Unit tests for src/risk/kill_switch.py — KillSwitch.

Tests cover:
  - Escalation only (SOFT → HARD → EMERGENCY)
  - No de-escalation (HARD → SOFT forbidden)
  - Dual persistence (Redis + PostgreSQL) on every state change
  - Startup recovery from PostgreSQL
  - Manual reset with exact confirmation string
  - PermissionError on wrong confirmation
  - EMERGENCY: MT5 disconnect, state dump, alert callback
  - Chaos test: trigger HARD → simulate restart → verify HARD persists
  - allows_new_signals / is_active properties
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.risk.kill_switch import KillLevel, KillSwitch


# ── fixtures ──────────────────────────────────────────────────────────────

_KILL_SWITCH_DDL = """
CREATE TABLE kill_switch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms BIGINT NOT NULL,
    level VARCHAR(20) NOT NULL,
    previous_state VARCHAR(20) NOT NULL,
    new_state VARCHAR(20) NOT NULL,
    reason TEXT NOT NULL,
    broker_state_mismatch BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""


class FakeRedis:
    """Minimal in-memory Redis stand-in."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def set(self, key: str, value: str, **kwargs) -> None:
        self._store[key] = value

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


def _make_sqlite_session_factory():
    engine = create_engine(
        "sqlite://",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as conn:
        conn.execute(text(_KILL_SWITCH_DDL))
        conn.commit()
    return sessionmaker(bind=engine)


def _make_ks(
    redis: FakeRedis | None = None,
    sf=None,
    mt5=None,
    alert_cb=None,
    dump_dir: Path | None = None,
) -> KillSwitch:
    """Create a KillSwitch with test doubles."""
    return KillSwitch(
        redis_client=redis or FakeRedis(),
        session_factory=sf or _make_sqlite_session_factory(),
        mt5_client=mt5,
        alert_callback=alert_cb,
        dump_dir=dump_dir or Path("/tmp/apex_test_emergency"),
    )


# ── escalation ───────────────────────────────────────────────────────────

class TestEscalation:
    """Only escalate — never auto-de-escalate."""

    @pytest.mark.asyncio
    async def test_soft_activates(self):
        ks = _make_ks()
        changed = await ks.trigger("SOFT", "test")
        assert changed is True
        assert ks.level == KillLevel.SOFT
        assert ks.label == "SOFT"

    @pytest.mark.asyncio
    async def test_hard_activates(self):
        ks = _make_ks()
        await ks.trigger("HARD", "test")
        assert ks.level == KillLevel.HARD

    @pytest.mark.asyncio
    async def test_emergency_activates(self):
        ks = _make_ks()
        await ks.trigger("EMERGENCY", "test")
        assert ks.level == KillLevel.EMERGENCY

    @pytest.mark.asyncio
    async def test_escalation_soft_to_hard(self):
        ks = _make_ks()
        await ks.trigger("SOFT", "soft reason")
        changed = await ks.trigger("HARD", "hard reason")
        assert changed is True
        assert ks.level == KillLevel.HARD

    @pytest.mark.asyncio
    async def test_escalation_soft_to_emergency(self):
        ks = _make_ks()
        await ks.trigger("SOFT", "soft")
        changed = await ks.trigger("EMERGENCY", "emergency")
        assert changed is True
        assert ks.level == KillLevel.EMERGENCY

    @pytest.mark.asyncio
    async def test_escalation_hard_to_emergency(self):
        ks = _make_ks()
        await ks.trigger("HARD", "hard")
        changed = await ks.trigger("EMERGENCY", "emergency")
        assert changed is True
        assert ks.level == KillLevel.EMERGENCY

    @pytest.mark.asyncio
    async def test_no_deescalation_hard_to_soft(self):
        """HARD → SOFT is forbidden."""
        ks = _make_ks()
        await ks.trigger("HARD", "hard")
        changed = await ks.trigger("SOFT", "try deescalate")
        assert changed is False
        assert ks.level == KillLevel.HARD

    @pytest.mark.asyncio
    async def test_no_deescalation_emergency_to_hard(self):
        ks = _make_ks()
        await ks.trigger("EMERGENCY", "emergency")
        changed = await ks.trigger("HARD", "try deescalate")
        assert changed is False
        assert ks.level == KillLevel.EMERGENCY

    @pytest.mark.asyncio
    async def test_no_deescalation_emergency_to_soft(self):
        ks = _make_ks()
        await ks.trigger("EMERGENCY", "emergency")
        changed = await ks.trigger("SOFT", "try deescalate")
        assert changed is False
        assert ks.level == KillLevel.EMERGENCY

    @pytest.mark.asyncio
    async def test_same_level_no_change(self):
        ks = _make_ks()
        await ks.trigger("SOFT", "first")
        changed = await ks.trigger("SOFT", "second")
        assert changed is False

    @pytest.mark.asyncio
    async def test_invalid_level_raises(self):
        ks = _make_ks()
        with pytest.raises(ValueError, match="Invalid kill level"):
            await ks.trigger("INVALID", "bad")


# ── properties ───────────────────────────────────────────────────────────

class TestProperties:
    @pytest.mark.asyncio
    async def test_initial_state(self):
        ks = _make_ks()
        assert ks.level == KillLevel.NONE
        assert ks.label is None
        assert ks.is_active is False
        assert ks.allows_new_signals() is True

    @pytest.mark.asyncio
    async def test_soft_blocks_signals(self):
        ks = _make_ks()
        await ks.trigger("SOFT", "test")
        assert ks.is_active is True
        assert ks.allows_new_signals() is False

    @pytest.mark.asyncio
    async def test_hard_blocks_signals(self):
        ks = _make_ks()
        await ks.trigger("HARD", "test")
        assert ks.allows_new_signals() is False

    @pytest.mark.asyncio
    async def test_emergency_blocks_signals(self):
        ks = _make_ks()
        await ks.trigger("EMERGENCY", "test")
        assert ks.allows_new_signals() is False


# ── dual persistence ─────────────────────────────────────────────────────

class TestPersistence:
    """Every state change persists to Redis AND PostgreSQL."""

    @pytest.mark.asyncio
    async def test_redis_updated_on_trigger(self):
        redis = FakeRedis()
        ks = _make_ks(redis=redis)
        await ks.trigger("SOFT", "test")
        assert redis.get("kill_switch") == "SOFT"

    @pytest.mark.asyncio
    async def test_redis_escalation_updates(self):
        redis = FakeRedis()
        ks = _make_ks(redis=redis)
        await ks.trigger("SOFT", "first")
        assert redis.get("kill_switch") == "SOFT"
        await ks.trigger("HARD", "second")
        assert redis.get("kill_switch") == "HARD"

    @pytest.mark.asyncio
    async def test_postgres_event_inserted(self):
        sf = _make_sqlite_session_factory()
        ks = _make_ks(sf=sf)
        await ks.trigger("SOFT", "pg test")

        with sf() as db:
            rows = db.execute(text("SELECT * FROM kill_switch_events")).fetchall()
            assert len(rows) == 1
            assert rows[0][4] == "SOFT"  # new_state
            assert rows[0][5] == "pg test"  # reason

    @pytest.mark.asyncio
    async def test_postgres_multiple_events(self):
        sf = _make_sqlite_session_factory()
        ks = _make_ks(sf=sf)
        await ks.trigger("SOFT", "reason1")
        await ks.trigger("HARD", "reason2")

        with sf() as db:
            rows = db.execute(text("SELECT * FROM kill_switch_events")).fetchall()
            assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_redis_cleared_on_reset(self):
        redis = FakeRedis()
        ks = _make_ks(redis=redis)
        await ks.trigger("SOFT", "test")
        assert redis.get("kill_switch") == "SOFT"

        await ks.manual_reset("I CONFIRM SYSTEM IS SAFE")
        assert redis.get("kill_switch") is None


# ── startup recovery ─────────────────────────────────────────────────────

class TestStartupRecovery:
    """On startup: read state from PostgreSQL."""

    @pytest.mark.asyncio
    async def test_recovery_reads_hard_from_db(self):
        """Simulate: DB has HARD → new KillSwitch recovers to HARD."""
        sf = _make_sqlite_session_factory()
        redis = FakeRedis()

        # First instance triggers HARD.
        ks1 = _make_ks(redis=redis, sf=sf)
        await ks1.trigger("HARD", "first crash")

        # Simulate process restart: new KillSwitch, same DB.
        ks2 = KillSwitch(
            redis_client=FakeRedis(),  # fresh Redis (simulates restart)
            session_factory=sf,        # same DB (persistent)
        )
        await ks2.recover_from_db()

        assert ks2.level == KillLevel.HARD
        assert ks2.allows_new_signals() is False

    @pytest.mark.asyncio
    async def test_recovery_empty_db_is_clear(self):
        sf = _make_sqlite_session_factory()
        ks = _make_ks(sf=sf)
        await ks.recover_from_db()
        assert ks.level == KillLevel.NONE
        assert ks.allows_new_signals() is True

    @pytest.mark.asyncio
    async def test_recovery_mirrors_to_redis(self):
        sf = _make_sqlite_session_factory()
        redis_old = FakeRedis()
        ks1 = _make_ks(redis=redis_old, sf=sf)
        await ks1.trigger("SOFT", "first")

        redis_new = FakeRedis()
        ks2 = KillSwitch(redis_client=redis_new, session_factory=sf)
        await ks2.recover_from_db()

        assert redis_new.get("kill_switch") == "SOFT"


# ── manual reset ─────────────────────────────────────────────────────────

class TestManualReset:

    @pytest.mark.asyncio
    async def test_correct_confirmation_resets(self):
        ks = _make_ks()
        await ks.trigger("HARD", "test")
        await ks.manual_reset("I CONFIRM SYSTEM IS SAFE", operator="admin")
        assert ks.level == KillLevel.NONE
        assert ks.allows_new_signals() is True

    @pytest.mark.asyncio
    async def test_wrong_confirmation_raises(self):
        ks = _make_ks()
        await ks.trigger("SOFT", "test")
        with pytest.raises(PermissionError):
            await ks.manual_reset("yes reset please")
        assert ks.level == KillLevel.SOFT  # unchanged

    @pytest.mark.asyncio
    async def test_empty_confirmation_raises(self):
        ks = _make_ks()
        await ks.trigger("SOFT", "test")
        with pytest.raises(PermissionError):
            await ks.manual_reset("")

    @pytest.mark.asyncio
    async def test_reset_persists_to_db(self):
        sf = _make_sqlite_session_factory()
        ks = _make_ks(sf=sf)
        await ks.trigger("SOFT", "test")
        await ks.manual_reset("I CONFIRM SYSTEM IS SAFE")

        with sf() as db:
            rows = db.execute(text("SELECT * FROM kill_switch_events")).fetchall()
            assert len(rows) == 2
            assert rows[1][4] == "NONE"  # new_state

    @pytest.mark.asyncio
    async def test_reset_then_retrigger(self):
        """After reset, can trigger again."""
        ks = _make_ks()
        await ks.trigger("SOFT", "first")
        await ks.manual_reset("I CONFIRM SYSTEM IS SAFE")
        changed = await ks.trigger("SOFT", "second")
        assert changed is True
        assert ks.level == KillLevel.SOFT


# ── HARD action: flatten positions ────────────────────────────────────────

class TestHardAction:

    @pytest.mark.asyncio
    async def test_hard_flattens_positions(self):
        mt5 = MagicMock()
        pos = MagicMock()
        pos.ticket = 12345
        pos.symbol = "EURUSD"
        pos.volume = 0.1
        pos.type = 0  # BUY
        mt5.positions_get.return_value = [pos]
        mt5.order_send.return_value = MagicMock()  # success

        ks = _make_ks(mt5=mt5)
        await ks.trigger("HARD", "test flatten")

        mt5.positions_get.assert_called_once()
        mt5.order_send.assert_called_once()
        req = mt5.order_send.call_args[0][0]
        assert req["position"] == 12345
        assert req["symbol"] == "EURUSD"
        assert req["volume"] == 0.1
        assert req["type"] == 1  # reversed from BUY(0) to SELL(1)

    @pytest.mark.asyncio
    async def test_hard_no_mt5_skips_flatten(self):
        """No MT5 client → flatten is skipped (not an error)."""
        ks = _make_ks(mt5=None)
        await ks.trigger("HARD", "no mt5")
        assert ks.level == KillLevel.HARD

    @pytest.mark.asyncio
    async def test_hard_no_positions(self):
        mt5 = MagicMock()
        mt5.positions_get.return_value = []
        ks = _make_ks(mt5=mt5)
        await ks.trigger("HARD", "no positions")
        mt5.order_send.assert_not_called()


# ── EMERGENCY action ─────────────────────────────────────────────────────

class TestEmergencyAction:

    @pytest.mark.asyncio
    async def test_emergency_disconnects_mt5(self):
        mt5 = MagicMock()
        ks = _make_ks(mt5=mt5)
        await ks.trigger("EMERGENCY", "test emergency")
        mt5.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_emergency_fires_alert(self):
        alert = AsyncMock()
        ks = _make_ks(alert_cb=alert)
        await ks.trigger("EMERGENCY", "alert test")
        alert.assert_awaited_once_with("alert test")

    @pytest.mark.asyncio
    async def test_emergency_dumps_state(self, tmp_path):
        redis = FakeRedis()
        redis.set("open_positions", json.dumps([{"pair": "EURUSD"}]))

        ks = _make_ks(redis=redis, dump_dir=tmp_path)
        await ks.trigger("EMERGENCY", "dump test")

        files = list(tmp_path.glob("emergency_*.json"))
        assert len(files) == 1

        dump = json.loads(files[0].read_text())
        assert dump["level"] == "EMERGENCY"
        assert dump["reason"] == "dump test"
        assert dump["redis_state"]["open_positions"] == [{"pair": "EURUSD"}]


# ── chaos test ───────────────────────────────────────────────────────────

class TestChaosRestart:
    """Trigger HARD, kill process, restart, verify HARD persists."""

    @pytest.mark.asyncio
    async def test_hard_survives_restart(self):
        sf = _make_sqlite_session_factory()
        redis1 = FakeRedis()

        # Process 1: trigger HARD.
        ks1 = KillSwitch(
            redis_client=redis1,
            session_factory=sf,
        )
        await ks1.trigger("HARD", "VaR breach")
        assert ks1.level == KillLevel.HARD

        # === SIMULATED PROCESS DEATH ===
        # redis1 is gone, ks1 is gone.  Only sf (PostgreSQL) survives.

        # Process 2: new KillSwitch, fresh Redis, same DB.
        redis2 = FakeRedis()
        ks2 = KillSwitch(
            redis_client=redis2,
            session_factory=sf,
        )

        # Before recovery: clean slate.
        assert ks2.level == KillLevel.NONE

        # Recover from DB.
        await ks2.recover_from_db()

        # Verify: HARD persisted and blocks trading.
        assert ks2.level == KillLevel.HARD
        assert ks2.allows_new_signals() is False
        assert redis2.get("kill_switch") == "HARD"

        # Verify: cannot downgrade.
        changed = await ks2.trigger("SOFT", "try downgrade")
        assert changed is False
        assert ks2.level == KillLevel.HARD

        # Verify: only manual reset unlocks.
        await ks2.manual_reset("I CONFIRM SYSTEM IS SAFE", operator="oncall")
        assert ks2.level == KillLevel.NONE
        assert ks2.allows_new_signals() is True

    @pytest.mark.asyncio
    async def test_emergency_survives_restart(self):
        sf = _make_sqlite_session_factory()

        ks1 = KillSwitch(redis_client=FakeRedis(), session_factory=sf)
        await ks1.trigger("EMERGENCY", "catastrophic")

        ks2 = KillSwitch(redis_client=FakeRedis(), session_factory=sf)
        await ks2.recover_from_db()

        assert ks2.level == KillLevel.EMERGENCY
        assert ks2.allows_new_signals() is False


# ── KillLevel enum ordering ─────────────────────────────────────────────

class TestKillLevelOrdering:
    def test_ordering(self):
        assert KillLevel.NONE < KillLevel.SOFT
        assert KillLevel.SOFT < KillLevel.HARD
        assert KillLevel.HARD < KillLevel.EMERGENCY

    def test_values(self):
        assert KillLevel.NONE == 0
        assert KillLevel.SOFT == 1
        assert KillLevel.HARD == 2
        assert KillLevel.EMERGENCY == 3
