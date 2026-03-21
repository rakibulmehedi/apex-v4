"""
src/market/mt5_client.py — Abstract base class for all MT5 interactions.

Every module that needs MT5 depends on this interface, never on the
MetaTrader5 library directly.  The concrete implementation is selected
at startup via mt5_factory.get_mt5_client().
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.market.mt5_types import AccountInfo, OrderResult, Position, RateBar, Tick


class MT5Client(ABC):
    """Platform-agnostic interface to MetaTrader 5."""

    @abstractmethod
    def initialize(self) -> bool:
        """Connect to the MT5 terminal. Returns True on success."""

    @abstractmethod
    def shutdown(self) -> None:
        """Disconnect from the MT5 terminal."""

    @abstractmethod
    def account_info(self) -> AccountInfo | None:
        """Return current account state, or None on failure."""

    @abstractmethod
    def positions_get(self) -> list[Position] | None:
        """Return all open positions, or None on failure."""

    @abstractmethod
    def order_send(self, request: dict[str, Any]) -> OrderResult | None:
        """Send a trade request. Returns result or None on failure."""

    @abstractmethod
    def symbol_info_tick(self, symbol: str) -> Tick | None:
        """Return the latest tick for *symbol*, or None on failure."""

    @abstractmethod
    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int,
    ) -> list[RateBar] | None:
        """Return *count* bars ending at *start_pos* (0 = current).

        Returns None on failure.
        """
