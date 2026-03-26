"""Prometheus metrics for APEX V4 trading system.

Phase 5 (P5.1).
Defines all counters, gauges, and histograms for the trading pipeline.
Exposes metrics on an HTTP endpoint via prometheus_client.

Metric naming convention: ``apex_<metric>`` with relevant labels.

Architecture ref: APEX_V4_STRATEGY.md Section 8 (Phase 5), CLI Playbook Ch. 7-A.
"""
from __future__ import annotations

import os

import structlog
from prometheus_client import Counter, Gauge, Histogram, start_http_server

logger = structlog.get_logger(__name__)

# ── default port (overridable via env) ────────────────────────────────

_DEFAULT_PORT = 8000

# ── counters ──────────────────────────────────────────────────────────

SIGNALS_GENERATED_TOTAL = Counter(
    "apex_signals_generated_total",
    "Total trading signals that passed all risk gates",
    ["strategy", "regime", "pair"],
)

TRADES_EXECUTED_TOTAL = Counter(
    "apex_trades_executed_total",
    "Total trades successfully executed (paper or live)",
    ["strategy", "regime", "direction"],
)

TRADES_WON_TOTAL = Counter(
    "apex_trades_won_total",
    "Total trades closed with positive R-multiple",
    ["strategy", "regime"],
)

GATE_REJECTIONS_TOTAL = Counter(
    "apex_gate_rejections_total",
    "Total risk gate rejections",
    ["gate_number", "reason"],
)

KILL_SWITCH_TOTAL = Counter(
    "apex_kill_switch_total",
    "Total kill switch activations by level",
    ["level"],
)

STATE_DRIFT_TOTAL = Counter(
    "apex_state_drift_total",
    "Total state drift events (Redis vs broker mismatch)",
)

# ── gauges ────────────────────────────────────────────────────────────

PORTFOLIO_VAR_PCT = Gauge(
    "apex_portfolio_var_pct",
    "Current portfolio VaR as fraction of portfolio value",
)

CURRENT_DRAWDOWN_PCT = Gauge(
    "apex_current_drawdown_pct",
    "Current drawdown as fraction of peak equity",
)

COVARIANCE_CONDITION = Gauge(
    "apex_covariance_condition",
    "Current covariance matrix condition number (kappa)",
)

OPEN_POSITIONS_COUNT = Gauge(
    "apex_open_positions_count",
    "Number of currently open positions",
)

WIN_RATE_7D = Gauge(
    "apex_win_rate_7d",
    "Rolling 7-day win rate across all segments",
)

# ── histograms ────────────────────────────────────────────────────────

SIGNAL_LATENCY_MS = Histogram(
    "apex_signal_latency_ms",
    "End-to-end signal processing latency in milliseconds",
    buckets=(50, 100, 200, 500, 1000),
)

SLIPPAGE_POINTS = Histogram(
    "apex_slippage_points",
    "Execution slippage in price points",
    buckets=(0.1, 0.5, 1, 2, 5),
)

R_MULTIPLE = Histogram(
    "apex_r_multiple",
    "Trade outcome measured in risk multiples",
    buckets=(-3, -2, -1, 0, 1, 2, 3, 5),
)

CYCLE_DURATION_MS = Histogram(
    "apex_cycle_duration_ms",
    "Wall-clock time for one pipeline cycle (message arrival to processing complete)",
    buckets=(10, 50, 100, 200, 500, 1000, 2000),
)


# ── server startup ────────────────────────────────────────────────────

def start_metrics_server(port: int | None = None) -> int:
    """Start the Prometheus HTTP metrics server.

    Parameters
    ----------
    port
        Port to listen on.  Falls back to ``APEX_METRICS_PORT`` env var,
        then to 8000.

    Returns
    -------
    int
        The port the server is listening on.
    """
    if port is None:
        port = int(os.environ.get("APEX_METRICS_PORT", str(_DEFAULT_PORT)))

    try:
        start_http_server(port)
        logger.info("metrics_server_started", port=port)
    except OSError:
        logger.error(
            "metrics_server_start_failed",
            port=port,
            reason="port_already_in_use",
            exc_info=True,
        )

    return port
