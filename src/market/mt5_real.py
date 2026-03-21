"""
src/market/mt5_real.py — Production MT5 client wrapping the MetaTrader5 library.

MetaTrader5 is imported lazily inside each method so this module can be
imported on macOS without raising ImportError at load time.  It will only
fail at *runtime* if a method is actually called on a non-Windows machine.
"""
from __future__ import annotations

from typing import Any

import structlog

from src.market.mt5_client import MT5Client
from src.market.mt5_types import (
    AccountInfo,
    OrderResult,
    Position,
    RateBar,
    Tick,
)

logger = structlog.get_logger(__name__)


def _import_mt5():  # noqa: ANN202
    """Lazy import — fails loudly if MetaTrader5 is not installed."""
    try:
        import MetaTrader5 as mt5  # type: ignore[import-not-found]
        return mt5
    except ImportError:
        logger.error(
            "MetaTrader5 package not installed — "
            "this is expected on macOS. Use mt5.mode: stub in settings.yaml."
        )
        raise


class RealMT5Client(MT5Client):
    """Thin wrapper around the MetaTrader5 Python package."""

    def initialize(self) -> bool:
        mt5 = _import_mt5()
        ok = mt5.initialize()
        if not ok:
            logger.error("mt5.initialize failed", error=mt5.last_error())
        return bool(ok)

    def shutdown(self) -> None:
        mt5 = _import_mt5()
        mt5.shutdown()

    def account_info(self) -> AccountInfo | None:
        mt5 = _import_mt5()
        info = mt5.account_info()
        if info is None:
            logger.error("mt5.account_info returned None", error=mt5.last_error())
            return None
        return AccountInfo(
            login=info.login,
            server=info.server,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            margin_free=info.margin_free,
            margin_level=info.margin_level,
            currency=info.currency,
        )

    def positions_get(self) -> list[Position] | None:
        mt5 = _import_mt5()
        raw = mt5.positions_get()
        if raw is None:
            logger.error("mt5.positions_get returned None", error=mt5.last_error())
            return None
        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                type=p.type,
                volume=p.volume,
                price_open=p.price_open,
                price_current=p.price_current,
                sl=p.sl,
                tp=p.tp,
                profit=p.profit,
                comment=p.comment,
            )
            for p in raw
        ]

    def order_send(self, request: dict[str, Any]) -> OrderResult | None:
        mt5 = _import_mt5()
        result = mt5.order_send(request)
        if result is None:
            logger.error("mt5.order_send returned None", error=mt5.last_error())
            return None
        return OrderResult(
            retcode=result.retcode,
            order=result.order,
            deal=result.deal,
            volume=result.volume,
            price=result.price,
            comment=result.comment,
        )

    def symbol_info_tick(self, symbol: str) -> Tick | None:
        mt5 = _import_mt5()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.error("mt5.symbol_info_tick returned None", symbol=symbol)
            return None
        return Tick(
            time=tick.time,
            bid=tick.bid,
            ask=tick.ask,
            last=tick.last,
            volume=tick.volume,
            flags=tick.flags,
        )

    def copy_rates_from_pos(
        self, symbol: str, timeframe: int, start_pos: int, count: int,
    ) -> list[RateBar] | None:
        mt5 = _import_mt5()
        rates = mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count)
        if rates is None or len(rates) == 0:
            logger.error(
                "mt5.copy_rates_from_pos returned None/empty",
                symbol=symbol,
                timeframe=timeframe,
                error=mt5.last_error(),
            )
            return None
        return [
            RateBar(
                time=int(r["time"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                tick_volume=int(r["tick_volume"]),
            )
            for r in rates
        ]
