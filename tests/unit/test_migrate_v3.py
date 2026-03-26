"""Tests for scripts/migrate_v3_data.py — V3 → V4 trade mapping logic."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Ensure project root is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.migrate_v3_data import (
    classify_session,
    load_from_json,
    map_trade,
    print_segment_breakdown,
)


# ---------------------------------------------------------------------------
# Fixtures — synthetic V3 paper trades
# ---------------------------------------------------------------------------

def _v3_trade(
    *,
    pair: str = "EURUSD",
    signal: str = "LONG",
    entry: float = 1.10000,
    sl: float = 1.09500,
    tp1: float = 1.11000,
    tp2: float = 1.12000,
    r_achieved: float = 2.0,
    outcome: str = "WIN",
    status: str = "CLOSED",
    opened_at: str = "2026-03-10T09:30:00+00:00",
    closed_at: str = "2026-03-10T14:00:00+00:00",
    paper_id: str = "PAPER_test0001",
) -> dict:
    return {
        "paper_id": paper_id,
        "pair": pair,
        "signal": signal,
        "entry_price": entry,
        "stop_loss": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": 0,
        "conviction": 82,
        "status": status,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "outcome": outcome,
        "r_achieved": r_achieved,
        "current_max_r": r_achieved,
        "last_updated": closed_at,
    }


# ---------------------------------------------------------------------------
# classify_session
# ---------------------------------------------------------------------------

class TestClassifySession:
    """Session classifier must mirror src/market/feed.py exactly."""

    @pytest.mark.parametrize("hour,expected", [
        (3, "ASIA"),
        (6, "ASIA"),
        (7, "LONDON"),
        (11, "LONDON"),
        (12, "OVERLAP"),
        (15, "OVERLAP"),
        (16, "NY"),
        (20, "NY"),
        (21, "ASIA"),
        (23, "ASIA"),
        (0, "ASIA"),
    ])
    def test_session_boundaries(self, hour: int, expected: str) -> None:
        assert classify_session(hour) == expected


# ---------------------------------------------------------------------------
# map_trade — LONG
# ---------------------------------------------------------------------------

class TestMapTradeLong:
    """LONG trade mapping."""

    def test_winning_long(self) -> None:
        trade = _v3_trade(signal="LONG", entry=1.10000, sl=1.09500,
                          r_achieved=2.0, outcome="WIN")
        result = map_trade(trade, {})
        assert result is not None
        assert result["direction"] == "LONG"
        assert result["won"] is True
        assert result["r_multiple"] == 2.0
        # exit = 1.1 + 2.0 * 0.005 = 1.11
        assert abs(result["exit_price"] - 1.11000) < 1e-5
        assert result["mode"] == "v3_historical"

    def test_losing_long(self) -> None:
        trade = _v3_trade(signal="LONG", entry=1.10000, sl=1.09500,
                          r_achieved=-1.0, outcome="LOSS")
        result = map_trade(trade, {})
        assert result is not None
        assert result["won"] is False
        assert result["r_multiple"] == -1.0
        # exit = 1.1 + (-1.0) * 0.005 = 1.095
        assert abs(result["exit_price"] - 1.09500) < 1e-5


# ---------------------------------------------------------------------------
# map_trade — SHORT
# ---------------------------------------------------------------------------

class TestMapTradeShort:
    """SHORT trade mapping."""

    def test_winning_short(self) -> None:
        trade = _v3_trade(signal="SHORT", entry=1.10000, sl=1.10500,
                          r_achieved=2.0, outcome="WIN")
        result = map_trade(trade, {})
        assert result is not None
        assert result["direction"] == "SHORT"
        assert result["won"] is True
        assert result["r_multiple"] == 2.0
        # exit = 1.1 - 2.0 * 0.005 = 1.09
        assert abs(result["exit_price"] - 1.09000) < 1e-5

    def test_losing_short(self) -> None:
        trade = _v3_trade(signal="SHORT", entry=1.10000, sl=1.10500,
                          r_achieved=-1.0, outcome="LOSS")
        result = map_trade(trade, {})
        assert result is not None
        assert result["won"] is False
        assert result["r_multiple"] == -1.0


# ---------------------------------------------------------------------------
# map_trade — edge cases / filtering
# ---------------------------------------------------------------------------

class TestMapTradeEdgeCases:
    """Trades that should be skipped or handled specially."""

    def test_open_trade_skipped(self) -> None:
        trade = _v3_trade(status="OPEN")
        assert map_trade(trade, {}) is None

    def test_no_pair_skipped(self) -> None:
        trade = _v3_trade(pair="")
        assert map_trade(trade, {}) is None

    def test_invalid_direction_skipped(self) -> None:
        trade = _v3_trade(signal="HOLD")
        assert map_trade(trade, {}) is None

    def test_zero_risk_skipped(self) -> None:
        trade = _v3_trade(entry=1.10000, sl=1.10000)
        assert map_trade(trade, {}) is None

    def test_missing_timestamps_skipped(self) -> None:
        trade = _v3_trade(opened_at="", closed_at="")
        assert map_trade(trade, {}) is None

    def test_breakeven_is_not_won(self) -> None:
        trade = _v3_trade(r_achieved=0.0, outcome="BREAKEVEN")
        result = map_trade(trade, {})
        assert result is not None
        assert result["won"] is False
        assert result["r_multiple"] == 0.0

    def test_naive_timestamp_gets_utc(self) -> None:
        """Timestamps without TZ info default to UTC."""
        trade = _v3_trade(opened_at="2026-03-10T09:30:00",
                          closed_at="2026-03-10T14:00:00")
        result = map_trade(trade, {})
        assert result is not None
        assert result["opened_at"].tzinfo is not None


# ---------------------------------------------------------------------------
# map_trade — enrichment from V3 DB
# ---------------------------------------------------------------------------

class TestMapTradeEnrichment:
    """V3 DB enrichment paths."""

    def test_ranging_regime_maps_to_mean_reversion(self) -> None:
        trade = _v3_trade(signal="LONG", entry=1.10000, sl=1.09500)
        lookup = {"EURUSD|LONG|1.10000": {"regime": "RANGING"}}
        result = map_trade(trade, lookup)
        assert result is not None
        assert result["strategy"] == "MEAN_REVERSION"
        assert result["regime"] == "RANGING"

    def test_trending_long_maps_to_trending_up(self) -> None:
        trade = _v3_trade(signal="LONG", entry=1.10000, sl=1.09500)
        lookup = {"EURUSD|LONG|1.10000": {"regime": "TRENDING"}}
        result = map_trade(trade, lookup)
        assert result is not None
        assert result["strategy"] == "MOMENTUM"
        assert result["regime"] == "TRENDING_UP"

    def test_trending_short_maps_to_trending_down(self) -> None:
        trade = _v3_trade(signal="SHORT", entry=1.10000, sl=1.10500)
        lookup = {"EURUSD|SHORT|1.10000": {"regime": "TRENDING"}}
        result = map_trade(trade, lookup)
        assert result is not None
        assert result["strategy"] == "MOMENTUM"
        assert result["regime"] == "TRENDING_DOWN"

    def test_no_enrichment_defaults_momentum_undefined(self) -> None:
        trade = _v3_trade()
        result = map_trade(trade, {})
        assert result is not None
        assert result["strategy"] == "MOMENTUM"
        assert result["regime"] == "UNDEFINED"


# ---------------------------------------------------------------------------
# Session from opened_at
# ---------------------------------------------------------------------------

class TestSessionFromTimestamp:
    """Session should be derived from opened_at UTC hour."""

    def test_london_session(self) -> None:
        trade = _v3_trade(opened_at="2026-03-10T09:30:00+00:00")
        result = map_trade(trade, {})
        assert result is not None
        assert result["session"] == "LONDON"

    def test_overlap_session(self) -> None:
        trade = _v3_trade(opened_at="2026-03-10T13:00:00+00:00")
        result = map_trade(trade, {})
        assert result is not None
        assert result["session"] == "OVERLAP"

    def test_ny_session(self) -> None:
        trade = _v3_trade(opened_at="2026-03-10T17:00:00+00:00")
        result = map_trade(trade, {})
        assert result is not None
        assert result["session"] == "NY"

    def test_asia_session(self) -> None:
        trade = _v3_trade(opened_at="2026-03-10T03:00:00+00:00")
        result = map_trade(trade, {})
        assert result is not None
        assert result["session"] == "ASIA"


# ---------------------------------------------------------------------------
# print_segment_breakdown (smoke test — just ensure no crash)
# ---------------------------------------------------------------------------

class TestSegmentBreakdown:
    """Segment breakdown printer should not raise."""

    def test_prints_without_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        trades = [
            _v3_trade(paper_id=f"PAPER_{i:04d}")
            for i in range(35)
        ]
        mapped = [map_trade(t, {}) for t in trades]
        mapped = [m for m in mapped if m is not None]
        print_segment_breakdown(mapped)
        out = capsys.readouterr().out
        assert "Segment Breakdown" in out
        assert "MOMENTUM" in out

    def test_thin_segment_flagged(self, capsys: pytest.CaptureFixture[str]) -> None:
        trades = [_v3_trade(paper_id=f"PAPER_{i:04d}") for i in range(5)]
        mapped = [map_trade(t, {}) for t in trades]
        mapped = [m for m in mapped if m is not None]
        print_segment_breakdown(mapped)
        out = capsys.readouterr().out
        assert "BLOCKED" in out


# ---------------------------------------------------------------------------
# load_from_json with temp files
# ---------------------------------------------------------------------------

class TestLoadFromJson:
    """JSON loader reads and deduplicates."""

    def test_loads_from_file(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        trades = [_v3_trade(paper_id="PAPER_a"), _v3_trade(paper_id="PAPER_b")]
        (data_dir / "paper_trades.json").write_text(json.dumps(trades))

        # Need output dir too
        output_dir = data_dir / "output"
        output_dir.mkdir()
        (output_dir / "paper_trades.json").write_text("[]")

        result = load_from_json(tmp_path)
        assert len(result) == 2

    def test_deduplicates_across_files(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        output_dir = data_dir / "output"
        output_dir.mkdir()

        trade = _v3_trade(paper_id="PAPER_dup")
        (data_dir / "paper_trades.json").write_text(json.dumps([trade]))
        (output_dir / "paper_trades.json").write_text(json.dumps([trade]))

        result = load_from_json(tmp_path)
        assert len(result) == 1


# Need json import for the JSON loader tests
import json  # noqa: E402
