"""
src/market/mt5_stub.py — Development stub returning realistic fake data.

Used on macOS and in all unit tests.  Every response mirrors what the real
MT5 terminal would return for a healthy, idle account.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from src.market.mt5_client import MT5Client
from src.market.mt5_types import (
    TRADE_RETCODE_DONE,
    AccountInfo,
    OrderResult,
    Position,
    RateBar,
    Tick,
)

logger = structlog.get_logger(__name__)

# Realistic mid-market prices for stub ticks (approximate March 2026).
_STUB_PRICES: dict[str, tuple[float, float]] = {
    "EURUSD": (1.08450, 1.08465),
    "GBPUSD": (1.26320, 1.26340),
    "USDJPY": (149.850, 149.865),
    "AUDUSD": (0.65120, 0.65135),
}

_DEFAULT_BID_ASK = (1.00000, 1.00015)


class StubMT5Client(MT5Client):
    """In-memory MT5 stand-in for development and testing."""

    def __init__(self) -> None:
        self._initialized: bool = False
        self._next_ticket: int = 100_000
        self._positions: list[Position] = []

    def initialize(self) -> bool:
        self._initialized = True
        logger.info("StubMT5Client.initialize — connected (stub)")
        return True

    def shutdown(self) -> None:
        self._initialized = False
        logger.info("StubMT5Client.shutdown — disconnected (stub)")

    def account_info(self) -> AccountInfo | None:
        if not self._initialized:
            return None
        return AccountInfo(
            login=12345678,
            server="StubBroker-Demo",
            balance=10_000.0,
            equity=10_000.0,
            margin=0.0,
            margin_free=10_000.0,
            margin_level=0.0,
            currency="USD",
        )

    def positions_get(self) -> list[Position] | None:
        if not self._initialized:
            return None
        return list(self._positions)

    def order_send(self, request: dict[str, Any]) -> OrderResult | None:
        if not self._initialized:
            return None

        symbol = request.get("symbol", "EURUSD")
        bid, ask = _STUB_PRICES.get(symbol, _DEFAULT_BID_ASK)
        action_type = request.get("type", 0)
        fill_price = ask if action_type == 0 else bid  # 0=BUY, 1=SELL

        ticket = self._next_ticket
        self._next_ticket += 1

        logger.info(
            "StubMT5Client.order_send — filled (stub)",
            ticket=ticket,
            symbol=symbol,
            price=fill_price,
        )
        return OrderResult(
            retcode=TRADE_RETCODE_DONE,
            order=ticket,
            deal=ticket + 50_000,
            volume=request.get("volume", 0.01),
            price=fill_price,
            comment="stub fill",
        )

    def symbol_info_tick(self, symbol: str) -> Tick | None:
        if not self._initialized:
            return None

        bid, ask = _STUB_PRICES.get(symbol, _DEFAULT_BID_ASK)
        return Tick(
            time=int(time.time()),
            bid=bid,
            ask=ask,
            last=0.0,
            volume=0,
            flags=0,
        )

    def copy_rates_from_pos(
        self,
        symbol: str,
        timeframe: int,
        start_pos: int,
        count: int,
    ) -> list[RateBar] | None:
        if not self._initialized:
            return None

        bid, _ = _STUB_PRICES.get(symbol, _DEFAULT_BID_ASK)
        now = int(time.time())

        # Determine bar duration in seconds from the timeframe constant.
        bar_seconds = _tf_to_seconds(timeframe)
        # Align to the most recent bar boundary.
        current_bar = (now // bar_seconds) * bar_seconds

        bars: list[RateBar] = []
        for i in range(count - 1, -1, -1):
            bar_time = current_bar - (i + start_pos) * bar_seconds
            # Small deterministic jitter from bar index for visual variety.
            jitter = ((hash((symbol, bar_time)) % 100) - 50) * 0.00001
            o = bid + jitter
            h = o + 0.00050
            l = o - 0.00050
            c = o + 0.00020
            bars.append(
                RateBar(
                    time=bar_time,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    tick_volume=150,
                )
            )
        return bars


def _tf_to_seconds(timeframe: int) -> int:
    """Convert an MT5 timeframe constant to bar duration in seconds."""
    _MAP = {
        5: 5 * 60,  # M5
        15: 15 * 60,  # M15
        16385: 60 * 60,  # H1
        16388: 4 * 60 * 60,  # H4
    }
    return _MAP.get(timeframe, 5 * 60)
