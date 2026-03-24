"""Unit tests for src/observability/metrics.py — Prometheus metric definitions.

Tests cover:
  - All 14 metrics importable and registered
  - Counter/Gauge/Histogram types correct
  - start_metrics_server() calls start_http_server with correct port
  - Port override via APEX_METRICS_PORT env var
  - Port-already-bound error logged, not raised
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import CollectorRegistry

from src.observability import metrics


# ── metric definitions ─────────────────────────────────────────────────


class TestMetricDefinitions:
    """Verify all 14 metrics are defined with correct types."""

    def test_counters_exist(self) -> None:
        assert metrics.SIGNALS_GENERATED_TOTAL is not None
        assert metrics.TRADES_EXECUTED_TOTAL is not None
        assert metrics.TRADES_WON_TOTAL is not None
        assert metrics.GATE_REJECTIONS_TOTAL is not None
        assert metrics.KILL_SWITCH_TOTAL is not None
        assert metrics.STATE_DRIFT_TOTAL is not None

    def test_gauges_exist(self) -> None:
        assert metrics.PORTFOLIO_VAR_PCT is not None
        assert metrics.CURRENT_DRAWDOWN_PCT is not None
        assert metrics.COVARIANCE_CONDITION is not None
        assert metrics.OPEN_POSITIONS_COUNT is not None
        assert metrics.WIN_RATE_7D is not None

    def test_histograms_exist(self) -> None:
        assert metrics.SIGNAL_LATENCY_MS is not None
        assert metrics.SLIPPAGE_POINTS is not None
        assert metrics.R_MULTIPLE is not None

    def test_counter_labels(self) -> None:
        """Counters have correct label names."""
        assert metrics.SIGNALS_GENERATED_TOTAL._labelnames == ("strategy", "regime", "pair")
        assert metrics.TRADES_EXECUTED_TOTAL._labelnames == ("strategy", "regime", "direction")
        assert metrics.TRADES_WON_TOTAL._labelnames == ("strategy", "regime")
        assert metrics.GATE_REJECTIONS_TOTAL._labelnames == ("gate_number", "reason")
        assert metrics.KILL_SWITCH_TOTAL._labelnames == ("level",)
        assert metrics.STATE_DRIFT_TOTAL._labelnames == ()

    def test_histogram_buckets(self) -> None:
        """Histograms have correct bucket boundaries."""
        # prometheus_client stores buckets as list of floats with +Inf appended.
        assert list(metrics.SIGNAL_LATENCY_MS._upper_bounds) == [50, 100, 200, 500, 1000, float("inf")]
        assert list(metrics.SLIPPAGE_POINTS._upper_bounds) == [0.1, 0.5, 1, 2, 5, float("inf")]
        assert list(metrics.R_MULTIPLE._upper_bounds) == [-3, -2, -1, 0, 1, 2, 3, 5, float("inf")]

    def test_metric_count(self) -> None:
        """All 14 metrics are defined (6 counters + 5 gauges + 3 histograms)."""
        from prometheus_client import Counter, Gauge, Histogram

        module_metrics = [
            v for v in vars(metrics).values()
            if isinstance(v, (Counter, Gauge, Histogram))
        ]
        assert len(module_metrics) == 14


# ── server startup ─────────────────────────────────────────────────────


class TestStartMetricsServer:
    """Test start_metrics_server() behavior."""

    @patch("src.observability.metrics.start_http_server")
    def test_default_port(self, mock_start: MagicMock) -> None:
        """Default port is 8000."""
        port = metrics.start_metrics_server()
        mock_start.assert_called_once_with(8000)
        assert port == 8000

    @patch("src.observability.metrics.start_http_server")
    def test_explicit_port(self, mock_start: MagicMock) -> None:
        """Explicit port arg is used."""
        port = metrics.start_metrics_server(port=9090)
        mock_start.assert_called_once_with(9090)
        assert port == 9090

    @patch.dict("os.environ", {"APEX_METRICS_PORT": "9100"})
    @patch("src.observability.metrics.start_http_server")
    def test_env_var_port(self, mock_start: MagicMock) -> None:
        """Port from APEX_METRICS_PORT env var."""
        port = metrics.start_metrics_server()
        mock_start.assert_called_once_with(9100)
        assert port == 9100

    @patch("src.observability.metrics.start_http_server", side_effect=OSError("Address in use"))
    def test_port_in_use_does_not_raise(self, mock_start: MagicMock) -> None:
        """Port-already-bound logs error but does not raise."""
        port = metrics.start_metrics_server(port=8000)
        assert port == 8000  # still returns port even on failure
