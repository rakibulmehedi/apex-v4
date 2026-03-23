"""
src/backtest/bt_feed.py — backtrader data feed adapter for synthetic data.

Converts SyntheticCandle list into a backtrader PandasData feed.
"""
from __future__ import annotations

import backtrader as bt
import pandas as pd

from src.backtest.data_gen import SyntheticCandle


def candles_to_bt_feed(candles: list[SyntheticCandle]) -> bt.feeds.PandasData:
    """Convert synthetic candles to a backtrader PandasData feed."""
    records = [
        {
            "datetime": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "openinterest": 0,
        }
        for c in candles
    ]
    df = pd.DataFrame.from_records(records)
    df.set_index("datetime", inplace=True)

    return bt.feeds.PandasData(dataname=df)
