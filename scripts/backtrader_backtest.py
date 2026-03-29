#!/usr/bin/env python3
"""scripts/backtrader_backtest.py — Full backtest with real PostgreSQL OHLCV data.

Loads historical candle data from the ``candles`` table (populated by
historical_bootstrap.py), runs backtrader with the APEX V4 signal pipeline
(Feature Fabric → Regime Classifier → Alpha Engines), simulates trade
execution, and reports per-pair and aggregate performance metrics.

Metrics reported:
  - Win rate, average R-multiple, profit factor
  - Sharpe ratio, Sortino ratio, maximum drawdown
  - Regime distribution, signal counts
  - Per-pair breakdown table

Usage:
    python scripts/backtrader_backtest.py [--days 120] [--pair EURUSD]

Env vars:
    APEX_DATABASE_URL  PostgreSQL connection string (or POSTGRES_* vars)
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import backtrader as bt
import numpy as np
import pandas as pd
import structlog
import talib

# Ensure project root is on sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from db.models import Candle, get_database_url, make_engine, make_session_factory
from src.alpha.mean_reversion import MeanReversionEngine
from src.alpha.momentum import MomentumEngine
from src.market.feed import classify_session
from src.market.schemas import (
    AlphaHypothesis,
    CandleMap,
    Direction,
    FeatureVector,
    MarketSnapshot,
    OHLCV,
    Regime,
    Strategy,
    TradingSession,
)
from src.regime.classifier import RegimeClassifier

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]

# Minimum H1 candles for indicator warmup (EMA-200).
_WARMUP = 200

# Spread assumption for historical data (no real spread available).
_SPREAD = 0.00015  # 1.5 pips
_SPREAD_MAX = 0.00030  # 3 pips

# ATR multipliers matching alpha engines.
_SL_ATR_MULT = 1.5
_TP_ATR_MULT = 4.0

# Risk fraction per trade for portfolio return conversion.
_RISK_FRACTION = 0.01

# Terminal colours.
_RED = "\033[91m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Data loading from PostgreSQL
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandleRow:
    """Lightweight candle from DB query."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def load_candles_from_db(
    session_factory: Any,
    pair: str,
    timeframe: str,
    days: int,
) -> list[CandleRow]:
    """Load candles from PostgreSQL ``candles`` table."""
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    with session_factory() as db:
        rows = (
            db.query(Candle)
            .filter(
                Candle.pair == pair,
                Candle.timeframe == timeframe,
                Candle.timestamp_ms >= cutoff_ms,
            )
            .order_by(Candle.timestamp_ms.asc())
            .all()
        )

    return [
        CandleRow(
            timestamp=datetime.fromtimestamp(r.timestamp_ms / 1000, tz=timezone.utc),
            open=r.open,
            high=r.high,
            low=r.low,
            close=r.close,
            volume=r.volume,
        )
        for r in rows
    ]


def candles_to_bt_feed(candles: list[CandleRow], name: str = "") -> bt.feeds.PandasData:
    """Convert CandleRow list to backtrader PandasData feed."""
    records = [
        {
            "datetime": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "openinterest": 0,
        }
        for c in candles
    ]
    df = pd.DataFrame.from_records(records)
    df.set_index("datetime", inplace=True)
    return bt.feeds.PandasData(dataname=df)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    """Single completed trade from the backtest."""

    pair: str
    strategy: str
    regime: str
    session: str
    direction: str
    entry_price: float
    exit_price: float
    r_multiple: float
    won: bool
    opened_at: datetime
    closed_at: datetime


# ---------------------------------------------------------------------------
# Backtrader Strategy — APEX V4 signal pipeline with trade execution
# ---------------------------------------------------------------------------


@dataclass
class PairState:
    """Per-pair accumulated state for indicator computation."""

    opens: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)


class ApexV4Strategy(bt.Strategy):
    """Backtrader strategy implementing the full APEX V4 signal pipeline.

    For each bar: accumulate OHLCV → compute TA-Lib indicators →
    classify regime → generate alpha hypothesis → simulate trade outcome
    using subsequent bars.

    Since backtrader's multi-data support makes outcome simulation complex,
    we accumulate bars and simulate outcomes post-hoc (look-ahead within
    the data array for TP/SL resolution).
    """

    params = (
        ("pair", "EURUSD"),
        ("adx_trend", 31.0),
        ("adx_range", 22.0),
    )

    def __init__(self) -> None:
        self.classifier = RegimeClassifier(
            adx_trend_threshold=self.p.adx_trend,
            adx_range_threshold=self.p.adx_range,
        )
        self.momentum = MomentumEngine()
        self.mr_engine = MeanReversionEngine()

        self.state = PairState()
        self.trades: list[TradeRecord] = []
        self.regime_counts: Counter = Counter()
        self.signal_count = 0
        self._in_trade = False
        self._trade_bar_idx: int = 0

    def next(self) -> None:
        # Accumulate bar data.
        self.state.opens.append(self.data.open[0])
        self.state.highs.append(self.data.high[0])
        self.state.lows.append(self.data.low[0])
        self.state.closes.append(self.data.close[0])
        self.state.volumes.append(self.data.volume[0])

        n = len(self.state.closes)
        if n < _WARMUP:
            return

        # Skip if currently in a trade (wait for outcome resolution).
        if self._in_trade:
            return

        # ── TA-Lib indicators ──────────────────────────────────────
        high = np.array(self.state.highs, dtype=np.float64)
        low = np.array(self.state.lows, dtype=np.float64)
        close = np.array(self.state.closes, dtype=np.float64)

        atr_arr = talib.ATR(high, low, close, timeperiod=14)
        adx_arr = talib.ADX(high, low, close, timeperiod=14)
        ema_arr = talib.EMA(close, timeperiod=200)
        bb_upper, bb_mid, bb_lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2)

        atr_14 = float(atr_arr[-1])
        adx_14 = float(adx_arr[-1])
        ema_200 = float(ema_arr[-1])

        if np.isnan(adx_14) or np.isnan(ema_200) or np.isnan(atr_14):
            return

        # ── Session + FeatureVector ────────────────────────────────
        dt = self.data.datetime.datetime(0)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        session = classify_session(dt.hour)

        fv = FeatureVector(
            pair=self.p.pair,
            timestamp=int(dt.timestamp() * 1000),
            atr_14=atr_14,
            adx_14=adx_14,
            ema_200=ema_200,
            bb_upper=float(bb_upper[-1]),
            bb_mid=float(bb_mid[-1]),
            bb_lower=float(bb_lower[-1]),
            session=session,
            spread_ok=_SPREAD < _SPREAD_MAX,
            news_blackout=False,
        )

        # ── Regime classification ──────────────────────────────────
        close_price = float(close[-1])
        regime = self.classifier.classify(fv, close_price)
        self.regime_counts[regime.value] += 1

        if regime == Regime.UNDEFINED:
            return

        # ── Build MarketSnapshot ───────────────────────────────────
        h1_candles = [
            OHLCV(
                open=self.state.opens[i],
                high=self.state.highs[i],
                low=self.state.lows[i],
                close=self.state.closes[i],
                volume=self.state.volumes[i],
            )
            for i in range(n)
        ]

        # For M5/M15/H4 we use subsets of H1 to satisfy CandleMap minimums.
        # This is a backtest approximation — real pipeline has multi-TF data.
        snapshot = MarketSnapshot(
            pair=self.p.pair,
            timestamp=int(dt.timestamp() * 1000),
            candles=CandleMap(
                M5=h1_candles[-50:],
                M15=h1_candles[-50:],
                H1=h1_candles,
                H4=h1_candles[-50:],
            ),
            spread_points=_SPREAD,
            session=session,
        )

        # ── Alpha engines ──────────────────────────────────────────
        hypothesis = None
        if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
            hypothesis = self.momentum.generate(fv, regime, snapshot)
        elif regime == Regime.RANGING:
            hypothesis = self.mr_engine.generate(fv, regime, snapshot)

        if hypothesis is None:
            return

        self.signal_count += 1

        # ── Simulate trade outcome using look-ahead ────────────────
        trade = self._simulate_trade(hypothesis, dt, session)
        if trade is not None:
            self.trades.append(trade)

    def _simulate_trade(
        self,
        hyp: AlphaHypothesis,
        signal_dt: datetime,
        session: TradingSession,
    ) -> TradeRecord | None:
        """Simulate trade outcome using subsequent bars in the data feed.

        Walks forward through remaining bars checking if SL or TP is hit.
        Uses H1 bars (our primary timeframe) for outcome resolution.
        """
        entry = (hyp.entry_zone[0] + hyp.entry_zone[1]) / 2.0
        sl = hyp.stop_loss
        tp = hyp.take_profit
        direction = hyp.direction

        # Mark as in-trade to skip signal generation until resolved.
        self._in_trade = True
        current_bar = len(self.state.closes) - 1

        # Look ahead up to 120 H1 bars (~5 trading days).
        max_lookahead = 120
        exit_price = entry
        exit_dt = signal_dt
        hit = False

        # We'll check forward bars as they arrive via _check_trade_exit.
        # For simplicity in backtrader, store trade params and resolve later.
        self._pending_trade = {
            "hyp": hyp,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "direction": direction,
            "signal_dt": signal_dt,
            "session": session,
            "bars_waited": 0,
            "max_bars": max_lookahead,
        }

        # Override next() to check trade exit on subsequent bars.
        self._original_next = self.next
        self.next = self._trade_next
        return None

    def _trade_next(self) -> None:
        """Called during an active trade to check for exit."""
        self.state.opens.append(self.data.open[0])
        self.state.highs.append(self.data.high[0])
        self.state.lows.append(self.data.low[0])
        self.state.closes.append(self.data.close[0])
        self.state.volumes.append(self.data.volume[0])

        pt = self._pending_trade
        pt["bars_waited"] += 1

        bar_high = self.data.high[0]
        bar_low = self.data.low[0]
        bar_close = self.data.close[0]

        entry = pt["entry"]
        sl = pt["sl"]
        tp = pt["tp"]
        direction = pt["direction"]

        exit_price = None
        hit_type = None

        if direction == Direction.LONG:
            if bar_low <= sl:
                exit_price = sl
                hit_type = "sl"
            elif bar_high >= tp:
                exit_price = tp
                hit_type = "tp"
        else:  # SHORT
            if bar_high >= sl:
                exit_price = sl
                hit_type = "sl"
            elif bar_low <= tp:
                exit_price = tp
                hit_type = "tp"

        # Timeout check.
        if exit_price is None and pt["bars_waited"] >= pt["max_bars"]:
            exit_price = bar_close
            hit_type = "timeout"

        if exit_price is not None:
            # Resolve the trade.
            sl_distance = abs(entry - sl)
            if sl_distance == 0:
                self._in_trade = False
                self.next = self._original_next
                return

            if direction == Direction.LONG:
                r_mult = (exit_price - entry) / sl_distance
            else:
                r_mult = (entry - exit_price) / sl_distance

            dt = self.data.datetime.datetime(0)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            hyp = pt["hyp"]
            trade = TradeRecord(
                pair=self.p.pair,
                strategy=hyp.strategy.value,
                regime=hyp.regime.value,
                session=pt["session"].value,
                direction=direction.value,
                entry_price=round(entry, 5),
                exit_price=round(exit_price, 5),
                r_multiple=round(r_mult, 4),
                won=r_mult > 0,
                opened_at=pt["signal_dt"],
                closed_at=dt,
            )
            self.trades.append(trade)

            # Resume normal signal generation.
            self._in_trade = False
            self.next = self._original_next


# ---------------------------------------------------------------------------
# Performance metrics computation
# ---------------------------------------------------------------------------


def compute_metrics(trades: list[TradeRecord]) -> dict[str, Any] | None:
    """Compute performance metrics from a list of trade records.

    Returns None if fewer than 5 trades.
    """
    if len(trades) < 5:
        return None

    r_multiples = np.array([t.r_multiple for t in trades])
    wins = int(np.sum(r_multiples > 0))
    losses = len(trades) - wins
    gross_profit = float(np.sum(r_multiples[r_multiples > 0]))
    gross_loss = float(np.abs(np.sum(r_multiples[r_multiples <= 0])))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Convert to daily portfolio returns for Sharpe/drawdown.
    records = []
    for t in trades:
        records.append({"date": t.closed_at.date(), "return": t.r_multiple * _RISK_FRACTION})
    df = pd.DataFrame(records)
    daily = df.groupby("date")["return"].sum()
    idx = pd.bdate_range(start=daily.index.min(), end=daily.index.max())
    returns = daily.reindex(idx, fill_value=0.0)

    # NumPy 2.0 compat.
    if not hasattr(np, "NINF"):
        np.NINF = -np.inf  # type: ignore[attr-defined]
    if not hasattr(np, "PINF"):
        np.PINF = np.inf  # type: ignore[attr-defined]
    import empyrical as ep

    sharpe = float(ep.sharpe_ratio(returns))
    sortino = float(ep.sortino_ratio(returns))
    max_dd = float(ep.max_drawdown(returns))
    total_return = float(ep.cum_returns_final(returns))

    return {
        "total_trades": len(trades),
        "winning": wins,
        "losing": losses,
        "win_rate": wins / len(trades),
        "avg_r": float(np.mean(r_multiples)),
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "total_return": total_return,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(
    pair_results: dict[str, dict[str, Any]],
    all_trades: list[TradeRecord],
    regime_counts: Counter,
) -> None:
    """Print the full backtest report."""
    print(f"\n{_BOLD}{'=' * 90}")
    print("  APEX V4 — BACKTEST REPORT (Real PostgreSQL OHLCV Data)")
    print(f"{'=' * 90}{_RESET}")

    # ── Regime distribution ────────────────────────────────────────
    total_classified = sum(regime_counts.values())
    print(f"\n{_BOLD}Regime Distribution{_RESET} ({total_classified:,} classified bars)")
    for regime in ["TRENDING_UP", "TRENDING_DOWN", "RANGING", "UNDEFINED"]:
        count = regime_counts.get(regime, 0)
        pct = count / total_classified * 100 if total_classified > 0 else 0
        bar = "█" * int(pct / 2)
        print(f"  {regime:<16} {pct:5.1f}%  {_DIM}{bar}{_RESET}  ({count:,})")

    trending = regime_counts.get("TRENDING_UP", 0) + regime_counts.get("TRENDING_DOWN", 0)
    ranging = regime_counts.get("RANGING", 0)
    if total_classified > 0:
        print(f"\n  Trending: {trending / total_classified * 100:.1f}%  |  Ranging: {ranging / total_classified * 100:.1f}%")

    # ── Per-pair results ───────────────────────────────────────────
    print(f"\n{_BOLD}Per-Pair Performance{_RESET}")
    print(
        f"  {'Pair':<8} {'Trades':>7} {'Win%':>7} {'AvgR':>8} "
        f"{'PF':>7} {'Sharpe':>8} {'Sortino':>9} {'MaxDD':>8} {'Return':>8}"
    )
    print(f"  {'─' * 8} {'─' * 7} {'─' * 7} {'─' * 8} {'─' * 7} {'─' * 8} {'─' * 9} {'─' * 8} {'─' * 8}")

    for pair in PAIRS:
        m = pair_results.get(pair)
        if m is None:
            print(f"  {pair:<8} {_DIM}insufficient data{_RESET}")
            continue

        wr_color = _GREEN if m["win_rate"] >= 0.50 else _RED
        dd_color = _GREEN if m["max_drawdown"] > -0.10 else _RED
        sh_color = _GREEN if m["sharpe"] > 0 else _RED

        print(
            f"  {pair:<8} {m['total_trades']:>7} "
            f"{wr_color}{m['win_rate'] * 100:>6.1f}%{_RESET} "
            f"{m['avg_r']:>+7.2f}R "
            f"{m['profit_factor']:>7.2f} "
            f"{sh_color}{m['sharpe']:>8.2f}{_RESET} "
            f"{m['sortino']:>9.2f} "
            f"{dd_color}{m['max_drawdown'] * 100:>7.1f}%{_RESET} "
            f"{m['total_return'] * 100:>+7.1f}%"
        )

    # ── Aggregate results ──────────────────────────────────────────
    agg = compute_metrics(all_trades)
    if agg:
        print(f"\n{_BOLD}Aggregate Performance{_RESET}")
        print(f"  Total trades:    {agg['total_trades']:>6}")
        print(f"  Win / Loss:      {agg['winning']:>3} / {agg['losing']}")

        wr_color = _GREEN if agg["win_rate"] >= 0.50 else _RED
        print(f"  Win rate:        {wr_color}{agg['win_rate'] * 100:.1f}%{_RESET}")
        print(f"  Avg R-multiple:  {agg['avg_r']:>+.3f}")
        print(f"  Profit factor:   {agg['profit_factor']:.2f}")

        sh_color = _GREEN if agg["sharpe"] > 0 else _RED
        print(f"  Sharpe ratio:    {sh_color}{agg['sharpe']:.3f}{_RESET}")
        print(f"  Sortino ratio:   {agg['sortino']:.3f}")

        dd_color = _GREEN if agg["max_drawdown"] > -0.10 else _RED
        print(f"  Max drawdown:    {dd_color}{agg['max_drawdown'] * 100:.2f}%{_RESET}")
        print(f"  Total return:    {agg['total_return'] * 100:+.2f}%")

    # ── Strategy breakdown ─────────────────────────────────────────
    mom_trades = [t for t in all_trades if t.strategy == "MOMENTUM"]
    mr_trades = [t for t in all_trades if t.strategy == "MEAN_REVERSION"]
    print(f"\n{_BOLD}Strategy Breakdown{_RESET}")
    print(f"  Momentum signals:       {len(mom_trades)}")
    if mom_trades:
        mom_wins = sum(1 for t in mom_trades if t.won)
        print(
            f"    Win rate: {mom_wins / len(mom_trades) * 100:.1f}%  "
            f"Avg R: {np.mean([t.r_multiple for t in mom_trades]):+.3f}"
        )
    print(f"  Mean Reversion signals: {len(mr_trades)}")
    if mr_trades:
        mr_wins = sum(1 for t in mr_trades if t.won)
        print(
            f"    Win rate: {mr_wins / len(mr_trades) * 100:.1f}%  Avg R: {np.mean([t.r_multiple for t in mr_trades]):+.3f}"
        )

    # ── Pass/Fail gate ─────────────────────────────────────────────
    print(f"\n{_BOLD}{'=' * 90}{_RESET}")
    if agg and agg["win_rate"] >= 0.48 and agg["avg_r"] > 0:
        print(
            f"  {_GREEN}BACKTEST PASS — Win rate {agg['win_rate'] * 100:.1f}% >= 48%, Avg R {agg['avg_r']:+.3f} > 0{_RESET}"
        )
    elif agg:
        print(f"  {_RED}BACKTEST NEEDS REVIEW — Win rate {agg['win_rate'] * 100:.1f}%, Avg R {agg['avg_r']:+.3f}{_RESET}")
    else:
        print(f"  {_RED}BACKTEST FAIL — insufficient trades{_RESET}")
    print(f"{_BOLD}{'=' * 90}{_RESET}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="APEX V4 backtest with real PostgreSQL OHLCV data.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=120,
        help="Lookback period in days (default: 120)",
    )
    parser.add_argument(
        "--pair",
        type=str,
        default=None,
        help="Run for a single pair only (e.g., EURUSD)",
    )
    args = parser.parse_args()

    pairs = [args.pair] if args.pair else PAIRS

    print(f"\n{_BOLD}APEX V4 — Backtest Engine{_RESET}")
    print(f"Period: {args.days} days  |  Pairs: {', '.join(pairs)}")

    # Suppress structlog noise during backtest.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(40),  # ERROR only
    )

    sf = make_session_factory()

    all_trades: list[TradeRecord] = []
    pair_results: dict[str, dict[str, Any]] = {}
    total_regime_counts: Counter = Counter()

    for pair in pairs:
        print(f"\n{_CYAN}Loading {pair} H1 candles...{_RESET}", end=" ", flush=True)
        candles = load_candles_from_db(sf, pair, "H1", args.days)
        print(f"{len(candles):,} bars")

        if len(candles) < _WARMUP + 50:
            print(f"  {_YELLOW}Skipping — need at least {_WARMUP + 50} bars{_RESET}")
            continue

        # Run backtrader.
        feed = candles_to_bt_feed(candles, name=pair)

        cerebro = bt.Cerebro()
        cerebro.adddata(feed)
        cerebro.addstrategy(ApexV4Strategy, pair=pair)

        results = cerebro.run()
        strat: ApexV4Strategy = results[0]

        # Collect results.
        pair_trades = strat.trades
        all_trades.extend(pair_trades)
        total_regime_counts.update(strat.regime_counts)

        metrics = compute_metrics(pair_trades)
        if metrics:
            pair_results[pair] = metrics

        print(
            f"  {_GREEN}{len(pair_trades)} trades{_RESET}  |  "
            f"{strat.signal_count} signals  |  "
            f"{sum(strat.regime_counts.values()):,} bars classified"
        )

    # Restore structlog.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(20),
    )

    # Print full report.
    print_report(pair_results, all_trades, total_regime_counts)


if __name__ == "__main__":
    main()
