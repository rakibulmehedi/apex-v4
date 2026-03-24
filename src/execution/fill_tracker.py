"""FillTracker — Slippage measurement + fill recording + close handling.

Phase 4 (P4.2).
Records fills only after TRADE_RETCODE_DONE (V3 bug P0.4 fix carried forward).
Measures: actual_fill_price vs requested_price → slippage in points.
Writes to PostgreSQL fills table.

On close: calculates R-multiple and returns outcome dict for the recorder.

Architecture ref: APEX_V4_STRATEGY.md Section 5, Phase 4 (P4.2)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy.orm import Session

from db.models import Fill, make_session_factory
from src.execution.gateway import FillRecord

logger = structlog.get_logger(__name__)


class FillTracker:
    """Record fills to PostgreSQL and compute outcomes on close.

    Parameters
    ----------
    session_factory
        SQLAlchemy sessionmaker.  When *None*, one is created from
        ``APEX_DATABASE_URL``.
    """

    def __init__(self, session_factory: Any = None) -> None:
        if session_factory is not None:
            self._sf = session_factory
        else:
            self._sf = make_session_factory()

        # In-memory map: order_id → fill metadata (for close-time R calc).
        self._open_fills: dict[int, dict[str, Any]] = {}

    # ── record fill ─────────────────────────────────────────────────

    def record_fill(self, fill: FillRecord) -> int | None:
        """Persist a confirmed fill to the ``fills`` table.

        Also caches fill metadata in-memory so ``record_close`` can
        compute the R-multiple without a DB round-trip.

        Returns
        -------
        int | None
            The ``fills.id`` primary key on success, None on DB error.
        """
        filled_at = datetime.fromtimestamp(
            fill.filled_at_ms / 1000.0, tz=timezone.utc,
        )

        try:
            with self._sf() as db:  # type: Session
                row = Fill(
                    order_id=fill.order_id,
                    pair=fill.pair,
                    direction=fill.direction,
                    strategy=fill.strategy,
                    regime=fill.regime,
                    requested_size=fill.requested_volume,
                    actual_size=fill.filled_volume,
                    requested_price=fill.requested_price,
                    actual_fill_price=fill.fill_price,
                    slippage_points=fill.slippage_points,
                    filled_at=filled_at,
                )
                db.add(row)
                db.commit()
                fill_id = row.id

                logger.info(
                    "fill_recorded",
                    fill_id=fill_id,
                    order_id=fill.order_id,
                    pair=fill.pair,
                    direction=fill.direction,
                    slippage=round(fill.slippage_points, 6),
                )
        except Exception:
            logger.critical(
                "fill_record_failed",
                order_id=fill.order_id,
                pair=fill.pair,
                exc_info=True,
            )
            return None

        # Cache for close-time lookup.
        self._open_fills[fill.order_id] = {
            "fill_id": fill_id,
            "pair": fill.pair,
            "direction": fill.direction,
            "strategy": fill.strategy,
            "regime": fill.regime,
            "entry_price": fill.fill_price,
            "session": fill.session if hasattr(fill, "session") else None,
            "opened_at": filled_at,
        }

        return fill_id

    # ── record close ────────────────────────────────────────────────

    def record_close(
        self,
        order_id: int,
        close_price: float,
        close_time_ms: int,
        stop_loss: float,
        session_label: str,
    ) -> dict[str, Any] | None:
        """Compute R-multiple and return an outcome dict.

        Parameters
        ----------
        order_id
            The MT5 order ticket (or paper-trade synthetic ID) that
            was returned in the FillRecord.
        close_price
            Price at which the position was closed.
        close_time_ms
            Unix ms timestamp of the close.
        stop_loss
            The original stop-loss price for the trade.
        session_label
            Trading session at close time (e.g. "LONDON").

        Returns
        -------
        dict | None
            Outcome dict ready for ``TradeOutcomeRecorder.record()``,
            or None if the order_id is unknown.
        """
        meta = self._open_fills.pop(order_id, None)
        if meta is None:
            logger.error(
                "close_unknown_order",
                order_id=order_id,
                reason="order_id not in open fills",
            )
            return None

        entry_price: float = meta["entry_price"]
        direction: str = meta["direction"]

        # Risk = |entry - stop_loss|
        risk = abs(entry_price - stop_loss)
        if risk == 0:
            logger.error(
                "close_zero_risk",
                order_id=order_id,
                entry_price=entry_price,
                stop_loss=stop_loss,
            )
            return None

        # R-multiple = (close - entry) / risk for LONG
        #            = (entry - close) / risk for SHORT
        if direction == "LONG":
            r_multiple = (close_price - entry_price) / risk
        else:
            r_multiple = (entry_price - close_price) / risk

        won = r_multiple > 0

        closed_at = datetime.fromtimestamp(
            close_time_ms / 1000.0, tz=timezone.utc,
        )

        outcome = {
            "pair": meta["pair"],
            "strategy": meta["strategy"],
            "regime": meta["regime"],
            "session": session_label,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": close_price,
            "r_multiple": r_multiple,
            "won": won,
            "fill_id": meta["fill_id"],
            "opened_at": meta["opened_at"],
            "closed_at": closed_at,
        }

        logger.info(
            "trade_closed",
            order_id=order_id,
            pair=meta["pair"],
            direction=direction,
            r_multiple=round(r_multiple, 4),
            won=won,
        )

        return outcome
