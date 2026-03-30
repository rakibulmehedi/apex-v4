"""Async MT5 data ingestion — candle close detection and ZMQ publishing.

Architecture ref: APEX_V4_STRATEGY.md Section 5, Market Input Layer.
Phase: P1.3.

On every M5/M15/H1 candle close the feed builds a validated
``MarketSnapshot`` for each configured pair and publishes it as JSON
over ZMQ PUSH to ``tcp://127.0.0.1:5559``.

Validation failures are logged and skipped — bad data never propagates.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Sequence

import structlog
import zmq
import zmq.asyncio

from src.market.mt5_client import MT5Client
from src.market.mt5_types import TIMEFRAME_MAP, RateBar
from src.market.schemas import CandleMap, MarketSnapshot, OHLCV, TradingSession

if TYPE_CHECKING:
    from src.risk.reconciler import StateReconciler

logger = structlog.get_logger(__name__)

# ── defaults ─────────────────────────────────────────────────────────────

_DEFAULT_ZMQ_ADDR = "tcp://127.0.0.1:5559"
_DEFAULT_POLL_SECONDS = 5.0

# Trigger timeframes — a snapshot is emitted when any of these close.
_TRIGGER_TIMEFRAMES = ("M5", "M15", "H1")

# All timeframes required in a snapshot (includes H4 for depth).
_ALL_TIMEFRAMES = ("M5", "M15", "H1", "H4")

# Minimum candle counts per timeframe (strategy spec).
_MIN_CANDLES: dict[str, int] = {"M5": 50, "M15": 50, "H1": 200, "H4": 50}


# ── session classifier ──────────────────────────────────────────────────


def classify_session(utc_hour: int) -> TradingSession:
    """Classify the current trading session from a UTC hour (0–23).

    Priority (strategy spec):
      OVERLAP  12-16  (London + NY both open)
      LONDON    7-12
      NY       16-21
      ASIA     else   (covers 21-07)
    """
    if 12 <= utc_hour < 16:
        return TradingSession.OVERLAP
    if 7 <= utc_hour < 12:
        return TradingSession.LONDON
    if 16 <= utc_hour < 21:
        return TradingSession.NY
    return TradingSession.ASIA


# ── MarketFeed ───────────────────────────────────────────────────────────


class MarketFeed:
    """Async poller that detects candle closes and publishes snapshots.

    Parameters
    ----------
    client : MT5Client
        Initialised MT5 client (stub or real).
    pairs : sequence of str
        Currency pairs to poll (e.g. ``["EURUSD", "GBPUSD"]``).
    zmq_addr : str
        ZMQ PUSH bind address.
    poll_interval : float
        Seconds between polling cycles.
    """

    def __init__(
        self,
        client: MT5Client,
        pairs: Sequence[str],
        *,
        zmq_addr: str = _DEFAULT_ZMQ_ADDR,
        poll_interval: float = _DEFAULT_POLL_SECONDS,
        reconciler: StateReconciler | None = None,
    ) -> None:
        self._client = client
        self._pairs = list(pairs)
        self._zmq_addr = zmq_addr
        self._poll_interval = poll_interval
        self._reconciler = reconciler

        # Last-seen bar open time per (pair, timeframe) — used for close
        # detection.  A change in the latest bar timestamp means the
        # previous bar has closed.
        self._last_bar_time: dict[tuple[str, str], int] = {}

        # ZMQ socket is created lazily in ``run()`` so the event loop
        # owns the context.
        self._zmq_ctx: zmq.asyncio.Context | None = None
        self._zmq_socket: zmq.asyncio.Socket | None = None

        # Counts for observability.
        self.snapshots_published: int = 0
        self.validation_errors: int = 0

    # ── lifecycle ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main loop — poll until cancelled."""
        self._zmq_ctx = zmq.asyncio.Context()
        self._zmq_socket = self._zmq_ctx.socket(zmq.PUSH)
        self._zmq_socket.bind(self._zmq_addr)
        logger.info(
            "MarketFeed started",
            pairs=self._pairs,
            zmq_addr=self._zmq_addr,
            poll_interval=self._poll_interval,
        )
        try:
            while True:
                await self._poll_once()
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("MarketFeed cancelled — shutting down")
        finally:
            self._cleanup_zmq()

    def _cleanup_zmq(self) -> None:
        if self._zmq_socket is not None:
            self._zmq_socket.close(linger=0)
            self._zmq_socket = None
        if self._zmq_ctx is not None:
            self._zmq_ctx.term()
            self._zmq_ctx = None

    # ── polling ──────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        """One polling cycle: check every pair for candle closes."""
        for pair in self._pairs:
            if self._has_candle_close(pair):
                snapshot = self._build_snapshot(pair)
                if snapshot is not None:
                    await self._publish(snapshot)

    def _has_candle_close(self, pair: str) -> bool:
        """Return True if any trigger-timeframe candle closed since last poll."""
        closed = False
        for tf in _TRIGGER_TIMEFRAMES:
            mt5_tf = TIMEFRAME_MAP[tf]
            # Fetch just the latest 1 bar to check the timestamp.
            bars = self._client.copy_rates_from_pos(pair, mt5_tf, 0, 1)
            if bars is None or len(bars) == 0:
                logger.warning(
                    "copy_rates_from_pos returned no data",
                    pair=pair,
                    timeframe=tf,
                )
                continue

            current_bar_time = bars[0].time
            key = (pair, tf)
            prev = self._last_bar_time.get(key)
            self._last_bar_time[key] = current_bar_time

            if prev is not None and current_bar_time != prev:
                logger.debug(
                    "candle close detected",
                    pair=pair,
                    timeframe=tf,
                    prev_bar=prev,
                    new_bar=current_bar_time,
                )
                closed = True

        return closed

    # ── snapshot building ────────────────────────────────────────────

    def _build_snapshot(self, pair: str) -> MarketSnapshot | None:
        """Fetch all timeframes, build and validate a MarketSnapshot.

        Returns None on failure — never raises.
        """
        candle_data: dict[str, list[dict]] = {}
        for tf in _ALL_TIMEFRAMES:
            mt5_tf = TIMEFRAME_MAP[tf]
            count = _MIN_CANDLES[tf]
            bars = self._client.copy_rates_from_pos(pair, mt5_tf, 0, count)
            if bars is None:
                logger.error(
                    "cannot build snapshot — missing candle data",
                    pair=pair,
                    timeframe=tf,
                )
                return None
            candle_data[tf] = [_bar_to_ohlcv(b) for b in bars]

        # Spread from latest tick.
        tick = self._client.symbol_info_tick(pair)
        if tick is None:
            logger.error("cannot build snapshot — tick unavailable", pair=pair)
            return None

        spread_points = tick.ask - tick.bid
        now_ms = int(time.time() * 1000)
        tick_age_ms = now_ms - (tick.time * 1000)
        utc_hour = datetime.now(timezone.utc).hour
        session = classify_session(utc_hour)

        try:
            snapshot = MarketSnapshot(
                pair=pair,
                timestamp=now_ms,
                candles=CandleMap(
                    M5=[OHLCV(**c) for c in candle_data["M5"]],
                    M15=[OHLCV(**c) for c in candle_data["M15"]],
                    H1=[OHLCV(**c) for c in candle_data["H1"]],
                    H4=[OHLCV(**c) for c in candle_data["H4"]],
                ),
                spread_points=spread_points,
                session=session,
            )
        except Exception as e:
            self.validation_errors += 1
            logger.error(
                "snapshot validation failed — skipping",
                pair=pair,
                error_type=type(e).__name__,
                error_msg=str(e),
            )
            return None

        if snapshot.is_stale:
            logger.warning(
                "snapshot marked stale",
                pair=pair,
                tick_age_ms=tick_age_ms,
            )

        return snapshot

    # ── publishing ───────────────────────────────────────────────────

    async def _publish(self, snapshot: MarketSnapshot) -> None:
        """Serialize and send snapshot over ZMQ PUSH."""
        if self._zmq_socket is None:
            logger.error("ZMQ socket not initialised — cannot publish")
            return

        payload = snapshot.model_dump_json()
        await self._zmq_socket.send_string(payload)
        self.snapshots_published += 1
        if self._reconciler is not None:
            self._reconciler.update_last_snapshot_time()
        logger.info(
            "snapshot published",
            pair=snapshot.pair,
            session=snapshot.session,
            is_stale=snapshot.is_stale,
        )


# ── helpers ──────────────────────────────────────────────────────────────


def _bar_to_ohlcv(bar: RateBar) -> dict:
    """Convert a RateBar to an OHLCV-compatible dict."""
    return {
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": float(bar.tick_volume),
    }
