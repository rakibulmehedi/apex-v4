"""
src/market/mt5_types.py — Data classes mirroring MetaTrader5 return types.

These decouple the rest of the codebase from the MetaTrader5 library so
that code compiles and type-checks on any platform, including macOS where
the MetaTrader5 package is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """Mirrors MetaTrader5.AccountInfo."""

    login: int
    server: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    currency: str


@dataclass(frozen=True, slots=True)
class Tick:
    """Mirrors MetaTrader5.Tick — last quote for a symbol."""

    time: int          # unix seconds
    bid: float
    ask: float
    last: float
    volume: int
    flags: int


@dataclass(frozen=True, slots=True)
class Position:
    """Mirrors a single open position from MetaTrader5.positions_get()."""

    ticket: int
    symbol: str
    type: int          # 0 = BUY, 1 = SELL
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    comment: str


@dataclass(frozen=True, slots=True)
class OrderResult:
    """Mirrors MetaTrader5.OrderSendResult."""

    retcode: int
    order: int         # ticket number
    deal: int
    volume: float
    price: float
    comment: str


@dataclass(frozen=True, slots=True)
class RateBar:
    """Single OHLCV bar from copy_rates_from_pos().

    Mirrors one row of the numpy structured array returned by
    MetaTrader5.copy_rates_from_pos().
    """

    time: int          # bar open time, unix seconds
    open: float
    high: float
    low: float
    close: float
    tick_volume: int


# MT5 retcodes we care about
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008

# MT5 timeframe constants (match MetaTrader5.TIMEFRAME_*)
TIMEFRAME_M5 = 5
TIMEFRAME_M15 = 15
TIMEFRAME_H1 = 16385   # 0x4001
TIMEFRAME_H4 = 16388   # 0x4004

# Map from string label to MT5 constant
TIMEFRAME_MAP: dict[str, int] = {
    "M5": TIMEFRAME_M5,
    "M15": TIMEFRAME_M15,
    "H1": TIMEFRAME_H1,
    "H4": TIMEFRAME_H4,
}
