"""Chaos tests for the APEX V4 risk engine.

5 end-to-end chaos scenarios testing the full risk stack:
  Test 1 — Kill switch survives restart (DB persistence)
  Test 2 — State drift halts trading (reconciler → HARD)
  Test 3 — Correlation crisis zeros position (EWMA → Φ(κ)=0 → HARD)
  Test 4 — Fail-fast on Gate 1 (kill switch before stale data)
  Test 5 — Drawdown hard stop (DD > 8% → HARD, sticky reject)

These tests use real SQLite DBs, real KillSwitch/Governor/Reconciler logic,
and real EWMA covariance math.  Only MT5 and Redis are faked.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock

import numpy as np
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.market.mt5_types import Position
from src.market.schemas import (
    AlphaHypothesis,
    CalibratedTradeIntent,
    CandleMap,
    Decision,
    Direction,
    MarketSnapshot,
    OHLCV,
    Regime,
    RiskState,
    Strategy,
    TradingSession,
)
from src.risk.covariance import EWMACovarianceMatrix
from src.risk.governor import RiskGovernor
from src.risk.kill_switch import KillLevel, KillSwitch
from src.risk.reconciler import StateReconciler


# ── shared DDL ──────────────────────────────────────────────────────────────

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


# ── fake Redis ──────────────────────────────────────────────────────────────


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


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_sqlite_sf(*ddls: str) -> sessionmaker:
    """Create an in-memory SQLite session factory with given DDL statements."""
    engine = create_engine(
        "sqlite://",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.connect() as conn:
        for ddl in ddls:
            conn.execute(text(ddl))
        conn.commit()
    return sessionmaker(bind=engine)


def _candles(n: int) -> list[OHLCV]:
    return [OHLCV(open=1.1, high=1.11, low=1.09, close=1.1, volume=100)] * n


def _fresh_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        pair="EURUSD",
        timestamp=int(time.time() * 1000),
        candles=CandleMap(
            M5=_candles(50),
            M15=_candles(50),
            H1=_candles(200),
            H4=_candles(50),
        ),
        spread_points=0.00015,
        session=TradingSession.LONDON,
    )


def _stale_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        pair="EURUSD",
        timestamp=int(time.time() * 1000) - 10_000,
        candles=CandleMap(
            M5=_candles(50),
            M15=_candles(50),
            H1=_candles(200),
            H4=_candles(50),
        ),
        spread_points=0.00015,
        session=TradingSession.LONDON,
    )


def _make_hypothesis() -> AlphaHypothesis:
    return AlphaHypothesis(
        strategy=Strategy.MOMENTUM,
        pair="EURUSD",
        direction=Direction.LONG,
        entry_zone=(1.1000, 1.1010),
        stop_loss=1.0950,
        take_profit=1.1200,
        setup_score=20,
        expected_R=2.0,
        regime=Regime.TRENDING_UP,
    )


def _make_intent(size: float = 0.01) -> CalibratedTradeIntent:
    return CalibratedTradeIntent(
        p_win=0.55,
        expected_R=2.0,
        edge=0.65,
        suggested_size=size,
        segment_count=50,
    )


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


# ═══════════════════════════════════════════════════════════════════════════
# Test 1 — Kill switch survives restart
# ═══════════════════════════════════════════════════════════════════════════


class TestKillSwitchSurvivesRestart:
    """Trigger HARD → simulate process restart → verify HARD persists."""

    @pytest.mark.asyncio
    async def test_hard_persists_across_restart(self):
        sf = _make_sqlite_sf(_KILL_SWITCH_DDL)

        # ── Process 1: trigger HARD ─────────────────────────────────
        redis1 = FakeRedis()
        ks1 = KillSwitch(redis_client=redis1, session_factory=sf)
        await ks1.trigger("HARD", "chaos test VaR breach")
        assert ks1.level == KillLevel.HARD

        # ── SIMULATED PROCESS DEATH ─────────────────────────────────
        # redis1 and ks1 are gone.  Only sf (PostgreSQL) survives.

        # ── Process 2: re-instantiate from DB ───────────────────────
        redis2 = FakeRedis()
        ks2 = KillSwitch(redis_client=redis2, session_factory=sf)

        # Before recovery: clean slate.
        assert ks2.level == KillLevel.NONE

        # Recover from DB — reads HARD from PostgreSQL.
        await ks2.recover_from_db()

        # Verify: HARD persisted.
        assert ks2.level == KillLevel.HARD

        # Verify: evaluate() returns REJECT immediately.
        assert ks2.allows_new_signals() is False

        # Verify: Redis mirrored.
        assert redis2.get("kill_switch") == "HARD"


# ═══════════════════════════════════════════════════════════════════════════
# Test 2 — State drift halts trading
# ═══════════════════════════════════════════════════════════════════════════


class TestStateDriftHaltsTrading:
    """3 positions in Redis, broker has only 2 → HARD triggered."""

    @pytest.mark.asyncio
    async def test_mismatch_triggers_hard_and_reconciles(self):
        sf = _make_sqlite_sf(_KILL_SWITCH_DDL, _RECONCILIATION_LOG_DDL)
        redis = FakeRedis()
        ks = KillSwitch(redis_client=redis, session_factory=sf)

        # ── Redis has 3 positions ───────────────────────────────────
        redis_positions = [
            {"ticket": 1001, "pair": "EURUSD", "type": 0, "volume": 0.1, "price_open": 1.1, "profit": 10.0},
            {"ticket": 1002, "pair": "GBPUSD", "type": 0, "volume": 0.1, "price_open": 1.25, "profit": 5.0},
            {"ticket": 1003, "pair": "USDJPY", "type": 1, "volume": 0.1, "price_open": 150.0, "profit": -2.0},
        ]
        redis.set("open_positions", json.dumps(redis_positions))

        # ── Broker has only 2 positions (1003 was closed) ──────────
        mt5 = MagicMock()
        mt5.positions_get.return_value = [
            _make_position(ticket=1001, symbol="EURUSD"),
            _make_position(ticket=1002, symbol="GBPUSD"),
        ]

        reconciler = StateReconciler(
            mt5_client=mt5,
            redis_client=redis,
            kill_switch=ks,
            session_factory=sf,
            heartbeat=0.01,
        )

        # Run a single cycle.
        await reconciler._cycle()

        # Verify: HARD kill switch triggered within the cycle.
        assert ks.level == KillLevel.HARD
        assert ks.allows_new_signals() is False

        # Verify: Redis updated to match broker (2 positions).
        reconciled = json.loads(redis.get("open_positions"))
        assert len(reconciled) == 2
        tickets = {p["ticket"] for p in reconciled}
        assert tickets == {1001, 1002}


# ═══════════════════════════════════════════════════════════════════════════
# Test 3 — Correlation crisis zeros position
# ═══════════════════════════════════════════════════════════════════════════


class TestCorrelationCrisisZerosPosition:
    """EWMA covariance with κ > 30 → Φ(κ) = 0.0 → HARD + REJECT."""

    @pytest.mark.asyncio
    async def test_kappa_above_30_rejects_and_triggers_hard(self):
        sf = _make_sqlite_sf(_KILL_SWITCH_DDL)
        redis = FakeRedis()
        ks = KillSwitch(redis_client=redis, session_factory=sf)

        # Build a covariance matrix with extreme condition number (κ > 30).
        # Use 2 pairs.  Feed returns that create a near-singular matrix.
        pairs = ["EURUSD", "GBPUSD"]
        cov = EWMACovarianceMatrix(pairs)

        # Feed many identical return vectors — creates rank-1 matrix.
        # A rank-1 matrix has κ → ∞ (one eigenvalue >> the other).
        for _ in range(500):
            cov.update({"EURUSD": 0.01, "GBPUSD": 0.01})

        # Verify κ > 30.
        kappa = cov.condition_number()
        assert kappa > 30, f"Expected κ > 30, got {kappa}"

        # Verify Φ(κ) returns 0.0.
        phi = cov.decay_multiplier()
        assert phi == 0.0

        # Now run through the governor.
        gov = RiskGovernor(kill_switch=ks, covariance=cov)

        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(size=0.01),
            _fresh_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        # Verify: REJECT with reason "correlation_crisis".
        assert result.decision == Decision.REJECT
        assert result.reason == "correlation_crisis"
        assert result.gate_failed == 6
        assert result.final_size == 0.0

        # Verify: HARD kill switch was triggered.
        assert ks.level == KillLevel.HARD
        assert ks.allows_new_signals() is False


# ═══════════════════════════════════════════════════════════════════════════
# Test 4 — Fail-fast on Gate 1
# ═══════════════════════════════════════════════════════════════════════════


class TestFailFastGate1:
    """Kill switch SOFT → evaluate rejects at Gate 1, not Gate 2."""

    @pytest.mark.asyncio
    async def test_gate1_fires_before_gate2(self):
        sf = _make_sqlite_sf(_KILL_SWITCH_DDL)
        redis = FakeRedis()
        ks = KillSwitch(redis_client=redis, session_factory=sf)

        # Set kill switch to SOFT (active).
        await ks.trigger("SOFT", "pre-existing soft")
        assert ks.level == KillLevel.SOFT
        assert ks.allows_new_signals() is False

        # Use a well-conditioned covariance (won't interfere).
        cov = EWMACovarianceMatrix(["EURUSD"])

        gov = RiskGovernor(kill_switch=ks, covariance=cov)

        # Pass a stale snapshot — would fail Gate 2 if it got there.
        result = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _stale_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,
        )

        # Verify: rejection reason is "kill_switch_active" not "stale_data".
        assert result.decision == Decision.REJECT
        assert result.reason == "kill_switch_active"
        assert result.gate_failed == 1

        # Confirms Gate 1 fires before Gate 2.
        assert result.reason != "stale_data"


# ═══════════════════════════════════════════════════════════════════════════
# Test 5 — Drawdown hard stop
# ═══════════════════════════════════════════════════════════════════════════


class TestDrawdownHardStop:
    """DD=9% → HARD + REJECT, and kill switch blocks even after DD passes."""

    @pytest.mark.asyncio
    async def test_drawdown_triggers_hard_and_stays_blocked(self):
        sf = _make_sqlite_sf(_KILL_SWITCH_DDL)
        redis = FakeRedis()
        ks = KillSwitch(redis_client=redis, session_factory=sf)

        # Use well-conditioned covariance.
        cov = EWMACovarianceMatrix(["EURUSD"])

        gov = RiskGovernor(kill_switch=ks, covariance=cov)

        # ── First evaluate: DD = 9% > 8% ───────────────────────────
        result1 = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _fresh_snapshot(),
            portfolio_value=100_000,
            current_dd=0.09,
        )

        # Verify: REJECT with reason "max_drawdown".
        assert result1.decision == Decision.REJECT
        assert result1.reason == "max_drawdown"
        assert result1.gate_failed == 7
        assert result1.risk_state == RiskState.HARD_STOP

        # Verify: HARD kill switch was triggered.
        assert ks.level == KillLevel.HARD
        assert ks.allows_new_signals() is False

        # ── Second evaluate: even with DD = 0%, kill switch blocks ──
        result2 = await gov.evaluate(
            _make_hypothesis(),
            _make_intent(),
            _fresh_snapshot(),
            portfolio_value=100_000,
            current_dd=0.0,  # drawdown recovered
        )

        # Verify: still REJECT — kill switch blocks at Gate 1.
        assert result2.decision == Decision.REJECT
        assert result2.reason == "kill_switch_active"
        assert result2.gate_failed == 1
