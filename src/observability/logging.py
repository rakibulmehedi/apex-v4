"""Structured logging configuration for APEX V4.

Configures structlog with JSON output for production (file/stdout)
and human-readable output for development.

Must be called once at startup before any logger is used.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

import structlog


def configure_logging(
    level: str = "INFO",
    log_dir: str | Path | None = None,
    json_output: bool = True,
) -> None:
    """Configure structlog and stdlib logging for production.

    Parameters
    ----------
    level
        Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    log_dir
        Directory for log files. When set, logs are written to
        ``apex_v4.log`` with rotation (10 MB, 5 backups).
        When None, logs go to stdout only.
    json_output
        When True, output JSON lines (production).
        When False, output colored console format (development).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # ── stdlib handler setup ──────────────────────────────────────
    handlers: list[logging.Handler] = []

    # Always add stdout
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    handlers.append(stdout_handler)

    # Add rotating file handler if log_dir is set
    if log_dir is not None:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path / "apex_v4.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(log_level)
        handlers.append(file_handler)

    # ── structlog pipeline ────────────────────────────────────────
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    # Apply to root logger
    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(log_level)

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "asyncio", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
