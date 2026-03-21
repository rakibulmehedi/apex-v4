"""
src/market/mt5_factory.py — Instantiate the correct MT5Client based on config.

Usage:
    from src.market.mt5_factory import get_mt5_client
    client = get_mt5_client()
    client.initialize()
"""
from __future__ import annotations

import os

import structlog

from src.market.mt5_client import MT5Client

logger = structlog.get_logger(__name__)

_MT5_MODE_ENV = "APEX_MT5_MODE"
_DEFAULT_MODE = "stub"


def get_mt5_client(mode: str | None = None) -> MT5Client:
    """Return the MT5Client implementation for the given mode.

    Resolution order for *mode*:
      1. Explicit ``mode`` argument
      2. ``APEX_MT5_MODE`` environment variable
      3. Falls back to ``"stub"``

    Raises ``ValueError`` for unrecognised modes.
    """
    if mode is None:
        mode = os.environ.get(_MT5_MODE_ENV, _DEFAULT_MODE)

    mode = mode.strip().lower()

    if mode == "stub":
        from src.market.mt5_stub import StubMT5Client

        logger.info("MT5 client mode: stub")
        return StubMT5Client()

    if mode == "real":
        from src.market.mt5_real import RealMT5Client

        logger.info("MT5 client mode: real")
        return RealMT5Client()

    raise ValueError(
        f"Unknown mt5 mode: {mode!r}. Expected 'stub' or 'real'."
    )
