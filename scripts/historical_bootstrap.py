#!/usr/bin/env python3
"""scripts/historical_bootstrap.py — Pre-warm V4 database with historical MT5 data.

Pulls 120 days of candle data from the MT5 terminal, stores it in PostgreSQL,
runs the full signal pipeline (Feature Fabric → Regime Classifier → Alpha Engines),
and simulates trade outcomes using actual subsequent price movement.

This gives the calibration engine real historical data to work with from day one
instead of relying on blind bootstrap mode.

Usage:
    python scripts/historical_bootstrap.py [--days 120] [--dry-run]

Env vars:
    APEX_DATABASE_URL  PostgreSQL connection string (or POSTGRES_* vars)

Requirements:
    - MT5 terminal running (Windows only)
    - PostgreSQL database with tables created (alembic upgrade head)
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

# Ensure project root is on sys.path for imports.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from db.models import (
    Candle,
    TradeOutcome,
    get_database_url,
    make_engine,
    make_session_factory,
)
from src.alpha.mean_reversion import MeanReversionEngine
from src.alpha.momentum import MomentumEngine
from src.features.fabric import FeatureFabric
from src.market.mt5_real import RealMT5Client
from src.market.mt5_types import TIMEFRAME_MAP, RateBar
from src.market.schemas import (
    CandleMap,
    Direction,
    MarketSnapshot,
    OHLCV,
    Regime,
    TradingSession,
)
from src.regime.classifier import RegimeClassifier

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]
TIMEFRAMES = ["M5", "M15", "H1", "H4"]

# Bars to fetch per timeframe for 120 days.
# M5:  120d × 24h × 12 = 34 560  (fetch 35 000 with margin)
# M15: 120d × 24h × 4  = 11 520  (fetch 12 000)
# H1:  120d × 24h      =  2 880  (fetch  3 000)
# H4:  120d × 6         =   720  (fetch    800)
_BAR_COUNTS: dict[str, int] = {
    "M5": 35_000,
    "M15": 12_000,
    "H1": 3_000,
    "H4": 800,
}

# Minimum candles required per timeframe for a valid snapshot.
_MIN_CANDLES = {"M5": 50, "M15": 50, "H1": 200, "H4": 50}

# How many M5 bars to look ahead for trade outcome simulation (~5 trading days).
_OUTCOME_LOOKAHEAD_M5 = 1440  # 5 days × 24h × 12

# Spread assumption for historical data (no real spread available).
_DEFAULT_SPREAD_POINTS = 0.00015  # 1.5 pips — conservative

# Spread max from settings.yaml.
_SPREAD_MAX_POINTS = 0.00030

# Segment minimum.
_MIN_SEGMENT = 30

# Terminal output colours.
_RED = "\033[91m"
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Session classifier — mirrors src/market/feed.py
# ---------------------------------------------------------------------------


def classify_session(utc_hour: int) -> TradingSession:
    """Map UTC hour (0-23) to TradingSession."""
    if 12 <= utc_hour < 16:
        return TradingSession.OVERLAP
    if 7 <= utc_hour < 12:
        return TradingSession.LONDON
    if 16 <= utc_hour < 21:
        return TradingSession.NY
    return TradingSession.ASIA


# ---------------------------------------------------------------------------
# Step 1: Fetch candles from MT5
# ---------------------------------------------------------------------------


def fetch_candles(
    mt5: RealMT5Client,
    pair: str,
    timeframe: str,
    count: int,
) -> list[RateBar]:
    """Fetch historical candles from MT5 for a single pair/timeframe."""
    tf_const = TIMEFRAME_MAP[timeframe]
    bars = mt5.copy_rates_from_pos(pair, tf_const, 0, count)
    if bars is None:
        logger.error("fetch_failed", pair=pair, timeframe=timeframe)
        return []
    logger.info("fetched_candles", pair=pair, timeframe=timeframe, count=len(bars))
    return bars


def fetch_all_candles(
    mt5: RealMT5Client,
    days: int,
) -> dict[str, dict[str, list[RateBar]]]:
    """Fetch candles for all pairs and timeframes.

    Returns nested dict: ``{pair: {timeframe: [RateBar, ...]}}``
    """
    # Scale bar counts proportionally if days != 120.
    scale = days / 120.0

    result: dict[str, dict[str, list[RateBar]]] = {}
    for pair in PAIRS:
        result[pair] = {}
        for tf in TIMEFRAMES:
            count = max(int(_BAR_COUNTS[tf] * scale), _MIN_CANDLES[tf])
            bars = fetch_candles(mt5, pair, tf, count)
            result[pair][tf] = bars
    return result


# ---------------------------------------------------------------------------
# Step 2: Store candles in PostgreSQL
# ---------------------------------------------------------------------------


def store_candles(
    session_factory: Any,
    all_candles: dict[str, dict[str, list[RateBar]]],
) -> int:
    """Bulk-insert candles into the ``candles`` table (upsert on conflict)."""
    inserted = 0
    with session_factory() as db:
        for pair, tf_map in all_candles.items():
            for tf, bars in tf_map.items():
                for bar in bars:
                    timestamp_ms = bar.time * 1000
                    # Check for existing candle to avoid duplicates.
                    exists = (
                        db.query(Candle.id)
                        .filter(
                            Candle.pair == pair,
                            Candle.timeframe == tf,
                            Candle.timestamp_ms == timestamp_ms,
                        )
                        .first()
                    )
                    if exists:
                        continue

                    row = Candle(
                        pair=pair,
                        timeframe=tf,
                        timestamp_ms=timestamp_ms,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=float(bar.tick_volume),
                    )
                    db.add(row)
                    inserted += 1

                # Commit per pair/timeframe to avoid huge transactions.
                db.commit()
                logger.info(
                    "candles_stored",
                    pair=pair,
                    timeframe=tf,
                    new_rows=inserted,
                )

    return inserted


# ---------------------------------------------------------------------------
# Step 3: Build MarketSnapshot from historical candles
# ---------------------------------------------------------------------------


def _bars_to_ohlcv(bars: list[RateBar]) -> list[OHLCV]:
    """Convert RateBar list to OHLCV list."""
    return [
        OHLCV(
            open=b.open,
            high=b.high,
            low=b.low,
            close=b.close,
            volume=float(b.tick_volume),
        )
        for b in bars
    ]


def _find_bars_up_to(
    bars: list[RateBar],
    cutoff_time: int,
    count: int,
) -> list[RateBar]:
    """Return the last *count* bars with bar.time <= cutoff_time."""
    eligible = [b for b in bars if b.time <= cutoff_time]
    return eligible[-count:] if len(eligible) >= count else eligible


def build_snapshot(
    pair: str,
    h1_time: int,
    all_bars: dict[str, list[RateBar]],
) -> MarketSnapshot | None:
    """Build a MarketSnapshot aligned to a specific H1 candle timestamp.

    Returns None if any timeframe has insufficient candles.
    """
    m5 = _find_bars_up_to(all_bars["M5"], h1_time, _MIN_CANDLES["M5"])
    m15 = _find_bars_up_to(all_bars["M15"], h1_time, _MIN_CANDLES["M15"])
    h1 = _find_bars_up_to(all_bars["H1"], h1_time, _MIN_CANDLES["H1"])
    h4 = _find_bars_up_to(all_bars["H4"], h1_time, _MIN_CANDLES["H4"])

    if (
        len(m5) < _MIN_CANDLES["M5"]
        or len(m15) < _MIN_CANDLES["M15"]
        or len(h1) < _MIN_CANDLES["H1"]
        or len(h4) < _MIN_CANDLES["H4"]
    ):
        return None

    candle_map = CandleMap(
        M5=_bars_to_ohlcv(m5),
        M15=_bars_to_ohlcv(m15),
        H1=_bars_to_ohlcv(h1),
        H4=_bars_to_ohlcv(h4),
    )

    dt = datetime.fromtimestamp(h1_time, tz=timezone.utc)
    session = classify_session(dt.hour)

    return MarketSnapshot(
        pair=pair,
        timestamp=h1_time * 1000,  # unix ms
        candles=candle_map,
        spread_points=_DEFAULT_SPREAD_POINTS,
        session=session,
    )


# ---------------------------------------------------------------------------
# Step 4: Simulate trade outcome from subsequent price data
# ---------------------------------------------------------------------------


def simulate_outcome(
    hypothesis: Any,
    m5_bars: list[RateBar],
    signal_time: int,
) -> dict[str, Any] | None:
    """Simulate whether TP or SL was hit using subsequent M5 candles.

    Looks ahead up to _OUTCOME_LOOKAHEAD_M5 bars after signal_time.
    Returns a trade_outcomes dict or None if insufficient data.
    """
    # Find M5 bars after signal time.
    future_bars = [b for b in m5_bars if b.time > signal_time]
    if not future_bars:
        return None

    lookahead = future_bars[:_OUTCOME_LOOKAHEAD_M5]
    entry_price = (hypothesis.entry_zone[0] + hypothesis.entry_zone[1]) / 2.0
    sl = hypothesis.stop_loss
    tp = hypothesis.take_profit
    direction = hypothesis.direction

    exit_price = entry_price  # fallback
    hit_tp = False
    hit_sl = False
    exit_time = signal_time

    for bar in lookahead:
        if direction == Direction.LONG:
            # SL hit: low goes below stop loss
            if bar.low <= sl:
                exit_price = sl
                hit_sl = True
                exit_time = bar.time
                break
            # TP hit: high goes above take profit
            if bar.high >= tp:
                exit_price = tp
                hit_tp = True
                exit_time = bar.time
                break
        else:  # SHORT
            # SL hit: high goes above stop loss
            if bar.high >= sl:
                exit_price = sl
                hit_sl = True
                exit_time = bar.time
                break
            # TP hit: low goes below take profit
            if bar.low <= tp:
                exit_price = tp
                hit_tp = True
                exit_time = bar.time
                break

    # If neither hit, use last bar's close as exit (timed out).
    if not hit_tp and not hit_sl:
        exit_price = lookahead[-1].close
        exit_time = lookahead[-1].time

    # Calculate R-multiple.
    sl_distance = abs(entry_price - sl)
    if sl_distance == 0:
        return None

    if direction == Direction.LONG:
        r_multiple = (exit_price - entry_price) / sl_distance
    else:
        r_multiple = (entry_price - exit_price) / sl_distance

    opened_dt = datetime.fromtimestamp(signal_time, tz=timezone.utc)
    closed_dt = datetime.fromtimestamp(exit_time, tz=timezone.utc)
    session_label = classify_session(opened_dt.hour)

    return {
        "pair": hypothesis.pair,
        "strategy": hypothesis.strategy.value,
        "regime": hypothesis.regime.value,
        "session": session_label.value,
        "direction": direction.value,
        "entry_price": round(entry_price, 5),
        "exit_price": round(exit_price, 5),
        "r_multiple": round(r_multiple, 4),
        "won": r_multiple > 0,
        "fill_id": None,
        "opened_at": opened_dt,
        "closed_at": closed_dt,
    }


# ---------------------------------------------------------------------------
# Step 5: Run the full pipeline
# ---------------------------------------------------------------------------


def run_pipeline(
    all_candles: dict[str, dict[str, list[RateBar]]],
) -> list[dict[str, Any]]:
    """Walk historical H1 candles and generate synthetic trade outcomes.

    For each pair, iterates over H1 candles (after the first 200),
    builds a snapshot, runs Feature Fabric → Regime → Alpha, and
    simulates the trade outcome.
    """
    fabric = FeatureFabric(
        spread_max_points=_SPREAD_MAX_POINTS,
        redis_client=None,  # no Redis for historical — news_blackout always False
    )
    classifier = RegimeClassifier()
    momentum = MomentumEngine()
    mean_reversion = MeanReversionEngine()

    outcomes: list[dict[str, Any]] = []
    stats = {
        "snapshots": 0,
        "features_computed": 0,
        "regimes": defaultdict(int),
        "signals_generated": 0,
        "outcomes_simulated": 0,
        "momentum_signals": 0,
        "mr_signals": 0,
    }

    for pair in PAIRS:
        bars = all_candles[pair]
        h1_bars = bars.get("H1", [])

        if len(h1_bars) < _MIN_CANDLES["H1"]:
            logger.warning(
                "insufficient_h1",
                pair=pair,
                count=len(h1_bars),
            )
            continue

        # Walk from the 200th bar onward (first valid window).
        for i in range(_MIN_CANDLES["H1"] - 1, len(h1_bars)):
            h1_bar = h1_bars[i]

            snapshot = build_snapshot(pair, h1_bar.time, bars)
            if snapshot is None:
                continue
            stats["snapshots"] += 1

            # Feature Fabric.
            try:
                fv = fabric.compute(snapshot)
            except ValueError:
                continue
            stats["features_computed"] += 1

            # Regime Classifier.
            close_price = h1_bar.close
            regime = classifier.classify(fv, close_price)
            stats["regimes"][regime.value] += 1

            if regime == Regime.UNDEFINED:
                continue

            # Alpha Engines.
            hypothesis = None
            if regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
                hypothesis = momentum.generate(fv, regime, snapshot)
                if hypothesis:
                    stats["momentum_signals"] += 1
            elif regime == Regime.RANGING:
                hypothesis = mean_reversion.generate(fv, regime, snapshot)
                if hypothesis:
                    stats["mr_signals"] += 1

            if hypothesis is None:
                continue
            stats["signals_generated"] += 1

            # Simulate trade outcome.
            outcome = simulate_outcome(
                hypothesis,
                bars.get("M5", []),
                h1_bar.time,
            )
            if outcome is None:
                continue

            outcomes.append(outcome)
            stats["outcomes_simulated"] += 1

        logger.info(
            "pair_complete",
            pair=pair,
            outcomes=sum(1 for o in outcomes if o["pair"] == pair),
        )

    # Print pipeline stats.
    print(f"\n{_BOLD}Pipeline Statistics:{_RESET}")
    print(f"  Snapshots built:     {stats['snapshots']:>6}")
    print(f"  Features computed:   {stats['features_computed']:>6}")
    print("  Regime breakdown:")
    for regime, count in sorted(stats["regimes"].items()):
        print(f"    {regime:<16}   {count:>6}")
    print(f"  Signals generated:   {stats['signals_generated']:>6}")
    print(f"    Momentum:          {stats['momentum_signals']:>6}")
    print(f"    Mean Reversion:    {stats['mr_signals']:>6}")
    print(f"  Outcomes simulated:  {stats['outcomes_simulated']:>6}")

    return outcomes


# ---------------------------------------------------------------------------
# Step 6: Store trade outcomes
# ---------------------------------------------------------------------------


def store_outcomes(
    session_factory: Any,
    outcomes: list[dict[str, Any]],
) -> int:
    """Bulk-insert simulated trade outcomes into ``trade_outcomes``."""
    inserted = 0
    try:
        with session_factory() as db:
            for outcome in outcomes:
                row = TradeOutcome(
                    pair=outcome["pair"],
                    strategy=outcome["strategy"],
                    regime=outcome["regime"],
                    session=outcome["session"],
                    direction=outcome["direction"],
                    entry_price=outcome["entry_price"],
                    exit_price=outcome["exit_price"],
                    r_multiple=outcome["r_multiple"],
                    won=outcome["won"],
                    fill_id=None,  # historical — no real fill
                    opened_at=outcome["opened_at"],
                    closed_at=outcome["closed_at"],
                )
                db.add(row)
                inserted += 1
            db.commit()
    except Exception:
        logger.critical("store_outcomes_failed", exc_info=True)
        return 0
    return inserted


# ---------------------------------------------------------------------------
# Step 7: Segment summary
# ---------------------------------------------------------------------------


def print_segment_summary(outcomes: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Print segment breakdown and return list of thin segments."""
    segments: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for o in outcomes:
        key = (o["strategy"], o["regime"], o["session"])
        segments[key].append(o)

    print(f"\n{_BOLD}{'=' * 80}")
    print("  Segment Summary: strategy x regime x session")
    print(f"{'=' * 80}{_RESET}\n")
    print(f"  {'Strategy':<18} {'Regime':<16} {'Session':<10} {'Count':>6}  {'Win%':>6}  {'AvgR':>7}  Status")
    print(f"  {'─' * 18} {'─' * 16} {'─' * 10} {'─' * 6}  {'─' * 6}  {'─' * 7}  {'─' * 14}")

    thin_segments: list[tuple[str, str, str]] = []

    for (strat, regime, session), trades in sorted(segments.items()):
        count = len(trades)
        wins = sum(1 for t in trades if t["won"])
        win_pct = (wins / count * 100) if count > 0 else 0.0
        avg_r = sum(t["r_multiple"] for t in trades) / count if count > 0 else 0.0

        if count < _MIN_SEGMENT:
            flag = f"{_RED}< 30 — BLOCKED{_RESET}"
            thin_segments.append((strat, regime, session))
        else:
            flag = f"{_GREEN}OK{_RESET}"

        print(f"  {strat:<18} {regime:<16} {session:<10} {count:>6}  {win_pct:>5.1f}%  {avg_r:>+7.2f}  {flag}")

    total = len(segments)
    blocked = len(thin_segments)
    ok = total - blocked

    print(f"\n  Segments: {total}  |  {_GREEN}Ready: {ok}{_RESET}  |  {_RED}Blocked (< 30): {blocked}{_RESET}")

    if thin_segments:
        print(f"\n  {_YELLOW}Thin segments (< 30 trades):{_RESET}")
        for strat, regime, session in thin_segments:
            print(f"    - {strat} / {regime} / {session}")
        print(f"\n  {_RED}These segments cannot pass the calibration gate until more trades accumulate.{_RESET}")

    return thin_segments


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-warm APEX V4 database with historical MT5 data.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=120,
        help="Number of days of historical data to fetch (default: 120)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run pipeline without writing to database.",
    )
    args = parser.parse_args()

    print(f"\n{_BOLD}APEX V4 — Historical Data Bootstrap{_RESET}")
    print(f"Period: {args.days} days  |  Pairs: {', '.join(PAIRS)}")
    print(f"Timeframes: {', '.join(TIMEFRAMES)}\n")

    # ── 1. Connect to MT5 ──────────────────────────────────────────────
    print("Step 1: Connecting to MT5...")
    mt5 = RealMT5Client()
    if not mt5.initialize():
        print(f"{_RED}MT5 initialization failed. Is the terminal running?{_RESET}")
        sys.exit(1)
    print(f"  {_GREEN}MT5 connected.{_RESET}")

    try:
        # ── 2. Fetch candles ───────────────────────────────────────────
        print("\nStep 2: Fetching historical candles...")
        t0 = time.monotonic()
        all_candles = fetch_all_candles(mt5, args.days)
        elapsed = time.monotonic() - t0
        total_bars = sum(len(bars) for tf_map in all_candles.values() for bars in tf_map.values())
        print(f"  Fetched {total_bars:,} bars in {elapsed:.1f}s")

    finally:
        mt5.shutdown()
        print("  MT5 disconnected.")

    # ── 3. Store candles in PostgreSQL ─────────────────────────────────
    if not args.dry_run:
        print("\nStep 3: Storing candles in PostgreSQL...")
        sf = make_session_factory()
        new_rows = store_candles(sf, all_candles)
        print(f"  {_GREEN}Inserted {new_rows:,} new candle rows.{_RESET}")
    else:
        print(f"\nStep 3: {_YELLOW}[DRY RUN] Skipping candle storage.{_RESET}")

    # ── 4. Run signal pipeline ─────────────────────────────────────────
    print("\nStep 4: Running signal pipeline over historical data...")
    t0 = time.monotonic()
    outcomes = run_pipeline(all_candles)
    elapsed = time.monotonic() - t0
    print(f"\n  Pipeline completed in {elapsed:.1f}s")
    print(f"  Total trade outcomes: {len(outcomes)}")

    if not outcomes:
        print(f"\n{_YELLOW}No trade outcomes generated. Check ADX thresholds and data quality.{_RESET}")
        sys.exit(0)

    # ── 5. Store trade outcomes ────────────────────────────────────────
    if not args.dry_run:
        print("\nStep 5: Storing trade outcomes in PostgreSQL...")
        sf = make_session_factory()
        inserted = store_outcomes(sf, outcomes)
        print(f"  {_GREEN}Inserted {inserted:,} trade outcomes.{_RESET}")
    else:
        print(f"\nStep 5: {_YELLOW}[DRY RUN] Skipping outcome storage.{_RESET}")

    # ── 6. Segment summary ─────────────────────────────────────────────
    print("\nStep 6: Segment analysis...")
    thin = print_segment_summary(outcomes)

    # ── Final report ───────────────────────────────────────────────────
    print(f"\n{_BOLD}{'=' * 80}")
    print("  Bootstrap Complete")
    print(f"{'=' * 80}{_RESET}")
    print(f"  Total outcomes:  {len(outcomes)}")
    print(f"  Thin segments:   {len(thin)}")

    if thin:
        print(f"\n  {_YELLOW}Action required: {len(thin)} segment(s) still below 30 trades.{_RESET}")
        print("  These will need live/paper trading to reach the minimum.\n")
    else:
        print(f"\n  {_GREEN}All segments meet the 30-trade minimum. Calibration engine is fully primed.{_RESET}\n")


if __name__ == "__main__":
    main()
