# APEX V4 — API Reference

> **Reference:** src/market/schemas.py (Pydantic v2 data contracts)
> **Last Updated:** 2026-03-29

---

## 1. Pydantic Schemas (src/market/schemas.py)

All schemas use Pydantic v2 with `frozen=True` (immutable after construction).

### Enums

```python
class TradingSession(StrEnum):
    LONDON   = "LONDON"    # 07:00–12:00 UTC
    NY       = "NY"        # 16:00–21:00 UTC
    ASIA     = "ASIA"      # 21:00–07:00 UTC
    OVERLAP  = "OVERLAP"   # 12:00–16:00 UTC

class Strategy(StrEnum):
    MOMENTUM        = "MOMENTUM"
    MEAN_REVERSION  = "MEAN_REVERSION"

class Regime(StrEnum):
    TRENDING_UP    = "TRENDING_UP"
    TRENDING_DOWN  = "TRENDING_DOWN"
    RANGING        = "RANGING"
    UNDEFINED      = "UNDEFINED"

class Direction(StrEnum):
    LONG   = "LONG"
    SHORT  = "SHORT"

class Decision(StrEnum):
    APPROVE = "APPROVE"
    REJECT  = "REJECT"
    REDUCE  = "REDUCE"

class RiskState(StrEnum):
    NORMAL    = "NORMAL"
    THROTTLE  = "THROTTLE"
    HARD_STOP = "HARD_STOP"
```

### OHLCV

```python
class OHLCV(BaseModel):
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float  # ≥ 0
```

### MarketSnapshot

```python
class MarketSnapshot(BaseModel):
    type:          Literal["MarketSnapshot"] = "MarketSnapshot"
    pair:          str        # 6 characters, e.g. "EURUSD"
    timestamp:     int        # Unix ms UTC, > 0
    candles:       CandleMap  # M5(50), M15(50), H1(200), H4(50) minimum
    spread_points: float      # ask - bid, > 0
    session:       TradingSession

    # Computed property (not stored)
    is_stale: bool  # True if timestamp > 5000ms behind wall clock
```

Validation rules:
- `pair` must be exactly 6 characters
- `candles.H1` must have ≥ 200 items
- `candles.M5`, `M15`, `H4` must have ≥ 50 items each
- `spread_points` must be > 0

### FeatureVector

```python
class FeatureVector(BaseModel):
    type:          Literal["FeatureVector"] = "FeatureVector"
    pair:          str           # 6 characters
    timestamp:     int           # Unix ms UTC
    atr_14:        float         # Average True Range, period 14
    adx_14:        float         # Average Directional Index, period 14
    ema_200:       float         # Exponential Moving Average, period 200
    bb_upper:      float         # Bollinger Band upper
    bb_lower:      float         # Bollinger Band lower
    bb_mid:        float         # Bollinger Band middle (SMA-20)
    session:       TradingSession
    spread_ok:     bool          # spread_points < max_points (0.00030)
    news_blackout: bool          # True during news windows (Redis key)
```

All computed from H1 candles using TA-Lib.

### AlphaHypothesis

```python
class AlphaHypothesis(BaseModel):
    type:        Literal["AlphaHypothesis"] = "AlphaHypothesis"
    strategy:    Strategy
    pair:        str           # 6 characters
    direction:   Direction
    entry_zone:  tuple[float, float]  # (low, high) entry price range
    stop_loss:   float
    take_profit: float
    setup_score: int           # 0–30
    expected_R:  float         # ≥ 1.8 (hard minimum)
    regime:      Regime
    conviction:  float | None  # 0.65–1.0 for MEAN_REVERSION only; None for MOMENTUM
```

Validation rules:
- `expected_R` must be ≥ 1.8
- `conviction` must be None for MOMENTUM strategy
- `conviction` must be 0.65–1.0 for MEAN_REVERSION strategy
- `setup_score` must be 0–30

### CalibratedTradeIntent

```python
class CalibratedTradeIntent(BaseModel):
    p_win:          float  # 0.0–1.0 (from historical segment data)
    expected_R:     float  # average R-multiple from segment history
    edge:           float  # > 0 (p_win × avg_R − (1 − p_win))
    suggested_size: float  # 0.0–0.02 (Kelly output, 2% hard cap)
    segment_count:  int    # ≥ 0 (number of trades in this segment)
```

Validation: `edge` must be > 0 (enforced at construction — CalibrationEngine never creates this with edge ≤ 0).

### RiskDecision

```python
class RiskDecision(BaseModel):
    decision:    Decision
    final_size:  float         # ≥ 0.0
    reason:      str           # non-empty
    risk_state:  RiskState
    gate_failed: int | None    # 1–7 for REJECT/REDUCE; None for APPROVE
```

Validation:
- `gate_failed` must be None when `decision == APPROVE`
- `gate_failed` must be set (1–7) when `decision == REJECT` or `REDUCE`

---

## 2. Key Classes

### RegimeClassifier (src/regime/classifier.py)

```python
class RegimeClassifier:
    def __init__(
        self,
        adx_trend_threshold: float = 31.0,  # from config/settings.yaml
        adx_range_threshold: float = 22.0,
    ) -> None: ...

    def classify(
        self,
        fv: FeatureVector,
        close_price: float,
    ) -> Regime:
        """Classify regime from FeatureVector and current close price.

        Rules evaluated in order:
          1. news_blackout → UNDEFINED
          2. not spread_ok → UNDEFINED
          3. adx_14 > trend_threshold AND close > ema_200 → TRENDING_UP
          4. adx_14 > trend_threshold AND close < ema_200 → TRENDING_DOWN
          5. adx_14 < range_threshold → RANGING
          6. else → UNDEFINED
        """
```

### MomentumEngine (src/alpha/momentum.py)

```python
class MomentumEngine:
    def __init__(self, min_rr: float = 1.8) -> None: ...

    def generate(
        self,
        fv: FeatureVector,
        snapshot: MarketSnapshot,
        regime: Regime,
    ) -> AlphaHypothesis | None:
        """Generate momentum hypothesis for TRENDING regimes.

        Returns None if:
          - Regime is not TRENDING_UP or TRENDING_DOWN
          - Multi-TF EMA confirmation fails
          - Expected R < min_rr
        """
```

### MeanReversionEngine (src/alpha/mean_reversion.py)

```python
class MeanReversionEngine:
    def __init__(
        self,
        adf_pvalue_threshold: float = 0.05,
        ou_max_halflife_h1: float = 48.0,
        conviction_threshold: float = 0.65,
        zscore_guard: float = 3.0,
        min_rr: float = 1.8,
    ) -> None: ...

    def generate(
        self,
        fv: FeatureVector,
        snapshot: MarketSnapshot,
        regime: Regime,
    ) -> AlphaHypothesis | None:
        """Generate mean reversion hypothesis for RANGING regime.

        Pipeline: ADF gate → Kalman smooth → OU MLE → conviction gate → signal
        Returns None if any gate fails.
        """
```

### CalibrationEngine (src/calibration/engine.py)

```python
class CalibrationEngine:
    def __init__(
        self,
        perf_db: PerformanceDatabase,
        capital_allocation_pct: float = 1.0,
    ) -> None: ...

    def calibrate(
        self,
        hypothesis: AlphaHypothesis,
        session_label: str,
        current_dd: float,
        open_positions: list[dict[str, Any]] | None = None,
    ) -> CalibratedTradeIntent | None:
        """Size trade using Kelly criterion.

        Returns None when:
          - current_dd >= 0.05
          - No segment data (< 30 trades)
          - Computed edge <= 0
        """
```

### RiskGovernor (src/risk/governor.py)

```python
class RiskGovernor:
    def __init__(
        self,
        kill_switch: KillSwitch,
        covariance: EWMACovarianceMatrix,
    ) -> None: ...

    async def evaluate(
        self,
        hypothesis: AlphaHypothesis,
        intent: CalibratedTradeIntent,
        snapshot: MarketSnapshot,
        portfolio_value: float,
        current_dd: float,
        open_positions: list[dict[str, Any]] | None = None,
    ) -> RiskDecision:
        """Run 7 gates sequentially. Fail-fast on first rejection."""
```

### KillSwitch (src/risk/kill_switch.py)

```python
class KillSwitch:
    def __init__(
        self,
        redis_client: Any,
        session_factory: Any,
        mt5_client: MT5Client | None = None,
        alert_callback: Any = None,
        dump_dir: Path = Path("data/emergency"),
    ) -> None: ...

    @property
    def level(self) -> KillLevel: ...          # KillLevel enum
    @property
    def label(self) -> str | None: ...         # "SOFT"|"HARD"|"EMERGENCY"|None
    @property
    def is_active(self) -> bool: ...           # True if any level active
    def allows_new_signals(self) -> bool: ...  # True only when NONE

    async def recover_from_db(self) -> None:
        """Call on startup — restores kill switch state from PostgreSQL."""

    async def trigger(self, level_label: str, reason: str) -> bool:
        """Escalate to level. Only escalates, never de-escalates.

        Args:
            level_label: "SOFT", "HARD", or "EMERGENCY"
            reason: human-readable explanation for audit trail
        Returns:
            True if state changed, False if already at or above requested level.
        """

    async def manual_reset(
        self,
        confirmation: str,  # must be exactly "I CONFIRM SYSTEM IS SAFE"
        operator: str = "unknown",
    ) -> None:
        """Reset kill switch to NONE. Raises PermissionError if confirmation wrong."""
```

### EWMACovarianceMatrix (src/risk/covariance.py)

```python
class EWMACovarianceMatrix:
    def __init__(
        self,
        pairs: list[str],
        lambda_: float = 0.999,
        kappa_warn: float = 15.0,
        kappa_max: float = 30.0,
        gamma: float = 0.5,
    ) -> None: ...

    def update(self, returns: dict[str, float]) -> None:
        """Update with H1 log returns. Call on H1 candle close only."""

    def regularize(self) -> np.ndarray:
        """Return covariance matrix with eigenvalue shrinkage applied."""

    def condition_number(self) -> float:
        """κ = max_eigenvalue / max(min_eigenvalue, 1e-8)"""

    def decay_multiplier(self) -> float:
        """Φ(κ): 1.0 if κ≤15, exp(-0.5(κ-15)) if 15<κ<30, 0.0 if κ≥30"""

    def portfolio_var(
        self,
        weights: dict[str, float],  # pair → position weight
        portfolio_value: float,
    ) -> float:
        """VaR_99 = 2.326 × sqrt(W^T × Σ_reg × W) × portfolio_value"""
```

### MarketFeed (src/market/feed.py)

```python
class MarketFeed:
    def __init__(
        self,
        client: MT5Client,
        pairs: Sequence[str],
        *,
        zmq_addr: str = "tcp://127.0.0.1:5559",
        poll_interval: float = 5.0,
    ) -> None: ...

    async def run(self) -> None:
        """Main loop — poll until asyncio.CancelledError."""

    # Observability
    snapshots_published: int  # count of published snapshots
    validation_errors: int    # count of failed snapshot validations
```

### FeatureFabric (src/features/fabric.py)

```python
class FeatureFabric:
    def __init__(
        self,
        spread_max_points: float,  # from config: spread.max_points
        redis_client: object | None = None,
    ) -> None: ...

    def compute(self, snapshot: MarketSnapshot) -> FeatureVector:
        """Compute TA-Lib indicators from snapshot.

        Raises ValueError if snapshot has < 200 H1 candles.
        All indicators computed on H1 candles.
        """
```

---

## 3. Configuration Schema

Complete reference for `config/settings.yaml`:

```python
# system
system.mode: str              # "paper" | "live"
system.log_level: str         # "DEBUG" | "INFO" | "WARNING" | "ERROR"

# mt5
mt5.mode: str                 # "stub" | "real"
mt5.pairs: list[str]          # e.g. ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
mt5.timeframes: list[str]     # ["M5", "M15", "H1", "H4"]
mt5.candle_limits.M5: int     # 50
mt5.candle_limits.M15: int    # 50
mt5.candle_limits.H1: int     # 200
mt5.candle_limits.H4: int     # 50

# redis
redis.host: str               # "localhost"
redis.port: int               # 6379
redis.db: int                 # 0
redis.ttl.feature_vector: int # 300 seconds
redis.ttl.open_positions: int # 60 seconds
redis.ttl.segment_stats: int  # 3600 seconds
redis.ttl.last_reconcile: int # 30 seconds

# postgres
postgres.host: str            # "localhost"
postgres.port: int            # 5432
postgres.dbname: str          # "apex_v4"

# regime
regime.adx_trend_threshold: float  # 31.0
regime.adx_range_threshold: float  # 22.0

# risk
risk.capital_allocation_pct: float  # 0.10 (10%)
risk.max_position_size: float       # 0.02 (2% hard cap)
risk.kelly_fraction: float          # 0.25 (quarter-Kelly)
risk.min_trade_sample: int          # 30
risk.var_hard_limit: float          # 0.05 (5%)
risk.var_soft_limit: float          # 0.03 (3%)
risk.dd_scalar_thresholds.low: float   # 0.02
risk.dd_scalar_thresholds.high: float  # 0.05
risk.ewma_lambda: float             # 0.999
risk.condition_number_warn: float   # 15.0
risk.condition_number_max: float    # 30.0

# spread
spread.max_points: float      # 0.00030 (3 pips)

# alpha
alpha.min_rr_ratio: float          # 1.8
alpha.adf_pvalue_threshold: float  # 0.05
alpha.ou_max_halflife_h1: float    # 48
alpha.conviction_threshold: float  # 0.65
alpha.zscore_guard: float          # 3.0

# reconciler
reconciler.heartbeat_seconds: int  # 5

# zmq
zmq.address: str              # "tcp://127.0.0.1:5559"

# prometheus
prometheus.port: int          # 8000
```

---

## 4. Prometheus Metrics Reference

Defined in `src/observability/metrics.py`:

```python
# Histogram
CYCLE_DURATION_MS        # apex_pipeline_cycle_duration_ms
                         # Buckets: [10, 25, 50, 100, 250, 500, 1000, 2500]

# Counter (labels: gate_number, reason)
GATE_REJECTIONS_TOTAL    # apex_gate_rejections_total

# Counter (labels: strategy, regime, pair)
SIGNALS_GENERATED_TOTAL  # apex_signals_generated_total

# Counter (labels: level)
KILL_SWITCH_TOTAL        # apex_kill_switch_total

# Gauge
PORTFOLIO_VAR_PCT        # apex_portfolio_var_pct
CURRENT_DRAWDOWN_PCT     # apex_current_drawdown_pct
COVARIANCE_CONDITION     # apex_covariance_condition
OPEN_POSITIONS_COUNT     # apex_open_positions_count
```

---

## 5. Database Access

```python
from db.models import (
    make_engine,
    make_session_factory,
    get_database_url,
    MarketSnapshot,
    Candle,
    FeatureVector,
    TradeOutcome,
    KillSwitchEvent,
    Fill,
    ReconciliationLog,
)

# Create engine (reads from environment variables)
engine = make_engine()
sf = make_session_factory(engine)

# Query example
with sf() as db:
    outcomes = db.query(TradeOutcome).filter(
        TradeOutcome.strategy == "MOMENTUM"
    ).all()
```

### Database URL Resolution Order

1. `APEX_DATABASE_URL` environment variable (full connection string)
2. `POSTGRES_USER` + `POSTGRES_PASSWORD` + optional `POSTGRES_HOST/PORT/DB`
3. Dev fallback: `postgresql://localhost:5432/apex_v4` (no auth — fails on production)

Always use `db.models.get_database_url()` — never build the URL independently.
