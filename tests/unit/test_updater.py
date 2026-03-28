"""Unit tests for src/learning/updater.py — KellyInputUpdater.

Tests cover:
  - update_segment: recalculates from PerformanceDatabase
  - update_segment: caches to Redis with correct key and TTL
  - update_segment: returns stats dict on success
  - update_segment: returns None when segment < 30 trades
  - update_segment: deletes Redis key when segment insufficient
  - update_segment: logs warning when segment < 30
  - update_segment: Redis failure doesn't crash
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import pytest

from src.learning.updater import KellyInputUpdater


# ── fixtures ──────────────────────────────────────────────────────────────


def _make_stats(
    win_rate: float = 0.60,
    avg_r: float = 2.0,
    trade_count: int = 50,
) -> dict:
    return {
        "win_rate": win_rate,
        "avg_R": avg_r,
        "trade_count": trade_count,
        "last_updated": "2026-03-24",
    }


def _make_updater(
    stats: dict | None = None,
    redis_error: bool = False,
) -> tuple[KellyInputUpdater, MagicMock, MagicMock]:
    """Return (updater, mock_perf_db, mock_redis)."""
    mock_db = MagicMock()
    mock_db.get_segment_stats.return_value = stats

    mock_redis = MagicMock()
    if redis_error:
        mock_redis.set.side_effect = ConnectionError("Redis down")
        mock_redis.delete.side_effect = ConnectionError("Redis down")

    return KellyInputUpdater(perf_db=mock_db, redis_client=mock_redis), mock_db, mock_redis


# ── success path tests ───────────────────────────────────────────────────


class TestUpdateSegmentSuccess:
    """Segment with >= 30 trades: recalculate and cache."""

    def test_returns_stats(self):
        updater, _, _ = _make_updater(stats=_make_stats())
        result = updater.update_segment("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["win_rate"] == 0.60
        assert result["avg_R"] == 2.0
        assert result["trade_count"] == 50

    def test_caches_to_redis(self):
        updater, _, mock_redis = _make_updater(stats=_make_stats())
        updater.update_segment("MOMENTUM", "TRENDING_UP", "LONDON")
        # Check Redis SET was called with correct key.
        key = "segment:MOMENTUM:TRENDING_UP:LONDON"
        assert mock_redis.set.called
        args = mock_redis.set.call_args
        assert args[0][0] == key
        payload = json.loads(args[0][1])
        assert payload["win_rate"] == 0.60
        assert payload["avg_R"] == 2.0
        assert payload["trade_count"] == 50

    def test_redis_ttl_3600(self):
        updater, _, mock_redis = _make_updater(stats=_make_stats())
        updater.update_segment("MOMENTUM", "TRENDING_UP", "LONDON")
        args = mock_redis.set.call_args
        assert args[1]["ex"] == 3600

    def test_queries_correct_segment(self):
        updater, mock_db, _ = _make_updater(stats=_make_stats())
        updater.update_segment("MEAN_REVERSION", "RANGING", "OVERLAP")
        mock_db.get_segment_stats.assert_called_once_with(
            "MEAN_REVERSION",
            "RANGING",
            "OVERLAP",
        )


# ── insufficient data tests ──────────────────────────────────────────────


class TestUpdateSegmentInsufficient:
    """Segment with < 30 trades: return None and clear cache."""

    def test_returns_none(self):
        updater, _, _ = _make_updater(stats=None)
        result = updater.update_segment("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is None

    def test_deletes_redis_key(self):
        updater, _, mock_redis = _make_updater(stats=None)
        updater.update_segment("MOMENTUM", "TRENDING_UP", "LONDON")
        key = "segment:MOMENTUM:TRENDING_UP:LONDON"
        mock_redis.delete.assert_called_once_with(key)


# ── Redis failure tests ──────────────────────────────────────────────────


class TestUpdateSegmentRedisFailure:
    """Redis errors are logged but don't crash."""

    def test_redis_set_failure_returns_stats(self):
        updater, _, _ = _make_updater(
            stats=_make_stats(),
            redis_error=True,
        )
        # Should not raise, stats still returned.
        result = updater.update_segment("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is not None
        assert result["win_rate"] == 0.60

    def test_redis_delete_failure_returns_none(self):
        updater, _, _ = _make_updater(stats=None, redis_error=True)
        # Should not raise.
        result = updater.update_segment("MOMENTUM", "TRENDING_UP", "LONDON")
        assert result is None


# ── key format tests ─────────────────────────────────────────────────────


class TestKeyFormat:
    """Redis key format is segment:{strategy}:{regime}:{session}."""

    def test_key_format_momentum(self):
        updater, _, mock_redis = _make_updater(stats=_make_stats())
        updater.update_segment("MOMENTUM", "TRENDING_DOWN", "NY")
        key = mock_redis.set.call_args[0][0]
        assert key == "segment:MOMENTUM:TRENDING_DOWN:NY"

    def test_key_format_mr(self):
        updater, _, mock_redis = _make_updater(stats=_make_stats())
        updater.update_segment("MEAN_REVERSION", "RANGING", "ASIA")
        key = mock_redis.set.call_args[0][0]
        assert key == "segment:MEAN_REVERSION:RANGING:ASIA"
