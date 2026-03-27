"""APEX V4 — PostgreSQL schema (SQLAlchemy ORM).

Tables defined per APEX_V4_STRATEGY.md Section 5 (State Architecture)
and Section 6 (Module Contracts).

Rule: PostgreSQL is the source of truth.  Redis is always derived from it.
On restart, Redis is populated from PostgreSQL — never the reverse.
"""
from __future__ import annotations

import os

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Enums (match strategy doc exactly)
# ---------------------------------------------------------------------------

SessionEnum = Enum(
    "LONDON", "NY", "ASIA", "OVERLAP",
    name="trading_session",
    create_type=True,
)

StrategyEnum = Enum(
    "MOMENTUM", "MEAN_REVERSION",
    name="strategy_type",
    create_type=True,
)

RegimeEnum = Enum(
    "TRENDING_UP", "TRENDING_DOWN", "RANGING", "UNDEFINED",
    name="regime_type",
    create_type=True,
)

DirectionEnum = Enum(
    "LONG", "SHORT",
    name="direction_type",
    create_type=True,
)

KillSwitchLevelEnum = Enum(
    "SOFT", "HARD", "EMERGENCY",
    name="kill_switch_level",
    create_type=True,
)

TimeframeEnum = Enum(
    "M5", "M15", "H1", "H4",
    name="timeframe_type",
    create_type=True,
)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class MarketSnapshot(Base):
    """Raw market snapshot — one row per pair per poll cycle.

    Strategy ref: Section 6, MarketSnapshot contract (lines 371-388).
    Candle data stored as JSONB keyed by timeframe.
    """
    __tablename__ = "market_snapshots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pair = Column(String(6), nullable=False)
    timestamp_ms = Column(BigInteger, nullable=False, comment="Unix ms UTC")
    candles = Column(JSONB, nullable=False, comment="Timeframe-keyed OHLCV arrays")
    spread_points = Column(Float, nullable=False)
    session = Column(SessionEnum, nullable=False)
    is_stale = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_market_snapshots_pair_ts", "pair", "timestamp_ms"),
    )


class Candle(Base):
    """OHLCV candle — one row per pair per timeframe per bar.

    Strategy ref: Section 5 state architecture.
    Minimum history: M5(50), M15(50), H1(200), H4(50).
    """
    __tablename__ = "candles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pair = Column(String(6), nullable=False)
    timeframe = Column(TimeframeEnum, nullable=False)
    timestamp_ms = Column(BigInteger, nullable=False, comment="Bar open time, Unix ms UTC")
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_candles_pair_tf_ts", "pair", "timeframe", "timestamp_ms", unique=True),
    )


class FeatureVector(Base):
    """Computed technical indicators — one row per pair per cycle.

    Strategy ref: Section 6, FeatureVector contract (lines 390-407).
    Redis cache key: fv:{pair}  TTL 300s.
    """
    __tablename__ = "feature_vectors"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pair = Column(String(6), nullable=False)
    timestamp_ms = Column(BigInteger, nullable=False, comment="Unix ms UTC")
    atr_14 = Column(Float, nullable=False)
    adx_14 = Column(Float, nullable=False)
    ema_200 = Column(Float, nullable=False)
    bb_upper = Column(Float, nullable=False)
    bb_lower = Column(Float, nullable=False)
    bb_mid = Column(Float, nullable=False)
    session = Column(SessionEnum, nullable=False)
    spread_ok = Column(Boolean, nullable=False)
    news_blackout = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_feature_vectors_pair_ts", "pair", "timestamp_ms"),
    )


class TradeOutcome(Base):
    """Trade result record — one row per closed trade.

    Strategy ref: Section 5 learning loop; Phase 4 (P4.3).
    Segment key = (strategy x regime x session).
    Minimum 30 outcomes per segment before live trading (ADR-002).
    """
    __tablename__ = "trade_outcomes"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pair = Column(String(6), nullable=False)
    strategy = Column(StrategyEnum, nullable=False)
    regime = Column(RegimeEnum, nullable=False)
    session = Column(SessionEnum, nullable=False)
    direction = Column(DirectionEnum, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    r_multiple = Column(Float, nullable=False, comment="Actual return / risk")
    won = Column(Boolean, nullable=False)
    fill_id = Column(BigInteger, nullable=True, comment="FK to fills.id if available")
    opened_at = Column(
        DateTime(timezone=True), nullable=False, comment="Trade open time",
    )
    closed_at = Column(
        DateTime(timezone=True), nullable=False, comment="Trade close time",
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_trade_outcomes_segment", "strategy", "regime", "session"),
        Index("ix_trade_outcomes_pair_closed", "pair", "closed_at"),
    )


class KillSwitchEvent(Base):
    """Kill switch state change audit trail.

    Strategy ref: Section 4, ADR-005 (lines 247-251).
    Levels: SOFT → no new signals, HARD → flatten, EMERGENCY → disconnect.
    """
    __tablename__ = "kill_switch_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp_ms = Column(BigInteger, nullable=False, comment="Unix ms UTC")
    level = Column(KillSwitchLevelEnum, nullable=False)
    previous_state = Column(String(20), nullable=False)
    new_state = Column(String(20), nullable=False)
    reason = Column(Text, nullable=False)
    broker_state_mismatch = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_kill_switch_events_ts", "timestamp_ms"),
    )


class Fill(Base):
    """Execution fill record — one row per confirmed fill.

    Strategy ref: Section 5, Execution Gateway; Phase 4 (P4.2).
    Only recorded after TRADE_RETCODE_DONE (V3 bug P0.4 fix).
    """
    __tablename__ = "fills"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    order_id = Column(BigInteger, nullable=False, comment="MT5 order ticket")
    pair = Column(String(6), nullable=False)
    direction = Column(DirectionEnum, nullable=False)
    strategy = Column(StrategyEnum, nullable=False)
    regime = Column(RegimeEnum, nullable=False)
    requested_size = Column(Float, nullable=False)
    actual_size = Column(Float, nullable=False)
    requested_price = Column(Float, nullable=False)
    actual_fill_price = Column(Float, nullable=False)
    slippage_points = Column(Float, nullable=False, comment="|actual - requested| / point_size")
    filled_at = Column(
        DateTime(timezone=True), nullable=False, comment="Time of fill confirmation",
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_fills_order_id", "order_id", unique=True),
        Index("ix_fills_pair_filled", "pair", "filled_at"),
    )


class ReconciliationLog(Base):
    """State reconciliation audit — one row per reconciler heartbeat.

    Strategy ref: Section 5, Risk Engine; ADR-004.
    Reconciler heartbeat: 5 seconds.
    ANY mismatch → HARD kill switch.
    """
    __tablename__ = "reconciliation_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    timestamp_ms = Column(BigInteger, nullable=False, comment="Unix ms UTC")
    redis_positions = Column(JSONB, nullable=False, comment="Snapshot of Redis open_positions")
    mt5_positions = Column(JSONB, nullable=False, comment="Snapshot of MT5 broker positions")
    mismatch_detected = Column(Boolean, nullable=False)
    positions_diverged = Column(JSONB, nullable=True, comment="Details of divergent positions")
    action_taken = Column(String(20), nullable=True, comment="SOFT|HARD or null if no mismatch")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_reconciliation_log_ts", "timestamp_ms"),
        Index("ix_reconciliation_log_mismatch", "mismatch_detected"),
    )


# ---------------------------------------------------------------------------
# Engine / Session factory
# ---------------------------------------------------------------------------

def get_database_url() -> str:
    """Build PostgreSQL URL from environment variables.

    Resolution order:
      1. APEX_DATABASE_URL (full connection string)
      2. Individual POSTGRES_* vars (assembled into URL)
      3. Fallback to localhost with no auth (dev only)

    On the Windows VPS, nssm_install.ps1 loads secrets.env which should
    set APEX_DATABASE_URL or POSTGRES_USER + POSTGRES_PASSWORD.
    """
    url = os.environ.get("APEX_DATABASE_URL")
    if url:
        return url

    # Build from individual parts (matches Alembic env.py logic)
    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "apex_v4")

    if user and password:
        from urllib.parse import quote_plus
        return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"

    # Dev fallback — will fail on production if auth is required
    return f"postgresql://{host}:{port}/{db}"


def make_engine(url: str | None = None):
    """Create a SQLAlchemy engine with production-grade pool settings.

    - pool_pre_ping: detect stale connections before use
    - pool_recycle: recycle connections after 30 min (survives PG restarts)
    - pool_size/max_overflow: bounded connection pool
    """
    return create_engine(
        url or get_database_url(),
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=10,
    )


def make_session_factory(engine=None) -> sessionmaker[Session]:
    """Create a session factory bound to the given engine."""
    if engine is None:
        engine = make_engine()
    return sessionmaker(bind=engine, expire_on_commit=False)
