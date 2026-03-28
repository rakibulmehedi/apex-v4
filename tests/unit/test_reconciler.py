"""Unit tests for src/risk/reconciler.py — StateReconciler.

Tests cover:
  - Clean cycle (no mismatch): Redis updated, no kill switch
  - Phantom positions (in Redis, not broker): HARD triggered, Redis reconciled
  - Ghost positions (in broker, not Redis): HARD triggered, Redis reconciled
  - Broker disconnect (positions_get → None): EMERGENCY triggered
  - Reconciler exception: EMERGENCY triggered
  - Redis reconciliation: broker always wins
  - DB reconciliation_log written on mismatch
  - last_reconcile_ts updated every cycle
  - Loop runs multiple cycles and can be stopped
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.market.mt5_types import Position
from src.risk.kill_switch import KillLevel, KillSwitch
from src.risk.reconciler import StateReconciler


# ── fixtures ──────────────────────────────────────────────────────────────

_RECONCILIATION_LOG_DDL = """
CREATE TABLE reconciliation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms BIGINT NOT NULL,
    redis_positions TEXT NOT NULL,
    mt5_positions TEXT NOT NULL,
    mismatch_detected BOOLEAN NOT NULL,
    positions_diverged TEXT,
    action_taken VARCHAR(20),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

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
        self._store[key] = str(value)

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


def _make_position(
    ticket: int = 1001,
    symbol: str = "EURUSD",
    type_: int = 0,
    volume: float = 0.1,
) -> Position:
    return Position(
        ticket=ticket,
        symbol=symbol,
        type=type_,
        volume=volume,
        price_open=1.1000,
        price_current=1.1010,
        sl=1.0950,
        tp=1.1100,
        profit=10.0,
        comment="",
    )


def _make_sqlite_sf():
    engine = create_engine(
        "sqlite://",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as conn:
        conn.execute(text(_RECONCILIATION_LOG_DDL))
        conn.execute(text(_KILL_SWITCH_DDL))
        conn.commit()
    return sessionmaker(bind=engine)


def _make_kill_switch(redis=None, sf=None) -> KillSwitch:
    return KillSwitch(
        redis_client=redis or FakeRedis(),
        session_factory=sf or _make_sqlite_sf(),
    )


def _make_reconciler(
    mt5=None,
    redis=None,
    ks=None,
    sf=None,
    heartbeat: float = 0.01,
) -> StateReconciler:
    r = redis or FakeRedis()
    s = sf or _make_sqlite_sf()
    k = ks or _make_kill_switch(redis=r, sf=s)
    m = mt5 or MagicMock()
    return StateReconciler(
        mt5_client=m,
        redis_client=r,
        kill_switch=k,
        session_factory=s,
        heartbeat=heartbeat,
    )


# ── clean cycle ──────────────────────────────────────────────────────────


class TestCleanCycle:
    """No mismatch — positions match, no kill switch triggered."""

    @pytest.mark.asyncio
    async def test_matching_positions(self):
        """Broker and Redis have same tickets → no mismatch."""
        redis = FakeRedis()
        redis.set("open_positions", json.dumps([{"ticket": 1001, "pair": "EURUSD"}]))

        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=1001)]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        assert ks.level == KillLevel.NONE

    @pytest.mark.asyncio
    async def test_both_empty(self):
        """No positions on either side → clean."""
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.return_value = []

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        assert ks.level == KillLevel.NONE

    @pytest.mark.asyncio
    async def test_redis_updated_with_broker_data(self):
        """After clean cycle, Redis has fresh broker snapshot."""
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.return_value = [
            _make_position(ticket=1001, symbol="EURUSD"),
            _make_position(ticket=1002, symbol="GBPUSD"),
        ]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        raw = redis.get("open_positions")
        positions = json.loads(raw)
        assert len(positions) == 2
        tickets = {p["ticket"] for p in positions}
        assert tickets == {1001, 1002}

    @pytest.mark.asyncio
    async def test_last_reconcile_ts_updated(self):
        """last_reconcile_ts set in Redis after every cycle."""
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.return_value = []

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        ts = redis.get("last_reconcile_ts")
        assert ts is not None
        assert int(ts) > 0


# ── phantom positions ────────────────────────────────────────────────────


class TestPhantomPositions:
    """In Redis but NOT in broker → state_drift → HARD."""

    @pytest.mark.asyncio
    async def test_phantom_triggers_hard(self):
        redis = FakeRedis()
        redis.set(
            "open_positions",
            json.dumps(
                [
                    {"ticket": 1001, "pair": "EURUSD"},
                    {"ticket": 9999, "pair": "PHANTOM"},
                ]
            ),
        )

        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=1001)]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        assert ks.level == KillLevel.HARD

    @pytest.mark.asyncio
    async def test_phantom_redis_reconciled_to_broker(self):
        """After phantom detection, Redis matches broker (broker wins)."""
        redis = FakeRedis()
        redis.set(
            "open_positions",
            json.dumps(
                [
                    {"ticket": 1001, "pair": "EURUSD"},
                    {"ticket": 9999, "pair": "PHANTOM"},
                ]
            ),
        )

        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=1001)]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        raw = redis.get("open_positions")
        positions = json.loads(raw)
        assert len(positions) == 1
        assert positions[0]["ticket"] == 1001


# ── ghost positions ──────────────────────────────────────────────────────


class TestGhostPositions:
    """In broker but NOT in Redis → state_drift → HARD."""

    @pytest.mark.asyncio
    async def test_ghost_triggers_hard(self):
        redis = FakeRedis()
        redis.set("open_positions", json.dumps([]))

        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=2001)]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        assert ks.level == KillLevel.HARD

    @pytest.mark.asyncio
    async def test_ghost_redis_reconciled_to_broker(self):
        """After ghost detection, Redis includes the ghost position."""
        redis = FakeRedis()
        redis.set("open_positions", json.dumps([]))

        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=2001, symbol="GBPUSD")]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        raw = redis.get("open_positions")
        positions = json.loads(raw)
        assert len(positions) == 1
        assert positions[0]["ticket"] == 2001
        assert positions[0]["pair"] == "GBPUSD"


# ── mixed phantom + ghost ────────────────────────────────────────────────


class TestMixedDrift:
    @pytest.mark.asyncio
    async def test_phantom_and_ghost_triggers_hard(self):
        """Both phantom and ghost in same cycle → HARD."""
        redis = FakeRedis()
        redis.set("open_positions", json.dumps([{"ticket": 1001, "pair": "EURUSD"}]))

        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=2001)]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        assert ks.level == KillLevel.HARD

        # Redis now has broker's position only
        positions = json.loads(redis.get("open_positions"))
        assert len(positions) == 1
        assert positions[0]["ticket"] == 2001


# ── broker disconnect ────────────────────────────────────────────────────


class TestBrokerDisconnect:
    """positions_get() returns None → EMERGENCY."""

    @pytest.mark.asyncio
    async def test_none_triggers_emergency(self):
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.return_value = None

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        assert ks.level == KillLevel.EMERGENCY

    @pytest.mark.asyncio
    async def test_disconnect_does_not_touch_redis(self):
        """On disconnect, Redis open_positions is NOT updated."""
        redis = FakeRedis()
        redis.set("open_positions", json.dumps([{"ticket": 1001}]))

        mt5 = MagicMock()
        mt5.positions_get.return_value = None

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        # Redis still has old data (not reconciled to empty)
        positions = json.loads(redis.get("open_positions"))
        assert len(positions) == 1


# ── reconciler failure ───────────────────────────────────────────────────


class TestReconcilerFailure:
    """Loop body exception → EMERGENCY, loop continues."""

    @pytest.mark.asyncio
    async def test_exception_triggers_emergency(self):
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.side_effect = RuntimeError("MT5 exploded")

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf, heartbeat=0.01)

        # Run one iteration of the loop
        task = asyncio.create_task(rec.run())
        await asyncio.sleep(0.05)
        rec.stop()
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert ks.level == KillLevel.EMERGENCY


# ── reconciliation_log DB write ──────────────────────────────────────────


class TestReconciliationLog:
    @pytest.mark.asyncio
    async def test_mismatch_writes_log_row(self):
        redis = FakeRedis()
        redis.set("open_positions", json.dumps([{"ticket": 9999, "pair": "PHANTOM"}]))

        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=1001)]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        with sf() as db:
            rows = db.execute(text("SELECT * FROM reconciliation_log")).fetchall()
            assert len(rows) == 1
            assert rows[0][4] == 1  # mismatch_detected = True
            assert rows[0][6] == "HARD"  # action_taken

    @pytest.mark.asyncio
    async def test_clean_cycle_no_log_row(self):
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.return_value = []

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        with sf() as db:
            rows = db.execute(text("SELECT * FROM reconciliation_log")).fetchall()
            assert len(rows) == 0


# ── loop lifecycle ───────────────────────────────────────────────────────


class TestLoopLifecycle:
    @pytest.mark.asyncio
    async def test_stop_terminates_loop(self):
        """rec.stop() cleanly exits the run() loop."""
        mt5 = MagicMock()
        mt5.positions_get.return_value = []

        rec = _make_reconciler(mt5=mt5, heartbeat=0.01)

        task = asyncio.create_task(rec.run())
        await asyncio.sleep(0.05)
        rec.stop()
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify it ran at least once
        assert mt5.positions_get.call_count >= 1

    @pytest.mark.asyncio
    async def test_multiple_clean_cycles(self):
        """Loop runs multiple cycles without issues."""
        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=1001)]

        redis = FakeRedis()
        # Pre-seed Redis so first cycle is clean.
        redis.set("open_positions", json.dumps([{"ticket": 1001, "pair": "EURUSD"}]))
        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf, heartbeat=0.01)

        task = asyncio.create_task(rec.run())
        await asyncio.sleep(0.08)
        rec.stop()
        await asyncio.sleep(0.03)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert mt5.positions_get.call_count >= 2
        assert ks.level == KillLevel.NONE


# ── redis expired / missing ──────────────────────────────────────────────


class TestRedisEdgeCases:
    @pytest.mark.asyncio
    async def test_redis_missing_positions_key(self):
        """Redis has no open_positions → treated as empty list."""
        redis = FakeRedis()  # no open_positions key
        mt5 = MagicMock()
        mt5.positions_get.return_value = [_make_position(ticket=1001)]

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        # Ghost position detected → HARD
        assert ks.level == KillLevel.HARD

    @pytest.mark.asyncio
    async def test_redis_empty_broker_empty_is_clean(self):
        """Both empty → no mismatch."""
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.return_value = []

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        await rec._cycle()

        assert ks.level == KillLevel.NONE


# ── feed silence detection ────────────────────────────────────────────────


class TestFeedSilence:
    """MT5 feed silence detection during active/inactive sessions."""

    @pytest.mark.asyncio
    async def test_silence_during_active_session_triggers_emergency(self):
        """Feed silent >300s during LONDON session → EMERGENCY."""
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.return_value = []

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        # Simulate last snapshot received 301 seconds ago.
        rec._last_snapshot_received_at = time.monotonic() - 301

        # Patch datetime to return an active-session UTC hour (10 = LONDON).
        with patch("src.risk.reconciler.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 10
            await rec._cycle()

        assert ks.level == KillLevel.EMERGENCY

    @pytest.mark.asyncio
    async def test_silence_outside_active_session_no_action(self):
        """Feed silent >300s during ASIA session (03:00 UTC) → no action."""
        redis = FakeRedis()
        mt5 = MagicMock()
        mt5.positions_get.return_value = []

        sf = _make_sqlite_sf()
        ks = _make_kill_switch(redis=redis, sf=sf)
        rec = StateReconciler(mt5, redis, ks, sf)

        # Simulate last snapshot received 301 seconds ago.
        rec._last_snapshot_received_at = time.monotonic() - 301

        # Patch datetime to return an inactive-session UTC hour (3 = ASIA).
        with patch("src.risk.reconciler.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 3
            await rec._cycle()

        assert ks.level == KillLevel.NONE
