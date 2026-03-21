"""Unit tests for src/features/state.py — Redis + PostgreSQL state layer.

Redis is mocked via fakeredis.  SQLAlchemy is mocked via unittest.mock.
No real connections to Redis or PostgreSQL.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.features.state import PostgresWriter, RedisStateManager
from src.market.schemas import FeatureVector, TradingSession


# ── helpers ──────────────────────────────────────────────────────────────

def _make_fv(**overrides) -> FeatureVector:
    base = {
        "pair": "EURUSD",
        "timestamp": int(time.time() * 1000),
        "atr_14": 0.0012,
        "adx_14": 28.5,
        "ema_200": 1.0950,
        "bb_upper": 1.1100,
        "bb_lower": 1.0800,
        "bb_mid": 1.0950,
        "session": "LONDON",
        "spread_ok": True,
        "news_blackout": False,
    }
    base.update(overrides)
    return FeatureVector(**base)


def _make_outcome() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "pair": "EURUSD",
        "strategy": "MOMENTUM",
        "regime": "TRENDING_UP",
        "session": "LONDON",
        "direction": "LONG",
        "entry_price": 1.0950,
        "exit_price": 1.1000,
        "r_multiple": 2.5,
        "won": True,
        "fill_id": None,
        "opened_at": now,
        "closed_at": now,
    }


class FakeRedis:
    """Minimal in-memory Redis mock with TTL tracking."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value
        if ex is not None:
            self._ttls[key] = ex

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)
        self._ttls.pop(key, None)

    # Test helpers
    def ttl_of(self, key: str) -> int | None:
        return self._ttls.get(key)

    def exists_key(self, key: str) -> bool:
        return key in self._store


# ═════════════════════════════════════════════════════════════════════════
# RedisStateManager — feature vectors
# ═════════════════════════════════════════════════════════════════════════

class TestRedisFeatureVector:
    def test_store_and_get_roundtrip(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        fv = _make_fv()
        mgr.store_feature_vector(fv)
        got = mgr.get_feature_vector("EURUSD")
        assert got is not None
        assert got.pair == "EURUSD"
        assert got.atr_14 == fv.atr_14
        assert got.session == TradingSession.LONDON

    def test_ttl_is_300(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.store_feature_vector(_make_fv())
        assert r.ttl_of("fv:EURUSD") == 300

    def test_get_missing_returns_none(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        assert mgr.get_feature_vector("GBPUSD") is None

    def test_key_format(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.store_feature_vector(_make_fv(pair="GBPUSD"))
        assert r.exists_key("fv:GBPUSD")

    def test_overwrite_updates_value(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.store_feature_vector(_make_fv(atr_14=0.001))
        mgr.store_feature_vector(_make_fv(atr_14=0.002))
        got = mgr.get_feature_vector("EURUSD")
        assert got is not None
        assert got.atr_14 == 0.002

    def test_all_fields_preserved(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        fv = _make_fv(
            adx_14=55.5,
            bb_upper=1.2,
            bb_lower=0.9,
            bb_mid=1.05,
            spread_ok=False,
            news_blackout=True,
        )
        mgr.store_feature_vector(fv)
        got = mgr.get_feature_vector("EURUSD")
        assert got is not None
        assert got.adx_14 == 55.5
        assert got.bb_upper == 1.2
        assert got.bb_lower == 0.9
        assert got.spread_ok is False
        assert got.news_blackout is True


# ═════════════════════════════════════════════════════════════════════════
# RedisStateManager — open positions
# ═════════════════════════════════════════════════════════════════════════

class TestRedisOpenPositions:
    def test_store_and_get_roundtrip(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        positions = [{"ticket": 1, "symbol": "EURUSD", "volume": 0.01}]
        mgr.store_open_positions(positions)
        got = mgr.get_open_positions()
        assert got == positions

    def test_ttl_is_60(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.store_open_positions([])
        assert r.ttl_of("open_positions") == 60

    def test_get_missing_returns_empty_list(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        assert mgr.get_open_positions() == []

    def test_empty_list(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.store_open_positions([])
        assert mgr.get_open_positions() == []

    def test_multiple_positions(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        positions = [
            {"ticket": 1, "symbol": "EURUSD"},
            {"ticket": 2, "symbol": "GBPUSD"},
        ]
        mgr.store_open_positions(positions)
        assert len(mgr.get_open_positions()) == 2


# ═════════════════════════════════════════════════════════════════════════
# RedisStateManager — kill switch
# ═════════════════════════════════════════════════════════════════════════

class TestRedisKillSwitch:
    def test_set_and_get(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_kill_switch("SOFT")
        assert mgr.get_kill_switch() == "SOFT"

    def test_no_ttl(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_kill_switch("HARD")
        # No TTL set — key persists indefinitely.
        assert r.ttl_of("kill_switch") is None

    def test_get_unset_returns_none(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        assert mgr.get_kill_switch() is None

    def test_overwrite(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_kill_switch("SOFT")
        mgr.set_kill_switch("HARD")
        assert mgr.get_kill_switch() == "HARD"

    def test_emergency(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_kill_switch("EMERGENCY")
        assert mgr.get_kill_switch() == "EMERGENCY"


# ═════════════════════════════════════════════════════════════════════════
# RedisStateManager — news blackout
# ═════════════════════════════════════════════════════════════════════════

class TestRedisNewsBlackout:
    def test_set_active(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_news_blackout("EURUSD", active=True, duration_minutes=30)
        assert r.exists_key("news_blackout_EURUSD")
        assert r.ttl_of("news_blackout_EURUSD") == 30 * 60

    def test_custom_duration(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_news_blackout("GBPUSD", active=True, duration_minutes=60)
        assert r.ttl_of("news_blackout_GBPUSD") == 60 * 60

    def test_clear(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_news_blackout("EURUSD", active=True)
        mgr.set_news_blackout("EURUSD", active=False)
        assert not r.exists_key("news_blackout_EURUSD")

    def test_key_format(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_news_blackout("USDJPY", active=True)
        assert r.exists_key("news_blackout_USDJPY")


# ═════════════════════════════════════════════════════════════════════════
# RedisStateManager — env var constructor
# ═════════════════════════════════════════════════════════════════════════

class TestRedisFromEnv:
    def test_default_url(self):
        """Without APEX_REDIS_URL, defaults to localhost:6379/0."""
        with patch.dict("os.environ", {}, clear=False):
            with patch("redis.Redis.from_url") as mock_from_url:
                mock_from_url.return_value = FakeRedis()
                mgr = RedisStateManager()
                mock_from_url.assert_called_once_with(
                    "redis://localhost:6379/0", decode_responses=True,
                )

    def test_custom_url(self):
        with patch.dict("os.environ", {"APEX_REDIS_URL": "redis://myhost:1234/5"}):
            with patch("redis.Redis.from_url") as mock_from_url:
                mock_from_url.return_value = FakeRedis()
                mgr = RedisStateManager()
                mock_from_url.assert_called_once_with(
                    "redis://myhost:1234/5", decode_responses=True,
                )


# ═════════════════════════════════════════════════════════════════════════
# PostgresWriter — feature vector
# ═════════════════════════════════════════════════════════════════════════

class TestPostgresWriteFeatureVector:
    @pytest.mark.asyncio
    async def test_writes_row(self):
        mock_session = MagicMock()
        sf = MagicMock(return_value=mock_session)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        pw = PostgresWriter(session_factory=sf)
        fv = _make_fv()
        await pw.write_feature_vector(fv)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        row = mock_session.add.call_args[0][0]
        assert row.pair == "EURUSD"
        assert row.atr_14 == fv.atr_14
        assert row.timestamp_ms == fv.timestamp

    @pytest.mark.asyncio
    async def test_error_does_not_crash(self):
        """DB failure must be logged, not raised."""
        sf = MagicMock(side_effect=Exception("connection refused"))
        pw = PostgresWriter(session_factory=sf)
        # Must not raise.
        await pw.write_feature_vector(_make_fv())


# ═════════════════════════════════════════════════════════════════════════
# PostgresWriter — trade outcome
# ═════════════════════════════════════════════════════════════════════════

class TestPostgresWriteTradeOutcome:
    @pytest.mark.asyncio
    async def test_writes_row(self):
        mock_session = MagicMock()
        sf = MagicMock(return_value=mock_session)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        pw = PostgresWriter(session_factory=sf)
        outcome = _make_outcome()
        await pw.write_trade_outcome(outcome)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        row = mock_session.add.call_args[0][0]
        assert row.pair == "EURUSD"
        assert row.r_multiple == 2.5
        assert row.won is True

    @pytest.mark.asyncio
    async def test_error_does_not_crash(self):
        sf = MagicMock(side_effect=Exception("timeout"))
        pw = PostgresWriter(session_factory=sf)
        await pw.write_trade_outcome(_make_outcome())


# ═════════════════════════════════════════════════════════════════════════
# PostgresWriter — kill switch event
# ═════════════════════════════════════════════════════════════════════════

class TestPostgresWriteKillSwitch:
    @pytest.mark.asyncio
    async def test_writes_row(self):
        mock_session = MagicMock()
        sf = MagicMock(return_value=mock_session)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        pw = PostgresWriter(session_factory=sf)
        await pw.write_kill_switch_event("HARD", "VaR exceeded 5%")

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        row = mock_session.add.call_args[0][0]
        assert row.level == "HARD"
        assert row.reason == "VaR exceeded 5%"
        assert row.new_state == "HARD"
        assert row.timestamp_ms > 0

    @pytest.mark.asyncio
    async def test_error_does_not_crash(self):
        sf = MagicMock(side_effect=Exception("disk full"))
        pw = PostgresWriter(session_factory=sf)
        await pw.write_kill_switch_event("EMERGENCY", "disk full")


# ═════════════════════════════════════════════════════════════════════════
# PostgresWriter — env var constructor
# ═════════════════════════════════════════════════════════════════════════

class TestPostgresFromEnv:
    def test_uses_apex_database_url(self):
        """Without explicit session_factory, uses APEX_DATABASE_URL."""
        with patch("src.features.state.make_session_factory") as mock_sf:
            mock_sf.return_value = MagicMock()
            pw = PostgresWriter()
            mock_sf.assert_called_once()
