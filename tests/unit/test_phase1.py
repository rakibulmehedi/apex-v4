"""Phase 1 integration gate — tests/unit/test_phase1.py

Exit criteria (APEX_V4_STRATEGY.md Section 9):
    pytest tests/unit/test_phase1.py → ALL PASSED

Verifies the complete Phase 1 pipeline:
  P1.2  Pydantic schemas validate correctly
  P1.4  TA-Lib indicators match known reference values
  P1.5  Redis TTL keys set correctly (mocked Redis)
  P1.5  PostgreSQL WAL write called (mocked DB)
  E2E   MarketSnapshot → FeatureVector → Redis + PostgreSQL
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import talib

from src.features.fabric import FeatureFabric
from src.features.state import PostgresWriter, RedisStateManager
from src.market.schemas import (
    CandleMap,
    FeatureVector,
    MarketSnapshot,
    OHLCV,
    TradingSession,
)


# ── helpers ──────────────────────────────────────────────────────────────

def _make_ohlcv(o: float, h: float, l: float, c: float, v: float = 100.0) -> OHLCV:
    return OHLCV(open=o, high=h, low=l, close=c, volume=v)


def _linear_candles(n: int) -> list[OHLCV]:
    """Linear ramp: close from 1.1000 to 1.1199 over *n* bars."""
    closes = np.linspace(1.1000, 1.1199, n)
    return [
        _make_ohlcv(
            o=float(closes[i]),
            h=float(closes[i] + 0.0010),
            l=float(closes[i] - 0.0010),
            c=float(closes[i]),
        )
        for i in range(n)
    ]


def _filler_candles(n: int) -> list[OHLCV]:
    return [_make_ohlcv(1.1, 1.101, 1.099, 1.1) for _ in range(n)]


def _snapshot(
    h1_candles: list[OHLCV],
    *,
    pair: str = "EURUSD",
    spread: float = 0.00015,
    session: TradingSession = TradingSession.LONDON,
) -> MarketSnapshot:
    return MarketSnapshot(
        pair=pair,
        timestamp=int(time.time() * 1000),
        candles=CandleMap(
            M5=_filler_candles(50),
            M15=_filler_candles(50),
            H1=h1_candles,
            H4=_filler_candles(50),
        ),
        spread_points=spread,
        session=session,
    )


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

    def ttl_of(self, key: str) -> int | None:
        return self._ttls.get(key)

    def exists_key(self, key: str) -> bool:
        return key in self._store


# Pre-computed TA-Lib reference values for 200-bar linear ramp
_LINEAR_ATR = 0.0020
_LINEAR_ADX = 100.0
_LINEAR_EMA = 1.10995
_LINEAR_BB_UPPER = 1.1201032563
_LINEAR_BB_MID = 1.11895
_LINEAR_BB_LOWER = 1.1177967437


# ═════════════════════════════════════════════════════════════════════════
# P1.2 — Pydantic schema validation
# ═════════════════════════════════════════════════════════════════════════

class TestSchemaValidation:
    """Verify Pydantic schemas accept valid data and reject invalid data."""

    def test_ohlcv_valid(self):
        o = OHLCV(open=1.1, high=1.2, low=1.0, close=1.15, volume=100)
        assert o.close == 1.15

    def test_ohlcv_negative_volume_rejected(self):
        with pytest.raises(Exception):
            OHLCV(open=1.1, high=1.2, low=1.0, close=1.15, volume=-1)

    def test_market_snapshot_valid(self):
        snap = _snapshot(_linear_candles(200))
        assert snap.pair == "EURUSD"
        assert snap.type == "MarketSnapshot"

    def test_market_snapshot_short_pair_rejected(self):
        with pytest.raises(Exception):
            _snapshot(_linear_candles(200), pair="EUR")

    def test_feature_vector_valid(self):
        fv = FeatureVector(
            pair="EURUSD",
            timestamp=int(time.time() * 1000),
            atr_14=0.002,
            adx_14=28.0,
            ema_200=1.10,
            bb_upper=1.12,
            bb_lower=1.08,
            bb_mid=1.10,
            session="LONDON",
            spread_ok=True,
            news_blackout=False,
        )
        assert fv.type == "FeatureVector"

    def test_feature_vector_frozen(self):
        fv = FeatureVector(
            pair="EURUSD",
            timestamp=int(time.time() * 1000),
            atr_14=0.002,
            adx_14=28.0,
            ema_200=1.10,
            bb_upper=1.12,
            bb_lower=1.08,
            bb_mid=1.10,
            session="LONDON",
            spread_ok=True,
            news_blackout=False,
        )
        with pytest.raises(Exception):
            fv.atr_14 = 0.003  # type: ignore[misc]


# ═════════════════════════════════════════════════════════════════════════
# P1.4 — TA-Lib indicator verification vs known values
# ═════════════════════════════════════════════════════════════════════════

class TestATRKnownValues:
    def test_atr_matches_reference(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.atr_14 == pytest.approx(_LINEAR_ATR, abs=1e-8)

    def test_atr_is_positive(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.atr_14 > 0


class TestADXKnownValues:
    def test_adx_matches_reference(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.adx_14 == pytest.approx(_LINEAR_ADX, abs=1e-8)

    def test_adx_in_range(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert 0 <= fv.adx_14 <= 100


class TestEMA200KnownValues:
    def test_ema200_matches_reference(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.ema_200 == pytest.approx(_LINEAR_EMA, abs=1e-6)


class TestBollingerBandsKnownValues:
    def test_bb_upper_matches_reference(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.bb_upper == pytest.approx(_LINEAR_BB_UPPER, abs=1e-6)

    def test_bb_mid_matches_reference(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.bb_mid == pytest.approx(_LINEAR_BB_MID, abs=1e-6)

    def test_bb_lower_matches_reference(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.bb_lower == pytest.approx(_LINEAR_BB_LOWER, abs=1e-6)

    def test_bb_ordering(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.bb_lower < fv.bb_mid < fv.bb_upper


# ═════════════════════════════════════════════════════════════════════════
# P1.4 — FeatureVector populates correctly
# ═════════════════════════════════════════════════════════════════════════

class TestFeatureVectorPopulation:
    def test_pair_passthrough(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200), pair="GBPUSD"))
        assert fv.pair == "GBPUSD"

    def test_session_passthrough(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200), session=TradingSession.ASIA))
        assert fv.session == TradingSession.ASIA

    def test_spread_ok_true(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200), spread=0.00015))
        assert fv.spread_ok is True

    def test_spread_ok_false(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200), spread=0.00050))
        assert fv.spread_ok is False

    def test_news_blackout_default_false(self):
        fabric = FeatureFabric(spread_max_points=0.00030, redis_client=None)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert fv.news_blackout is False

    def test_insufficient_candles_raises(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        snap = MagicMock()
        snap.candles.H1 = _linear_candles(50)
        with pytest.raises(ValueError, match="Need at least 200"):
            fabric.compute(snap)

    def test_returns_feature_vector_type(self):
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(_snapshot(_linear_candles(200)))
        assert isinstance(fv, FeatureVector)
        assert fv.type == "FeatureVector"


# ═════════════════════════════════════════════════════════════════════════
# P1.5 — Redis TTL keys set correctly (mocked Redis)
# ═════════════════════════════════════════════════════════════════════════

class TestRedisFeatureVectorTTL:
    def test_fv_key_format(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        fv = FeatureVector(
            pair="EURUSD",
            timestamp=int(time.time() * 1000),
            atr_14=0.002, adx_14=28.0, ema_200=1.10,
            bb_upper=1.12, bb_lower=1.08, bb_mid=1.10,
            session="LONDON", spread_ok=True, news_blackout=False,
        )
        mgr.store_feature_vector(fv)
        assert r.exists_key("fv:EURUSD")

    def test_fv_ttl_300(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        fv = FeatureVector(
            pair="EURUSD",
            timestamp=int(time.time() * 1000),
            atr_14=0.002, adx_14=28.0, ema_200=1.10,
            bb_upper=1.12, bb_lower=1.08, bb_mid=1.10,
            session="LONDON", spread_ok=True, news_blackout=False,
        )
        mgr.store_feature_vector(fv)
        assert r.ttl_of("fv:EURUSD") == 300

    def test_fv_roundtrip(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        fv = FeatureVector(
            pair="EURUSD",
            timestamp=int(time.time() * 1000),
            atr_14=0.002, adx_14=28.0, ema_200=1.10,
            bb_upper=1.12, bb_lower=1.08, bb_mid=1.10,
            session="LONDON", spread_ok=True, news_blackout=False,
        )
        mgr.store_feature_vector(fv)
        got = mgr.get_feature_vector("EURUSD")
        assert got is not None
        assert got.pair == "EURUSD"
        assert got.atr_14 == fv.atr_14


class TestRedisPositionsTTL:
    def test_positions_ttl_60(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.store_open_positions([{"ticket": 1, "symbol": "EURUSD"}])
        assert r.ttl_of("open_positions") == 60


class TestRedisKillSwitchPersistence:
    def test_kill_switch_no_ttl(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_kill_switch("HARD")
        assert r.ttl_of("kill_switch") is None

    def test_kill_switch_roundtrip(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_kill_switch("SOFT")
        assert mgr.get_kill_switch() == "SOFT"


class TestRedisNewsBlackoutTTL:
    def test_news_blackout_ttl(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_news_blackout("EURUSD", active=True, duration_minutes=30)
        assert r.ttl_of("news_blackout_EURUSD") == 1800

    def test_news_blackout_clear(self):
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.set_news_blackout("EURUSD", active=True)
        mgr.set_news_blackout("EURUSD", active=False)
        assert not r.exists_key("news_blackout_EURUSD")


# ═════════════════════════════════════════════════════════════════════════
# P1.5 — PostgreSQL WAL write called (mocked DB)
# ═════════════════════════════════════════════════════════════════════════

class TestPostgresFeatureVectorWrite:
    @pytest.mark.asyncio
    async def test_write_fv_calls_add_and_commit(self):
        mock_session = MagicMock()
        sf = MagicMock(return_value=mock_session)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        pw = PostgresWriter(session_factory=sf)
        fv = FeatureVector(
            pair="EURUSD",
            timestamp=int(time.time() * 1000),
            atr_14=0.002, adx_14=28.0, ema_200=1.10,
            bb_upper=1.12, bb_lower=1.08, bb_mid=1.10,
            session="LONDON", spread_ok=True, news_blackout=False,
        )
        await pw.write_feature_vector(fv)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        row = mock_session.add.call_args[0][0]
        assert row.pair == "EURUSD"
        assert row.atr_14 == 0.002

    @pytest.mark.asyncio
    async def test_write_fv_error_does_not_crash(self):
        sf = MagicMock(side_effect=Exception("connection refused"))
        pw = PostgresWriter(session_factory=sf)
        fv = FeatureVector(
            pair="EURUSD",
            timestamp=int(time.time() * 1000),
            atr_14=0.002, adx_14=28.0, ema_200=1.10,
            bb_upper=1.12, bb_lower=1.08, bb_mid=1.10,
            session="LONDON", spread_ok=True, news_blackout=False,
        )
        # Must not raise
        await pw.write_feature_vector(fv)


class TestPostgresTradeOutcomeWrite:
    @pytest.mark.asyncio
    async def test_write_outcome_calls_add_and_commit(self):
        mock_session = MagicMock()
        sf = MagicMock(return_value=mock_session)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        pw = PostgresWriter(session_factory=sf)
        now = datetime.now(timezone.utc)
        outcome = {
            "pair": "EURUSD", "strategy": "MOMENTUM",
            "regime": "TRENDING_UP", "session": "LONDON",
            "direction": "LONG", "entry_price": 1.0950,
            "exit_price": 1.1000, "r_multiple": 2.5,
            "won": True, "fill_id": None,
            "opened_at": now, "closed_at": now,
        }
        await pw.write_trade_outcome(outcome)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()


class TestPostgresKillSwitchWrite:
    @pytest.mark.asyncio
    async def test_write_ks_event_calls_add_and_commit(self):
        mock_session = MagicMock()
        sf = MagicMock(return_value=mock_session)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        pw = PostgresWriter(session_factory=sf)
        await pw.write_kill_switch_event("HARD", "VaR exceeded")

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        row = mock_session.add.call_args[0][0]
        assert row.level == "HARD"
        assert row.reason == "VaR exceeded"


# ═════════════════════════════════════════════════════════════════════════
# End-to-end: MarketSnapshot → FeatureVector → Redis + PostgreSQL
# ═════════════════════════════════════════════════════════════════════════

class TestEndToEndPipeline:
    """Full P1 pipeline: snapshot → fabric → state layer."""

    def test_snapshot_to_fv_to_redis(self):
        """MarketSnapshot → FeatureFabric.compute → RedisStateManager.store."""
        snap = _snapshot(_linear_candles(200))
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(snap)

        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.store_feature_vector(fv)

        got = mgr.get_feature_vector("EURUSD")
        assert got is not None
        assert got.pair == "EURUSD"
        assert got.atr_14 == pytest.approx(_LINEAR_ATR, abs=1e-8)
        assert got.adx_14 == pytest.approx(_LINEAR_ADX, abs=1e-8)
        assert got.ema_200 == pytest.approx(_LINEAR_EMA, abs=1e-6)
        assert got.bb_upper == pytest.approx(_LINEAR_BB_UPPER, abs=1e-6)
        assert got.bb_mid == pytest.approx(_LINEAR_BB_MID, abs=1e-6)
        assert got.bb_lower == pytest.approx(_LINEAR_BB_LOWER, abs=1e-6)
        assert got.spread_ok is True
        assert got.session == TradingSession.LONDON
        assert r.ttl_of("fv:EURUSD") == 300

    @pytest.mark.asyncio
    async def test_snapshot_to_fv_to_postgres(self):
        """MarketSnapshot → FeatureFabric.compute → PostgresWriter.write."""
        snap = _snapshot(_linear_candles(200))
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(snap)

        mock_session = MagicMock()
        sf = MagicMock(return_value=mock_session)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        pw = PostgresWriter(session_factory=sf)
        await pw.write_feature_vector(fv)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        row = mock_session.add.call_args[0][0]
        assert row.pair == "EURUSD"
        assert row.atr_14 == pytest.approx(_LINEAR_ATR, abs=1e-8)
        assert row.timestamp_ms == fv.timestamp

    def test_full_pipeline_redis_and_postgres_together(self):
        """Complete pipeline: snapshot → fabric → Redis + PostgreSQL (sync path)."""
        snap = _snapshot(_linear_candles(200), pair="GBPUSD")
        fabric = FeatureFabric(spread_max_points=0.00030)
        fv = fabric.compute(snap)

        # Redis store
        r = FakeRedis()
        mgr = RedisStateManager(client=r)
        mgr.store_feature_vector(fv)

        # PostgreSQL store (sync path to avoid async in this test)
        mock_session = MagicMock()
        sf = MagicMock(return_value=mock_session)
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        pw = PostgresWriter(session_factory=sf)
        pw._sync_write_fv(fv)

        # Verify both stores received the same data
        redis_fv = mgr.get_feature_vector("GBPUSD")
        pg_row = mock_session.add.call_args[0][0]

        assert redis_fv is not None
        assert redis_fv.pair == pg_row.pair == "GBPUSD"
        assert redis_fv.atr_14 == pytest.approx(pg_row.atr_14, abs=1e-10)
