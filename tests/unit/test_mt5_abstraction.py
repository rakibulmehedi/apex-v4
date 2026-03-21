"""
tests/unit/test_mt5_abstraction.py — Unit tests for the MT5 abstraction layer.

Covers: mt5_types, mt5_client ABC, mt5_stub, mt5_factory.
All tests run on macOS — no real MT5 connection needed.
"""
from __future__ import annotations

import pytest

from src.market.mt5_client import MT5Client
from src.market.mt5_factory import get_mt5_client
from src.market.mt5_stub import StubMT5Client
from src.market.mt5_types import (
    TRADE_RETCODE_DONE,
    AccountInfo,
    OrderResult,
    Position,
    Tick,
)


# ── Type dataclasses ──────────────────────────────────────────────


class TestMT5Types:
    def test_account_info_fields(self) -> None:
        info = AccountInfo(
            login=123, server="Test", balance=10_000.0, equity=10_000.0,
            margin=0.0, margin_free=10_000.0, margin_level=0.0, currency="USD",
        )
        assert info.equity == 10_000.0
        assert info.currency == "USD"

    def test_account_info_is_frozen(self) -> None:
        info = AccountInfo(
            login=1, server="S", balance=0, equity=0,
            margin=0, margin_free=0, margin_level=0, currency="USD",
        )
        with pytest.raises(AttributeError):
            info.equity = 999  # type: ignore[misc]

    def test_tick_fields(self) -> None:
        tick = Tick(time=1000, bid=1.08, ask=1.09, last=0.0, volume=0, flags=0)
        assert tick.bid == 1.08
        assert tick.ask == 1.09

    def test_order_result_fields(self) -> None:
        result = OrderResult(
            retcode=TRADE_RETCODE_DONE, order=1, deal=2,
            volume=0.01, price=1.08, comment="ok",
        )
        assert result.retcode == TRADE_RETCODE_DONE

    def test_position_fields(self) -> None:
        pos = Position(
            ticket=1, symbol="EURUSD", type=0, volume=0.01,
            price_open=1.08, price_current=1.09, sl=1.07, tp=1.10,
            profit=10.0, comment="",
        )
        assert pos.symbol == "EURUSD"
        assert pos.profit == 10.0

    def test_retcode_constants(self) -> None:
        assert TRADE_RETCODE_DONE == 10009


# ── StubMT5Client ────────────────────────────────────────────────


class TestStubMT5Client:
    def test_is_mt5client_subclass(self) -> None:
        assert issubclass(StubMT5Client, MT5Client)

    def test_initialize_returns_true(self) -> None:
        client = StubMT5Client()
        assert client.initialize() is True

    def test_shutdown_succeeds(self) -> None:
        client = StubMT5Client()
        client.initialize()
        client.shutdown()  # should not raise

    def test_account_info_before_init_returns_none(self) -> None:
        client = StubMT5Client()
        assert client.account_info() is None

    def test_account_info_returns_expected_values(self) -> None:
        client = StubMT5Client()
        client.initialize()
        info = client.account_info()
        assert info is not None
        assert info.equity == 10_000.0
        assert info.balance == 10_000.0
        assert info.currency == "USD"

    def test_positions_get_empty_by_default(self) -> None:
        client = StubMT5Client()
        client.initialize()
        positions = client.positions_get()
        assert positions is not None
        assert positions == []

    def test_positions_get_before_init_returns_none(self) -> None:
        client = StubMT5Client()
        assert client.positions_get() is None

    def test_order_send_returns_done(self) -> None:
        client = StubMT5Client()
        client.initialize()
        request = {"symbol": "EURUSD", "type": 0, "volume": 0.01}
        result = client.order_send(request)
        assert result is not None
        assert result.retcode == TRADE_RETCODE_DONE
        assert result.volume == 0.01
        assert result.price > 0

    def test_order_send_before_init_returns_none(self) -> None:
        client = StubMT5Client()
        assert client.order_send({"symbol": "EURUSD"}) is None

    def test_order_send_increments_ticket(self) -> None:
        client = StubMT5Client()
        client.initialize()
        r1 = client.order_send({"symbol": "EURUSD", "type": 0, "volume": 0.01})
        r2 = client.order_send({"symbol": "EURUSD", "type": 0, "volume": 0.01})
        assert r1 is not None and r2 is not None
        assert r2.order == r1.order + 1

    def test_order_send_buy_fills_at_ask(self) -> None:
        client = StubMT5Client()
        client.initialize()
        result = client.order_send({"symbol": "EURUSD", "type": 0, "volume": 0.01})
        tick = client.symbol_info_tick("EURUSD")
        assert result is not None and tick is not None
        assert result.price == tick.ask

    def test_order_send_sell_fills_at_bid(self) -> None:
        client = StubMT5Client()
        client.initialize()
        result = client.order_send({"symbol": "EURUSD", "type": 1, "volume": 0.01})
        tick = client.symbol_info_tick("EURUSD")
        assert result is not None and tick is not None
        assert result.price == tick.bid

    def test_symbol_info_tick_eurusd(self) -> None:
        client = StubMT5Client()
        client.initialize()
        tick = client.symbol_info_tick("EURUSD")
        assert tick is not None
        assert 1.0 < tick.bid < 1.2
        assert tick.ask > tick.bid

    def test_symbol_info_tick_unknown_symbol(self) -> None:
        client = StubMT5Client()
        client.initialize()
        tick = client.symbol_info_tick("XYZABC")
        assert tick is not None  # stub returns default prices
        assert tick.bid > 0

    def test_symbol_info_tick_before_init_returns_none(self) -> None:
        client = StubMT5Client()
        assert client.symbol_info_tick("EURUSD") is None


# ── Factory ───────────────────────────────────────────────────────


class TestMT5Factory:
    def test_stub_mode_returns_stub_client(self) -> None:
        client = get_mt5_client(mode="stub")
        assert isinstance(client, StubMT5Client)

    def test_real_mode_returns_real_client(self) -> None:
        from src.market.mt5_real import RealMT5Client
        client = get_mt5_client(mode="real")
        assert isinstance(client, RealMT5Client)

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown mt5 mode"):
            get_mt5_client(mode="bogus")

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("APEX_MT5_MODE", "stub")
        client = get_mt5_client()
        assert isinstance(client, StubMT5Client)

    def test_default_is_stub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("APEX_MT5_MODE", raising=False)
        client = get_mt5_client()
        assert isinstance(client, StubMT5Client)

    def test_mode_is_case_insensitive(self) -> None:
        client = get_mt5_client(mode="STUB")
        assert isinstance(client, StubMT5Client)

    def test_mode_strips_whitespace(self) -> None:
        client = get_mt5_client(mode="  stub  ")
        assert isinstance(client, StubMT5Client)
