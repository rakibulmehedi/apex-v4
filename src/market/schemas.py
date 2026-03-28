"""Pydantic v2 data-contract schemas — APEX_V4_STRATEGY.md Section 6.

Every field, type, and constraint matches the strategy spec exactly.
These schemas are the Python-side validation layer; PostgreSQL tables
in db/models.py are the persistence layer.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)


# ---------------------------------------------------------------------------
# Enums — match strategy doc
# ---------------------------------------------------------------------------


class TradingSession(StrEnum):
    LONDON = "LONDON"
    NY = "NY"
    ASIA = "ASIA"
    OVERLAP = "OVERLAP"


class Strategy(StrEnum):
    MOMENTUM = "MOMENTUM"
    MEAN_REVERSION = "MEAN_REVERSION"


class Regime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    UNDEFINED = "UNDEFINED"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Decision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REDUCE = "REDUCE"


class RiskState(StrEnum):
    NORMAL = "NORMAL"
    THROTTLE = "THROTTLE"
    HARD_STOP = "HARD_STOP"


# ---------------------------------------------------------------------------
# OHLCV — candle bar (referenced by MarketSnapshot)
# ---------------------------------------------------------------------------


class OHLCV(BaseModel):
    """Single OHLCV candle bar."""

    model_config = ConfigDict(frozen=True)

    open: float
    high: float
    low: float
    close: float
    volume: Annotated[float, Field(ge=0)]


# ---------------------------------------------------------------------------
# MarketSnapshot — Section 6
# ---------------------------------------------------------------------------

# Minimum candle counts per timeframe (strategy spec).
_MIN_CANDLES = {"M5": 50, "M15": 50, "H1": 200, "H4": 50}

# Staleness threshold in milliseconds.
_STALE_MS = 5000


class CandleMap(BaseModel):
    """Timeframe-keyed candle arrays with minimum-count validation."""

    model_config = ConfigDict(frozen=True)

    M5: list[OHLCV] = Field(min_length=50)
    M15: list[OHLCV] = Field(min_length=50)
    H1: list[OHLCV] = Field(min_length=200)
    H4: list[OHLCV] = Field(min_length=50)


class MarketSnapshot(BaseModel):
    """Raw market snapshot — one per pair per poll cycle.

    ``is_stale`` is a computed field: True when the snapshot timestamp
    is more than 5 000 ms behind the current wall-clock time.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["MarketSnapshot"] = "MarketSnapshot"
    pair: Annotated[str, Field(min_length=6, max_length=6)]
    timestamp: Annotated[int, Field(gt=0, description="Unix ms UTC")]
    candles: CandleMap
    spread_points: Annotated[float, Field(gt=0)]
    session: TradingSession

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_stale(self) -> bool:
        """True if snapshot is older than 5 000 ms."""
        now_ms = int(time.time() * 1000)
        return (now_ms - self.timestamp) > _STALE_MS


# ---------------------------------------------------------------------------
# FeatureVector — Section 6
# ---------------------------------------------------------------------------


class FeatureVector(BaseModel):
    """Computed technical indicators for one pair at one point in time."""

    model_config = ConfigDict(frozen=True)

    type: Literal["FeatureVector"] = "FeatureVector"
    pair: Annotated[str, Field(min_length=6, max_length=6)]
    timestamp: Annotated[int, Field(gt=0, description="Unix ms UTC")]
    atr_14: float
    adx_14: float
    ema_200: float
    bb_upper: float
    bb_lower: float
    bb_mid: float
    session: TradingSession
    spread_ok: bool
    news_blackout: bool


# ---------------------------------------------------------------------------
# AlphaHypothesis — Section 6
# ---------------------------------------------------------------------------


class AlphaHypothesis(BaseModel):
    """Trade hypothesis emitted by an alpha engine.

    ``conviction`` is mean-reversion only (0.65–1.0 when present).
    ``expected_R`` must be >= 1.8 (hard gate from strategy spec).
    ``setup_score`` is an integer 0–30.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["AlphaHypothesis"] = "AlphaHypothesis"
    strategy: Strategy
    pair: Annotated[str, Field(min_length=6, max_length=6)]
    direction: Direction
    entry_zone: Annotated[tuple[float, float], Field(min_length=2, max_length=2)]
    stop_loss: float
    take_profit: float
    setup_score: Annotated[int, Field(ge=0, le=30)]
    expected_R: Annotated[float, Field(ge=1.8)]
    regime: Regime
    conviction: Annotated[float, Field(ge=0.65, le=1.0)] | None = None

    @model_validator(mode="after")
    def _conviction_only_for_mr(self) -> AlphaHypothesis:
        if self.strategy == Strategy.MOMENTUM and self.conviction is not None:
            raise ValueError("conviction must be None for MOMENTUM strategy")
        return self


# ---------------------------------------------------------------------------
# CalibratedTradeIntent — Section 6
# ---------------------------------------------------------------------------


class CalibratedTradeIntent(BaseModel):
    """Calibration engine output — size and edge from historical segments.

    ``p_win`` comes from PostgreSQL trade history, never from AI.
    ``edge`` must be > 0 or the trade is rejected upstream.
    ``suggested_size`` is capped at 0.02 (2 % hard cap, Section 7.1).
    """

    model_config = ConfigDict(frozen=True)

    p_win: Annotated[float, Field(ge=0.0, le=1.0)]
    expected_R: float
    edge: Annotated[float, Field(gt=0)]
    suggested_size: Annotated[float, Field(ge=0.0, le=0.02)]
    segment_count: Annotated[int, Field(ge=0)]


# ---------------------------------------------------------------------------
# RiskDecision — Section 6
# ---------------------------------------------------------------------------


class RiskDecision(BaseModel):
    """Risk governor output — approve, reject, or reduce a trade.

    ``gate_failed`` is 1–7 indicating which risk gate failed,
    or None when decision is APPROVE.
    """

    model_config = ConfigDict(frozen=True)

    decision: Decision
    final_size: Annotated[float, Field(ge=0.0)]
    reason: Annotated[str, Field(min_length=1)]
    risk_state: RiskState
    gate_failed: Annotated[int, Field(ge=1, le=7)] | None = None

    @model_validator(mode="after")
    def _gate_failed_consistency(self) -> RiskDecision:
        if self.decision == Decision.APPROVE and self.gate_failed is not None:
            raise ValueError("gate_failed must be None when decision is APPROVE")
        if self.decision != Decision.APPROVE and self.gate_failed is None:
            raise ValueError("gate_failed is required when decision is REJECT or REDUCE")
        return self
