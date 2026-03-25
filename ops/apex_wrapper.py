"""
ops/apex_wrapper.py — Graceful shutdown wrapper for APEX V4 Windows service.

NSSM sends Ctrl+C (SIGINT) when stopping the service. This wrapper
catches that signal, gives the pipeline time to close positions and
flush state, then exits with a clean code.

Exit codes:
    0  — clean shutdown (market close, manual stop)
    42 — kill switch SOFT/HARD
    43 — kill switch EMERGENCY
    1  — unhandled error (triggers NSSM restart)

Usage (via NSSM):
    python.exe -m ops.apex_wrapper
Or direct:
    python.exe ops/apex_wrapper.py
"""

from __future__ import annotations

import signal
import sys
import time
from typing import NoReturn

import structlog

log = structlog.get_logger(__name__)

# Maximum seconds to wait for pipeline graceful shutdown before force-exit.
# Must be less than NSSM's total stop timeout (~30s).
SHUTDOWN_TIMEOUT_SEC = 25

_shutting_down = False


def _shutdown_handler(signum: int, _frame: object) -> None:
    """Handle Ctrl+C / SIGINT / SIGTERM from NSSM stop."""
    global _shutting_down
    if _shutting_down:
        log.warning("forced_exit", reason="second signal received")
        sys.exit(1)

    _shutting_down = True
    sig_name = signal.Signals(signum).name
    log.info("shutdown_requested", signal=sig_name, timeout_sec=SHUTDOWN_TIMEOUT_SEC)


def is_shutting_down() -> bool:
    """Check if a shutdown signal has been received.

    The pipeline main loop should poll this and begin graceful teardown
    (close positions, cancel pending orders, flush state) when True.
    """
    return _shutting_down


def main() -> NoReturn:
    """Entry point: register signal handlers, then hand off to pipeline."""
    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    log.info("apex_wrapper_start", pid=__import__("os").getpid())

    try:
        # Import and run the pipeline.
        # When pipeline.py (P4.6) is implemented, it should:
        #   1. Check ops.apex_wrapper.is_shutting_down() in its main loop
        #   2. On True: close positions, flush state, then return
        #   3. On kill switch: call sys.exit(42) or sys.exit(43)
        from src.pipeline import main as pipeline_main  # type: ignore[attr-defined]

        pipeline_main()
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
        log.info("pipeline_exit", code=code)
        sys.exit(code)
    except Exception:
        log.exception("pipeline_crash")
        sys.exit(1)

    # If pipeline returns normally, treat as clean shutdown.
    log.info("pipeline_complete", exit_code=0)
    sys.exit(0)


if __name__ == "__main__":
    main()
