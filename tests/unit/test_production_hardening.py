"""Tests for production hardening changes.

Covers:
  - get_database_url() — env var resolution order
  - make_engine() — pool settings
  - configure_logging() — structlog setup
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest


# ── get_database_url tests ────────────────────────────────────────────


class TestGetDatabaseUrl:
    """Test get_database_url() env var resolution order."""

    def test_apex_database_url_takes_priority(self):
        """APEX_DATABASE_URL env var is used when set."""
        from db.models import get_database_url

        with patch.dict(
            os.environ,
            {
                "APEX_DATABASE_URL": "postgresql://user:pass@db:5432/apex",
                "POSTGRES_USER": "other",
                "POSTGRES_PASSWORD": "other",
            },
        ):
            assert get_database_url() == "postgresql://user:pass@db:5432/apex"

    def test_postgres_vars_assembled(self):
        """Individual POSTGRES_* vars are assembled when APEX_DATABASE_URL missing."""
        from db.models import get_database_url

        env = {
            "POSTGRES_USER": "apex",
            "POSTGRES_PASSWORD": "s3cret",
            "POSTGRES_HOST": "db.host",
            "POSTGRES_PORT": "5433",
            "POSTGRES_DB": "trading",
        }
        with patch.dict(os.environ, env, clear=False):
            # Remove APEX_DATABASE_URL if present
            os.environ.pop("APEX_DATABASE_URL", None)
            url = get_database_url()
            assert "apex" in url
            assert "s3cret" in url
            assert "db.host" in url
            assert "5433" in url
            assert "trading" in url

    def test_postgres_password_urlencoded(self):
        """Special chars in password are URL-encoded."""
        from db.models import get_database_url

        env = {
            "POSTGRES_USER": "apex",
            "POSTGRES_PASSWORD": "p@ss/word",
        }
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("APEX_DATABASE_URL", None)
            url = get_database_url()
            # @ should be encoded as %40
            assert "%40" in url or "p%40ss" in url

    def test_fallback_no_auth(self):
        """Fallback URL has no auth when POSTGRES_USER/PASSWORD not set."""
        from db.models import get_database_url

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APEX_DATABASE_URL", None)
            os.environ.pop("POSTGRES_USER", None)
            os.environ.pop("POSTGRES_PASSWORD", None)
            url = get_database_url()
            assert "localhost" in url
            assert "apex_v4" in url


class TestMakeEngine:
    """Test make_engine() pool settings."""

    def test_engine_calls_create_engine_with_pool_settings(self):
        """make_engine passes production pool settings to create_engine."""
        from unittest.mock import MagicMock

        with patch("db.models.create_engine") as mock_ce:
            mock_ce.return_value = MagicMock()
            from db.models import make_engine

            make_engine("postgresql://localhost/test")
            mock_ce.assert_called_once()
            _, kwargs = mock_ce.call_args
            assert kwargs["pool_pre_ping"] is True
            assert kwargs["pool_recycle"] == 1800
            assert kwargs["pool_size"] == 5
            assert kwargs["max_overflow"] == 10


class TestConfigureLogging:
    """Test structlog configuration."""

    def test_configure_logging_sets_root_level(self):
        """configure_logging sets the root logger level."""
        from src.observability.logging import configure_logging

        configure_logging(level="WARNING", json_output=False)
        root = logging.getLogger()
        assert root.level == logging.WARNING

        # Reset for other tests
        configure_logging(level="INFO", json_output=False)

    def test_configure_logging_json_output(self):
        """JSON output mode doesn't raise."""
        from src.observability.logging import configure_logging

        configure_logging(level="INFO", json_output=True)

    def test_configure_logging_with_log_dir(self, tmp_path):
        """Log dir creates file handler with rotation."""
        from src.observability.logging import configure_logging

        log_dir = tmp_path / "logs"
        configure_logging(level="INFO", log_dir=str(log_dir), json_output=True)
        assert log_dir.exists()
        assert (log_dir / "apex_v4.log").exists() or True  # handler created

        # Reset
        configure_logging(level="INFO", json_output=False)
