#!/usr/bin/env python3
"""scripts/migrate_v3_data.py — Import V3 historical trades into V4 trade_outcomes.

Phase 6 (P6.1).
Reads V3 paper trades from Redis / JSON fallback files, enriches with
signal context from V3 PostgreSQL when available, maps to V4 schema,
and bulk-inserts via PerformanceDatabase.bootstrap_from_v3.

Data sources (tried in order):
  1. Redis  — ``apex:paper_trades`` key (V3 primary store)
  2. JSON   — ``data/paper_trades.json``, ``data/output/paper_trades.json``
  3. V3 DB  — ``signals`` table for regime/setup enrichment (optional)

Mapping rules:
  strategy  → MOMENTUM if V3 setup is TREND_CONTINUATION or LIQUIDITY_SWEEP_REVERSAL,
              MEAN_REVERSION if V3 setup is MEAN_REVERSION.
              Fallback: RANGING regime → MEAN_REVERSION, else MOMENTUM.
  regime    → TRENDING + LONG → TRENDING_UP, TRENDING + SHORT → TRENDING_DOWN,
              RANGING → RANGING, else UNDEFINED.
  session   → classify from opened_at UTC hour (mirrors src/market/feed.py).
  r_multiple→ direction-aware: LONG = (exit−entry)/risk, SHORT = (entry−exit)/risk.
  won       → r_multiple > 0.
  mode      → "v3_historical" (logged, not persisted — no mode column in schema).

Usage:
    python scripts/migrate_v3_data.py [--v3-dir ~/Desktop/apex_v3] [--dry-run]

Env vars:
    V3_REDIS_URL       Redis URL for V3 paper trades  (default: redis://localhost:6379/0)
    V3_DATABASE_URL    V3 PostgreSQL for signal enrichment (optional)
    APEX_DATABASE_URL  V4 PostgreSQL target
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# V3 → V4 mapping tables
# ---------------------------------------------------------------------------

_V3_REGIME_MAP: dict[str, dict[str, str]] = {
    "TRENDING":      {"LONG": "TRENDING_UP", "SHORT": "TRENDING_DOWN"},
    "RANGING":       {"LONG": "RANGING",     "SHORT": "RANGING"},
    "TRANSITIONING": {"LONG": "UNDEFINED",   "SHORT": "UNDEFINED"},
    "MANIPULATED":   {"LONG": "UNDEFINED",   "SHORT": "UNDEFINED"},
}

_V3_SETUP_MAP: dict[str, str] = {
    "TREND_CONTINUATION":       "MOMENTUM",
    "LIQUIDITY_SWEEP_REVERSAL": "MOMENTUM",
    "MEAN_REVERSION":           "MEAN_REVERSION",
}


# ---------------------------------------------------------------------------
# Session classifier — mirrors src/market/feed.py exactly
# ---------------------------------------------------------------------------

def classify_session(utc_hour: int) -> str:
    """Map UTC hour (0-23) to V4 TradingSession value."""
    if 12 <= utc_hour < 16:
        return "OVERLAP"
    if 7 <= utc_hour < 12:
        return "LONDON"
    if 16 <= utc_hour < 21:
        return "NY"
    return "ASIA"


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_from_redis(redis_url: str) -> list[dict[str, Any]]:
    """Load V3 paper trades from Redis key ``apex:paper_trades``."""
    try:
        import redis as redis_lib
    except ImportError:
        print("  Redis: redis-py not installed, skipping")
        return []

    try:
        r = redis_lib.from_url(redis_url, decode_responses=True)
        raw: str | None = r.get("apex:paper_trades")
        if raw is None:
            print("  Redis: key 'apex:paper_trades' not found")
            return []
        trades: list[dict[str, Any]] = json.loads(raw)
        print(f"  Redis: loaded {len(trades)} trades")
        return trades
    except Exception as exc:
        print(f"  Redis: connection failed — {exc}")
        return []


def load_from_json(v3_dir: Path) -> list[dict[str, Any]]:
    """Load V3 paper trades from JSON fallback files, deduplicating by paper_id."""
    candidates = [
        v3_dir / "data" / "paper_trades.json",
        v3_dir / "data" / "output" / "paper_trades.json",
    ]
    all_trades: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for path in candidates:
        if not path.exists():
            print(f"  JSON: {path} — not found")
            continue
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, list):
                print(f"  JSON: {path} — not a list, skipping")
                continue
            count = 0
            for trade in data:
                pid = trade.get("paper_id", "")
                if pid and pid not in seen_ids:
                    all_trades.append(trade)
                    seen_ids.add(pid)
                    count += 1
            print(f"  JSON: {path} — {len(data)} records, {count} new after dedup")
        except Exception as exc:
            print(f"  JSON: {path} — read failed: {exc}")

    return all_trades


def load_v3_signal_context(db_url: str) -> dict[str, dict[str, str]]:
    """Load regime from V3 PostgreSQL signals table for enrichment.

    Returns lookup keyed by ``pair|direction|entry_price_5dp`` → {regime}.
    """
    try:
        import psycopg2
    except ImportError:
        print("  V3 DB: psycopg2 not installed, skipping enrichment")
        return {}

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute(
            "SELECT pair, direction, entry_price, daily_regime "
            "FROM signals "
            "WHERE direction IN ('LONG', 'SHORT')"
        )
        rows = cur.fetchall()
        conn.close()

        lookup: dict[str, dict[str, str]] = {}
        for pair, direction, entry_price, regime in rows:
            key = f"{pair}|{direction}|{float(entry_price):.5f}"
            lookup[key] = {"regime": regime or ""}
        print(f"  V3 DB: loaded {len(lookup)} signal records for enrichment")
        return lookup
    except Exception as exc:
        print(f"  V3 DB: connection failed — {exc}")
        return {}


# ---------------------------------------------------------------------------
# Mapping logic
# ---------------------------------------------------------------------------

def _infer_strategy(v3_regime: str) -> str:
    """Infer V4 strategy from V3 regime when no setup_type is available.

    RANGING → MEAN_REVERSION, everything else → MOMENTUM.
    """
    if v3_regime == "RANGING":
        return "MEAN_REVERSION"
    return "MOMENTUM"


def _map_regime(v3_regime: str, direction: str) -> str:
    """Map V3 regime label + direction to V4 Regime enum."""
    regime_variants = _V3_REGIME_MAP.get(v3_regime, {})
    return regime_variants.get(direction, "UNDEFINED")


def _parse_dt(iso_str: str) -> datetime:
    """Parse ISO 8601 string, ensuring UTC timezone."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def map_trade(
    trade: dict[str, Any],
    signal_lookup: dict[str, dict[str, str]],
) -> dict[str, Any] | None:
    """Map a single V3 paper trade to V4 trade_outcomes dict.

    Returns None for non-closed trades or trades with invalid data.
    """
    if trade.get("status") != "CLOSED":
        return None

    pair = trade.get("pair", "")
    direction = trade.get("signal", "")
    entry_price = trade.get("entry_price", 0.0)
    stop_loss = trade.get("stop_loss", 0.0)

    if not pair or direction not in ("LONG", "SHORT") or entry_price == 0:
        return None

    risk = abs(entry_price - stop_loss)
    if risk == 0:
        return None

    # ── Timestamps ──
    opened_str = trade.get("opened_at", "")
    closed_str = trade.get("closed_at", "")
    if not opened_str or not closed_str:
        return None

    opened_at = _parse_dt(opened_str)
    closed_at = _parse_dt(closed_str)

    # ── Exit price from r_achieved ──
    r_achieved = trade.get("r_achieved", 0.0)
    if direction == "LONG":
        exit_price = entry_price + r_achieved * risk
    else:
        exit_price = entry_price - r_achieved * risk

    # ── R-multiple (direction-aware, positive = win) ──
    if direction == "LONG":
        r_multiple = (exit_price - entry_price) / risk
    else:
        r_multiple = (entry_price - exit_price) / risk

    # ── Enrichment from V3 DB (best-effort) ──
    lookup_key = f"{pair}|{direction}|{entry_price:.5f}"
    v3_ctx = signal_lookup.get(lookup_key, {})
    v3_regime = v3_ctx.get("regime", "")

    # ── Strategy ──
    strategy = _infer_strategy(v3_regime) if v3_regime else "MOMENTUM"

    # ── Regime ──
    regime = _map_regime(v3_regime, direction) if v3_regime else "UNDEFINED"

    # ── Session ──
    session = classify_session(opened_at.hour)

    return {
        "pair": pair,
        "strategy": strategy,
        "regime": regime,
        "session": session,
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": round(exit_price, 5),
        "r_multiple": round(r_multiple, 4),
        "won": r_multiple > 0,
        "fill_id": None,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "mode": "v3_historical",
    }


# ---------------------------------------------------------------------------
# Segment analysis
# ---------------------------------------------------------------------------

_MIN_SEGMENT = 30
_RED = "\033[91m"
_GREEN = "\033[92m"
_BOLD = "\033[1m"
_RESET = "\033[0m"


def print_segment_breakdown(mapped: list[dict[str, Any]]) -> None:
    """Print strategy x regime x session grid, flagging thin segments in red."""
    segments: dict[tuple[str, str, str], int] = defaultdict(int)
    for t in mapped:
        key = (t["strategy"], t["regime"], t["session"])
        segments[key] += 1

    print(f"\n{_BOLD}{'═' * 72}")
    print(f"  Segment Breakdown: strategy × regime × session")
    print(f"{'═' * 72}{_RESET}\n")
    print(f"  {'Strategy':<18} {'Regime':<16} {'Session':<10} {'Count':>6}  Status")
    print(f"  {'─' * 18} {'─' * 16} {'─' * 10} {'─' * 6}  {'─' * 14}")

    for (strat, regime, session), count in sorted(segments.items()):
        if count < _MIN_SEGMENT:
            flag = f"{_RED}< 30 — BLOCKED{_RESET}"
        else:
            flag = f"{_GREEN}OK{_RESET}"
        print(f"  {strat:<18} {regime:<16} {session:<10} {count:>6}  {flag}")

    total = len(segments)
    blocked = sum(1 for c in segments.values() if c < _MIN_SEGMENT)
    ok = total - blocked

    print(f"\n  Segments: {total}  |  "
          f"{_GREEN}Ready: {ok}{_RESET}  |  "
          f"{_RED}Blocked (< 30): {blocked}{_RESET}")

    if blocked:
        print(f"\n  {_RED}Blocked segments cannot trade in V4 "
              f"until the live sample reaches 30.{_RESET}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate V3 paper-trade history into V4 trade_outcomes.",
    )
    parser.add_argument(
        "--v3-dir",
        type=Path,
        default=Path.home() / "Desktop" / "apex_v3",
        help="Path to APEX V3 repository (default: ~/Desktop/apex_v3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Map and analyze without writing to V4 database.",
    )
    args = parser.parse_args()

    v3_dir: Path = args.v3_dir.expanduser().resolve()
    redis_url = os.environ.get("V3_REDIS_URL", "redis://localhost:6379/0")
    v3_db_url = os.environ.get("V3_DATABASE_URL", "")

    print(f"\n{_BOLD}APEX V4 — V3 Data Migration (Phase 6){_RESET}")
    print(f"V3 directory: {v3_dir}\n")

    # ── 1. Load V3 trades ──────────────────────────────────────────────
    print("Step 1: Loading V3 trade data...")
    trades: list[dict[str, Any]] = []

    redis_trades = load_from_redis(redis_url)
    trades.extend(redis_trades)

    json_trades = load_from_json(v3_dir)
    seen = {t.get("paper_id") for t in trades}
    for t in json_trades:
        if t.get("paper_id") not in seen:
            trades.append(t)
            seen.add(t.get("paper_id"))

    total = len(trades)
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    open_count = sum(1 for t in trades if t.get("status") == "OPEN")

    print(f"\n  Total V3 trades: {total}  "
          f"(closed: {len(closed)}, open: {open_count})")

    if not closed:
        print(f"\n{_RED}No closed V3 trades found. Nothing to migrate.{_RESET}")
        print("Ensure Redis is running or paper_trades.json contains data.")
        sys.exit(0)

    # ── 2. Enrichment from V3 DB (optional) ────────────────────────────
    signal_lookup: dict[str, dict[str, str]] = {}
    if v3_db_url:
        print("\nStep 2: Enriching from V3 PostgreSQL signals table...")
        signal_lookup = load_v3_signal_context(v3_db_url)
    else:
        print("\nStep 2: V3_DATABASE_URL not set — "
              "using heuristic strategy/regime inference.")

    # ── 3. Map to V4 schema ────────────────────────────────────────────
    print("\nStep 3: Mapping V3 trades → V4 trade_outcomes...")
    mapped: list[dict[str, Any]] = []
    skipped = 0
    for trade in trades:
        result = map_trade(trade, signal_lookup)
        if result is not None:
            mapped.append(result)
        else:
            skipped += 1

    print(f"  Mapped: {len(mapped)}  |  Skipped: {skipped}")

    if not mapped:
        print(f"\n{_RED}No trades could be mapped. Check V3 data format.{_RESET}")
        sys.exit(1)

    # ── 4. Segment analysis ────────────────────────────────────────────
    print_segment_breakdown(mapped)
    print(f"\n{_BOLD}Total imported trade count: {len(mapped)}{_RESET}")

    # ── 5. Write to V4 database ────────────────────────────────────────
    if args.dry_run:
        print(f"\n{_BOLD}[DRY RUN] No data written to V4 database.{_RESET}\n")
        return

    apex_db_url = os.environ.get("APEX_DATABASE_URL", "")
    if not apex_db_url:
        print(f"\n{_RED}APEX_DATABASE_URL not set — cannot write to V4.{_RESET}")
        print("Set APEX_DATABASE_URL and re-run, or use --dry-run to preview.\n")
        sys.exit(1)

    # Import V4 modules (deferred so dry-run works without DB deps)
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))

    from src.calibration.history import PerformanceDatabase  # noqa: E402

    print("\nStep 5: Writing to V4 database...")
    perf_db = PerformanceDatabase()
    inserted = perf_db.bootstrap_from_v3(mapped)
    print(f"\n{_GREEN}Successfully inserted {inserted} "
          f"trade outcomes into V4.{_RESET}\n")


if __name__ == "__main__":
    main()
