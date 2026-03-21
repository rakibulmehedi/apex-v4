"""Unit tests for src/market/feed.py — async MT5 data ingestion.

MT5 is fully mocked via unittest.mock — no real broker, no real ZMQ IPC.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import zmq
import zmq.asyncio

from src.market.feed import (
    MarketFeed,
    _bar_to_ohlcv,
    classify_session,
)
from src.market.mt5_types import RateBar, Tick, TIMEFRAME_MAP
from src.market.schemas import TradingSession


# ── helpers ──────────────────────────────────────────────────────────────

def _make_bars(count: int, *, base_time: int = 1_710_000_000, interval: int = 300) -> list[RateBar]:
    """Generate a list of RateBar objects for testing."""
    return [
        RateBar(
            time=base_time + i * interval,
            open=1.1000 + i * 0.0001,
            high=1.1010 + i * 0.0001,
            low=1.0990 + i * 0.0001,
            close=1.1005 + i * 0.0001,
            tick_volume=100 + i,
        )
        for i in range(count)
    ]


def _make_tick(bid: float = 1.08450, ask: float = 1.08465) -> Tick:
    return Tick(time=int(time.time()), bid=bid, ask=ask, last=0.0, volume=0, flags=0)


def _make_mock_client(
    *,
    bars_m5: list[RateBar] | None = None,
    bars_m15: list[RateBar] | None = None,
    bars_h1: list[RateBar] | None = None,
    bars_h4: list[RateBar] | None = None,
    tick: Tick | None = None,
) -> MagicMock:
    """Create a fully mocked MT5Client."""
    client = MagicMock()

    if bars_m5 is None:
        bars_m5 = _make_bars(50, interval=300)
    if bars_m15 is None:
        bars_m15 = _make_bars(50, interval=900)
    if bars_h1 is None:
        bars_h1 = _make_bars(200, interval=3600)
    if bars_h4 is None:
        bars_h4 = _make_bars(50, interval=14400)
    if tick is None:
        tick = _make_tick()

    def copy_rates(symbol: str, timeframe: int, start_pos: int, count: int):
        if timeframe == TIMEFRAME_MAP["M5"]:
            return bars_m5[:count]
        if timeframe == TIMEFRAME_MAP["M15"]:
            return bars_m15[:count]
        if timeframe == TIMEFRAME_MAP["H1"]:
            return bars_h1[:count]
        if timeframe == TIMEFRAME_MAP["H4"]:
            return bars_h4[:count]
        return None

    client.copy_rates_from_pos.side_effect = copy_rates
    client.symbol_info_tick.return_value = tick
    return client


# ═════════════════════════════════════════════════════════════════════════
# classify_session
# ═════════════════════════════════════════════════════════════════════════

class TestClassifySession:
    def test_asia_early_morning(self):
        for h in (0, 1, 2, 3, 4, 5, 6):
            assert classify_session(h) == TradingSession.ASIA

    def test_london(self):
        for h in (7, 8, 9, 10, 11):
            assert classify_session(h) == TradingSession.LONDON

    def test_overlap(self):
        for h in (12, 13, 14, 15):
            assert classify_session(h) == TradingSession.OVERLAP

    def test_ny(self):
        for h in (16, 17, 18, 19, 20):
            assert classify_session(h) == TradingSession.NY

    def test_asia_late_night(self):
        for h in (21, 22, 23):
            assert classify_session(h) == TradingSession.ASIA

    def test_all_hours_covered(self):
        """Every hour 0-23 returns a valid TradingSession."""
        for h in range(24):
            result = classify_session(h)
            assert isinstance(result, TradingSession)


# ═════════════════════════════════════════════════════════════════════════
# _bar_to_ohlcv
# ═════════════════════════════════════════════════════════════════════════

class TestBarToOhlcv:
    def test_converts_correctly(self):
        bar = RateBar(time=1000, open=1.1, high=1.2, low=1.0, close=1.15, tick_volume=99)
        result = _bar_to_ohlcv(bar)
        assert result == {
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.15,
            "volume": 99.0,
        }

    def test_volume_is_float(self):
        bar = RateBar(time=0, open=0, high=0, low=0, close=0, tick_volume=42)
        assert isinstance(_bar_to_ohlcv(bar)["volume"], float)


# ═════════════════════════════════════════════════════════════════════════
# MarketFeed — candle close detection
# ═════════════════════════════════════════════════════════════════════════

class TestCandleCloseDetection:
    def test_first_poll_no_close(self):
        """First poll only seeds _last_bar_time — no close detected."""
        client = _make_mock_client()
        feed = MarketFeed(client, ["EURUSD"])
        assert feed._has_candle_close("EURUSD") is False

    def test_same_bar_no_close(self):
        """Polling with same bar timestamp → no close."""
        client = _make_mock_client()
        feed = MarketFeed(client, ["EURUSD"])
        feed._has_candle_close("EURUSD")  # seed
        assert feed._has_candle_close("EURUSD") is False

    def test_new_bar_triggers_close(self):
        """When bar timestamp changes, a close is detected."""
        bars_v1 = _make_bars(50, base_time=1_000_000)
        bars_v2 = _make_bars(50, base_time=1_000_300)  # shifted by 1 M5 bar

        client = MagicMock()
        call_count = {"n": 0}

        def copy_rates(symbol, timeframe, start_pos, count):
            call_count["n"] += 1
            # First 3 calls (M5, M15, H1) use v1; next round uses v2 for M5
            if timeframe == TIMEFRAME_MAP["M5"]:
                if call_count["n"] <= 3:
                    return bars_v1[:count]
                return bars_v2[:count]
            return _make_bars(max(count, 200), interval=900)[:count]

        client.copy_rates_from_pos.side_effect = copy_rates
        client.symbol_info_tick.return_value = _make_tick()

        feed = MarketFeed(client, ["EURUSD"])
        feed._has_candle_close("EURUSD")  # seed
        assert feed._has_candle_close("EURUSD") is True

    def test_copy_rates_none_does_not_crash(self):
        """If copy_rates_from_pos returns None, skip gracefully."""
        client = MagicMock()
        client.copy_rates_from_pos.return_value = None
        client.symbol_info_tick.return_value = _make_tick()

        feed = MarketFeed(client, ["EURUSD"])
        # Should not raise.
        assert feed._has_candle_close("EURUSD") is False


# ═════════════════════════════════════════════════════════════════════════
# MarketFeed — snapshot building
# ═════════════════════════════════════════════════════════════════════════

class TestBuildSnapshot:
    def test_builds_valid_snapshot(self):
        client = _make_mock_client()
        feed = MarketFeed(client, ["EURUSD"])
        snap = feed._build_snapshot("EURUSD")
        assert snap is not None
        assert snap.pair == "EURUSD"
        assert snap.spread_points > 0
        assert snap.type == "MarketSnapshot"
        assert len(snap.candles.M5) == 50
        assert len(snap.candles.H1) == 200

    def test_missing_candle_data_returns_none(self):
        """If any timeframe returns None, snapshot is None."""
        client = MagicMock()

        def copy_rates(symbol, timeframe, start_pos, count):
            if timeframe == TIMEFRAME_MAP["H1"]:
                return None
            return _make_bars(count)

        client.copy_rates_from_pos.side_effect = copy_rates
        client.symbol_info_tick.return_value = _make_tick()

        feed = MarketFeed(client, ["EURUSD"])
        assert feed._build_snapshot("EURUSD") is None

    def test_missing_tick_returns_none(self):
        client = _make_mock_client()
        client.symbol_info_tick.return_value = None
        feed = MarketFeed(client, ["EURUSD"])
        assert feed._build_snapshot("EURUSD") is None

    def test_zero_spread_validation_failure(self):
        """A tick with bid==ask gives spread 0 → Pydantic rejects it."""
        client = _make_mock_client(tick=Tick(
            time=int(time.time()), bid=1.1, ask=1.1, last=0.0, volume=0, flags=0,
        ))
        feed = MarketFeed(client, ["EURUSD"])
        snap = feed._build_snapshot("EURUSD")
        assert snap is None
        assert feed.validation_errors == 1

    def test_session_field_is_set(self):
        client = _make_mock_client()
        feed = MarketFeed(client, ["EURUSD"])
        snap = feed._build_snapshot("EURUSD")
        assert snap is not None
        assert snap.session in list(TradingSession)


# ═════════════════════════════════════════════════════════════════════════
# MarketFeed — ZMQ publishing
# ═════════════════════════════════════════════════════════════════════════

class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_sends_json(self):
        """Verify _publish sends JSON payload to ZMQ socket."""
        client = _make_mock_client()
        feed = MarketFeed(client, ["EURUSD"])
        snap = feed._build_snapshot("EURUSD")
        assert snap is not None

        mock_socket = AsyncMock()
        feed._zmq_socket = mock_socket

        await feed._publish(snap)

        mock_socket.send_string.assert_awaited_once()
        payload = mock_socket.send_string.call_args[0][0]
        assert '"pair":"EURUSD"' in payload or '"pair": "EURUSD"' in payload
        assert feed.snapshots_published == 1

    @pytest.mark.asyncio
    async def test_publish_no_socket_logs_error(self):
        """If ZMQ socket is None, _publish logs but doesn't crash."""
        client = _make_mock_client()
        feed = MarketFeed(client, ["EURUSD"])
        feed._zmq_socket = None

        snap = feed._build_snapshot("EURUSD")
        assert snap is not None
        # Should not raise.
        await feed._publish(snap)
        assert feed.snapshots_published == 0


# ═════════════════════════════════════════════════════════════════════════
# MarketFeed — poll_once integration
# ═════════════════════════════════════════════════════════════════════════

class TestPollOnce:
    @pytest.mark.asyncio
    async def test_poll_once_publishes_on_close(self):
        """After seeding, a bar change triggers publish."""
        bars_v1 = _make_bars(200, base_time=1_000_000)
        bars_v2 = _make_bars(200, base_time=1_000_300)

        client = MagicMock()
        round_num = {"n": 0}

        def copy_rates(symbol, timeframe, start_pos, count):
            if timeframe == TIMEFRAME_MAP["M5"] and round_num["n"] >= 1:
                return bars_v2[:count]
            return bars_v1[:count]

        client.copy_rates_from_pos.side_effect = copy_rates
        client.symbol_info_tick.return_value = _make_tick()

        feed = MarketFeed(client, ["EURUSD"])
        mock_socket = AsyncMock()
        feed._zmq_socket = mock_socket

        # Round 0: seed — no publish.
        await feed._poll_once()
        assert mock_socket.send_string.await_count == 0

        # Round 1: bar changed → publish.
        round_num["n"] = 1
        await feed._poll_once()
        assert mock_socket.send_string.await_count == 1
        assert feed.snapshots_published == 1

    @pytest.mark.asyncio
    async def test_poll_once_skips_bad_data(self):
        """If snapshot validation fails, poll_once continues without error."""
        client = MagicMock()
        round_num = {"n": 0}

        bars_v1 = _make_bars(200, base_time=1_000_000)
        bars_v2 = _make_bars(200, base_time=1_000_300)

        def copy_rates(symbol, timeframe, start_pos, count):
            if timeframe == TIMEFRAME_MAP["M5"] and round_num["n"] >= 1:
                return bars_v2[:count]
            return bars_v1[:count]

        client.copy_rates_from_pos.side_effect = copy_rates
        # Zero spread → validation error.
        client.symbol_info_tick.return_value = Tick(
            time=int(time.time()), bid=1.0, ask=1.0, last=0.0, volume=0, flags=0,
        )

        feed = MarketFeed(client, ["EURUSD"])
        mock_socket = AsyncMock()
        feed._zmq_socket = mock_socket

        await feed._poll_once()  # seed
        round_num["n"] = 1
        await feed._poll_once()  # trigger close, but validation fails

        assert mock_socket.send_string.await_count == 0
        assert feed.validation_errors == 1

    @pytest.mark.asyncio
    async def test_multiple_pairs(self):
        """Feed polls all configured pairs."""
        bars_v1 = _make_bars(200, base_time=1_000_000)
        bars_v2 = _make_bars(200, base_time=1_000_300)

        client = MagicMock()
        round_num = {"n": 0}

        def copy_rates(symbol, timeframe, start_pos, count):
            if timeframe == TIMEFRAME_MAP["M5"] and round_num["n"] >= 1:
                return bars_v2[:count]
            return bars_v1[:count]

        client.copy_rates_from_pos.side_effect = copy_rates
        client.symbol_info_tick.return_value = _make_tick()

        feed = MarketFeed(client, ["EURUSD", "GBPUSD"])
        mock_socket = AsyncMock()
        feed._zmq_socket = mock_socket

        await feed._poll_once()  # seed both pairs
        round_num["n"] = 1
        await feed._poll_once()  # both pairs see close

        assert mock_socket.send_string.await_count == 2
        assert feed.snapshots_published == 2


# ═════════════════════════════════════════════════════════════════════════
# MarketFeed — run lifecycle
# ═════════════════════════════════════════════════════════════════════════

class TestRunLifecycle:
    @pytest.mark.asyncio
    async def test_run_cancellation_cleans_up(self):
        """Cancelling run() closes ZMQ resources."""
        client = _make_mock_client()
        # Use tcp for test (no filesystem ipc socket).
        feed = MarketFeed(client, ["EURUSD"], zmq_addr="tcp://127.0.0.1:0", poll_interval=0.01)

        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.05)
        task.cancel()
        # run() catches CancelledError internally and cleans up, so
        # await may or may not re-raise depending on Python version.
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert feed._zmq_socket is None
        assert feed._zmq_ctx is None
