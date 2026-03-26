"""Unit tests for startup pre-flight validation in src/pipeline.py.

Uses in-memory SQLite so DB checks run without a real PostgreSQL/Redis.
MT5 and Redis checks are validated via monkeypatching / mocks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.pipeline import (
    PreflightResult,
    _check_capital_allocation,
    _check_kill_switch,
    _check_mt5,
    _check_no_state_drift,
    _check_paper_duration,
    _check_postgres,
    _check_redis,
    _check_secrets_env,
    _check_segment_counts,
    _check_v3_data_imported,
    run_preflight,
)


# ── DDL for in-memory SQLite ──────────────────────────────────────────────

_TRADE_OUTCOMES_DDL = """
CREATE TABLE trade_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair VARCHAR(6) NOT NULL,
    strategy VARCHAR(20) NOT NULL,
    regime VARCHAR(20) NOT NULL,
    session VARCHAR(20) NOT NULL,
    direction VARCHAR(10) NOT NULL,
    entry_price FLOAT NOT NULL,
    exit_price FLOAT NOT NULL,
    r_multiple FLOAT NOT NULL,
    won BOOLEAN NOT NULL,
    fill_id BIGINT,
    opened_at DATETIME NOT NULL,
    closed_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

_KILL_SWITCH_EVENTS_DDL = """
CREATE TABLE kill_switch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms BIGINT NOT NULL,
    level VARCHAR(20) NOT NULL,
    previous_state VARCHAR(20) NOT NULL,
    new_state VARCHAR(20) NOT NULL,
    reason TEXT NOT NULL,
    broker_state_mismatch BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

_RECONCILIATION_LOG_DDL = """
CREATE TABLE reconciliation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_ms BIGINT NOT NULL,
    redis_positions TEXT NOT NULL,
    mt5_positions TEXT NOT NULL,
    mismatch_detected BOOLEAN NOT NULL,
    positions_diverged TEXT,
    action_taken VARCHAR(20),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

# All required tables for Check 5
_ALL_TABLES_DDL = [
    _TRADE_OUTCOMES_DDL,
    _KILL_SWITCH_EVENTS_DDL,
    _RECONCILIATION_LOG_DDL,
    """CREATE TABLE market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair VARCHAR(6), timestamp_ms BIGINT, candles TEXT,
        spread_points FLOAT, session VARCHAR(20), is_stale BOOLEAN,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE candles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair VARCHAR(6), timeframe VARCHAR(10), timestamp_ms BIGINT,
        open FLOAT, high FLOAT, low FLOAT, close FLOAT, volume FLOAT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE feature_vectors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair VARCHAR(6), timestamp_ms BIGINT,
        atr_14 FLOAT, adx_14 FLOAT, ema_200 FLOAT,
        bb_upper FLOAT, bb_lower FLOAT, bb_mid FLOAT,
        session VARCHAR(20), spread_ok BOOLEAN, news_blackout BOOLEAN,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE fills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id BIGINT, pair VARCHAR(6), direction VARCHAR(10),
        strategy VARCHAR(20), regime VARCHAR(20),
        requested_size FLOAT, actual_size FLOAT,
        requested_price FLOAT, actual_fill_price FLOAT,
        slippage_points FLOAT, filled_at DATETIME,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""",
]


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_session_factory(*ddl_statements: str) -> sessionmaker:
    """Create in-memory SQLite with the given tables."""
    engine = create_engine("sqlite://", echo=False)
    with engine.connect() as conn:
        for ddl in ddl_statements:
            conn.execute(text(ddl))
        conn.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_v3_trades(sf: sessionmaker, count: int = 1) -> None:
    """Insert V3 historical trades (fill_id=NULL)."""
    now = datetime.now(timezone.utc)
    with sf() as db:
        for i in range(count):
            db.execute(text(
                "INSERT INTO trade_outcomes "
                "(pair,strategy,regime,session,direction,entry_price,exit_price,"
                "r_multiple,won,fill_id,opened_at,closed_at) VALUES "
                "(:p,:s,:rg,:se,:d,:ep,:xp,:rm,:w,NULL,:oa,:ca)"
            ), {
                "p": "EURUSD", "s": "MOMENTUM", "rg": "TRENDING_UP",
                "se": "LONDON", "d": "LONG",
                "ep": 1.1000 + i * 0.001, "xp": 1.1050 + i * 0.001,
                "rm": 1.5, "w": True,
                "oa": now - timedelta(days=10),
                "ca": now - timedelta(days=10 - i),
            })
        db.commit()


def _seed_all_segments(sf: sessionmaker, trades_per_segment: int = 30) -> None:
    """Seed every active segment with the given number of trades."""
    strategies = ["MOMENTUM", "MEAN_REVERSION"]
    regimes = ["TRENDING_UP", "TRENDING_DOWN", "RANGING"]
    sessions = ["LONDON", "NY", "ASIA", "OVERLAP"]
    now = datetime.now(timezone.utc)

    with sf() as db:
        for strat in strategies:
            for regime in regimes:
                for sess in sessions:
                    for i in range(trades_per_segment):
                        db.execute(text(
                            "INSERT INTO trade_outcomes "
                            "(pair,strategy,regime,session,direction,entry_price,exit_price,"
                            "r_multiple,won,fill_id,opened_at,closed_at) VALUES "
                            "(:p,:s,:rg,:se,:d,:ep,:xp,:rm,:w,NULL,:oa,:ca)"
                        ), {
                            "p": "EURUSD", "s": strat, "rg": regime, "se": sess,
                            "d": "LONG",
                            "ep": 1.1000, "xp": 1.1050,
                            "rm": 1.5, "w": True,
                            "oa": now - timedelta(days=20),
                            "ca": now - timedelta(days=3),
                        })
        db.commit()


# ── Check 1: V3 data imported ────────────────────────────────────────────


class TestCheckV3DataImported:
    def test_pass_when_v3_rows_exist(self):
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        _seed_v3_trades(sf, count=5)
        result = _check_v3_data_imported(sf)
        assert result.passed is True
        assert "5 V3 rows" in result.detail

    def test_fail_when_no_v3_rows(self):
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        result = _check_v3_data_imported(sf)
        assert result.passed is False
        assert "No V3 historical" in result.detail

    def test_fail_when_only_live_trades(self):
        """Trades with fill_id set are NOT V3 — should fail."""
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        now = datetime.now(timezone.utc)
        with sf() as db:
            db.execute(text(
                "INSERT INTO trade_outcomes "
                "(pair,strategy,regime,session,direction,entry_price,exit_price,"
                "r_multiple,won,fill_id,opened_at,closed_at) VALUES "
                "(:p,:s,:rg,:se,:d,:ep,:xp,:rm,:w,:fid,:oa,:ca)"
            ), {
                "p": "EURUSD", "s": "MOMENTUM", "rg": "TRENDING_UP",
                "se": "LONDON", "d": "LONG",
                "ep": 1.1000, "xp": 1.1050,
                "rm": 1.5, "w": True, "fid": 12345,
                "oa": now - timedelta(days=10),
                "ca": now - timedelta(days=3),
            })
            db.commit()
        result = _check_v3_data_imported(sf)
        assert result.passed is False


# ── Check 2: Segment counts ──────────────────────────────────────────────


class TestCheckSegmentCounts:
    def test_pass_when_all_segments_have_30(self):
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        _seed_all_segments(sf, trades_per_segment=30)
        result = _check_segment_counts(sf)
        assert result.passed is True
        assert "24 active segments" in result.detail

    def test_fail_when_segments_below_30(self):
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        # Only seed 5 trades in one segment
        _seed_v3_trades(sf, count=5)
        result = _check_segment_counts(sf)
        assert result.passed is False
        assert "segment(s) below minimum" in result.detail

    def test_fail_lists_thin_segments(self):
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        _seed_all_segments(sf, trades_per_segment=10)
        result = _check_segment_counts(sf)
        assert result.passed is False
        # All 24 segments should appear since 10 < 30
        assert "24 segment(s)" in result.detail


# ── Check 3: Kill switch ─────────────────────────────────────────────────


class TestCheckKillSwitch:
    def test_pass_when_no_events(self):
        sf = _make_session_factory(_KILL_SWITCH_EVENTS_DDL)
        result = _check_kill_switch(sf)
        assert result.passed is True

    def test_pass_when_last_event_is_none(self):
        sf = _make_session_factory(_KILL_SWITCH_EVENTS_DDL)
        with sf() as db:
            db.execute(text(
                "INSERT INTO kill_switch_events "
                "(timestamp_ms,level,previous_state,new_state,reason,broker_state_mismatch) "
                "VALUES (1000,'SOFT','NONE','SOFT','test',0)"
            ))
            db.execute(text(
                "INSERT INTO kill_switch_events "
                "(timestamp_ms,level,previous_state,new_state,reason,broker_state_mismatch) "
                "VALUES (2000,'SOFT','SOFT','NONE','manual reset',0)"
            ))
            db.commit()
        result = _check_kill_switch(sf)
        assert result.passed is True

    def test_fail_when_active(self):
        sf = _make_session_factory(_KILL_SWITCH_EVENTS_DDL)
        with sf() as db:
            db.execute(text(
                "INSERT INTO kill_switch_events "
                "(timestamp_ms,level,previous_state,new_state,reason,broker_state_mismatch) "
                "VALUES (1000,'HARD','NONE','HARD','drawdown breach',0)"
            ))
            db.commit()
        result = _check_kill_switch(sf)
        assert result.passed is False
        assert "HARD" in result.detail


# ── Check 4: Redis ────────────────────────────────────────────────────────


class TestCheckRedis:
    def test_pass_when_redis_reachable(self):
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        with patch("src.pipeline.redis.Redis.from_url", return_value=mock_redis):
            result = _check_redis({})
        assert result.passed is True
        assert "PING OK" in result.detail

    def test_fail_when_redis_down(self):
        with patch("src.pipeline.redis.Redis.from_url", side_effect=ConnectionError("refused")):
            result = _check_redis({})
        assert result.passed is False
        assert "unreachable" in result.detail


# ── Check 5: PostgreSQL + tables ──────────────────────────────────────────


class TestCheckPostgres:
    def test_pass_when_all_tables_exist(self):
        sf = _make_session_factory(*_ALL_TABLES_DDL)
        result = _check_postgres(sf)
        assert result.passed is True
        assert "7 tables" in result.detail

    def test_fail_when_tables_missing(self):
        # Only create trade_outcomes
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        result = _check_postgres(sf)
        assert result.passed is False
        assert "Missing tables" in result.detail


# ── Check 6: MT5 ─────────────────────────────────────────────────────────


class TestCheckMT5:
    def test_pass_when_account_info_returns_data(self):
        mock_mt5 = MagicMock()
        mock_mt5.account_info.return_value = MagicMock(
            login=12345, server="Demo", equity=10000.0,
        )
        with patch("src.pipeline.get_mt5_client", return_value=mock_mt5):
            result = _check_mt5({"mt5": {"mode": "stub"}})
        assert result.passed is True
        assert "12345" in result.detail

    def test_fail_when_account_info_none(self):
        mock_mt5 = MagicMock()
        mock_mt5.account_info.return_value = None
        with patch("src.pipeline.get_mt5_client", return_value=mock_mt5):
            result = _check_mt5({"mt5": {"mode": "stub"}})
        assert result.passed is False
        assert "returned None" in result.detail

    def test_fail_when_initialize_throws(self):
        with patch("src.pipeline.get_mt5_client", side_effect=RuntimeError("no terminal")):
            result = _check_mt5({"mt5": {"mode": "real"}})
        assert result.passed is False
        assert "failed" in result.detail


# ── Check 7: Paper trading duration ──────────────────────────────────────


class TestCheckPaperDuration:
    def test_pass_when_7_days(self):
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        now = datetime.now(timezone.utc)
        with sf() as db:
            db.execute(text(
                "INSERT INTO trade_outcomes "
                "(pair,strategy,regime,session,direction,entry_price,exit_price,"
                "r_multiple,won,fill_id,opened_at,closed_at) VALUES "
                "(:p,:s,:rg,:se,:d,:ep,:xp,:rm,:w,NULL,:oa,:ca)"
            ), {
                "p": "EURUSD", "s": "MOMENTUM", "rg": "TRENDING_UP",
                "se": "LONDON", "d": "LONG",
                "ep": 1.1, "xp": 1.105, "rm": 1.5, "w": True,
                "oa": now - timedelta(days=10),
                "ca": now - timedelta(days=1),
            })
            db.commit()
        result = _check_paper_duration(sf)
        assert result.passed is True
        assert "9 days" in result.detail

    def test_fail_when_less_than_7_days(self):
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        now = datetime.now(timezone.utc)
        with sf() as db:
            db.execute(text(
                "INSERT INTO trade_outcomes "
                "(pair,strategy,regime,session,direction,entry_price,exit_price,"
                "r_multiple,won,fill_id,opened_at,closed_at) VALUES "
                "(:p,:s,:rg,:se,:d,:ep,:xp,:rm,:w,NULL,:oa,:ca)"
            ), {
                "p": "EURUSD", "s": "MOMENTUM", "rg": "TRENDING_UP",
                "se": "LONDON", "d": "LONG",
                "ep": 1.1, "xp": 1.105, "rm": 1.5, "w": True,
                "oa": now - timedelta(days=3),
                "ca": now - timedelta(days=1),
            })
            db.commit()
        result = _check_paper_duration(sf)
        assert result.passed is False
        assert "2 day(s)" in result.detail

    def test_fail_when_no_trades(self):
        sf = _make_session_factory(_TRADE_OUTCOMES_DDL)
        result = _check_paper_duration(sf)
        assert result.passed is False
        assert "not started" in result.detail


# ── Check 8: State drift ─────────────────────────────────────────────────


class TestCheckNoStateDrift:
    def test_pass_when_no_drift(self):
        sf = _make_session_factory(_RECONCILIATION_LOG_DDL)
        result = _check_no_state_drift(sf)
        assert result.passed is True

    def test_pass_when_only_clean_heartbeats(self):
        sf = _make_session_factory(_RECONCILIATION_LOG_DDL)
        with sf() as db:
            db.execute(text(
                "INSERT INTO reconciliation_log "
                "(timestamp_ms,redis_positions,mt5_positions,mismatch_detected) "
                "VALUES (1000,'[]','[]',0)"
            ))
            db.commit()
        result = _check_no_state_drift(sf)
        assert result.passed is True

    def test_fail_when_drift_detected(self):
        sf = _make_session_factory(_RECONCILIATION_LOG_DDL)
        with sf() as db:
            db.execute(text(
                "INSERT INTO reconciliation_log "
                "(timestamp_ms,redis_positions,mt5_positions,mismatch_detected,"
                "positions_diverged,action_taken) "
                "VALUES (:ts,:rp,:mp,:mm,:pd,:at)"
            ), {"ts": 1000, "rp": "[]", "mp": '[{"ticket":1}]',
                "mm": 1, "pd": '{"ghost":[1]}', "at": "HARD"})
            db.commit()
        result = _check_no_state_drift(sf)
        assert result.passed is False
        assert "1 unresolved" in result.detail


# ── Check 9: capital_allocation_pct ───────────────────────────────────────


class TestCheckCapitalAllocation:
    def test_pass_valid_value(self):
        result = _check_capital_allocation({"risk": {"capital_allocation_pct": 0.10}})
        assert result.passed is True
        assert "0.1" in result.detail

    def test_fail_missing(self):
        result = _check_capital_allocation({"risk": {}})
        assert result.passed is False
        assert "missing" in result.detail

    def test_fail_missing_risk_section(self):
        result = _check_capital_allocation({})
        assert result.passed is False

    def test_fail_out_of_range_zero(self):
        result = _check_capital_allocation({"risk": {"capital_allocation_pct": 0.0}})
        assert result.passed is False
        assert "(0.0, 1.0]" in result.detail

    def test_fail_out_of_range_negative(self):
        result = _check_capital_allocation({"risk": {"capital_allocation_pct": -0.5}})
        assert result.passed is False

    def test_fail_out_of_range_over_1(self):
        result = _check_capital_allocation({"risk": {"capital_allocation_pct": 1.5}})
        assert result.passed is False

    def test_pass_exactly_1(self):
        result = _check_capital_allocation({"risk": {"capital_allocation_pct": 1.0}})
        assert result.passed is True

    def test_fail_non_numeric(self):
        result = _check_capital_allocation({"risk": {"capital_allocation_pct": "abc"}})
        assert result.passed is False
        assert "not a valid number" in result.detail


# ── Check 10: secrets.env ────────────────────────────────────────────────


class TestCheckSecretsEnv:
    def test_pass_all_credentials_set(self, tmp_path):
        secrets = tmp_path / "secrets.env"
        secrets.write_text(
            "MT5_LOGIN=12345\n"
            "MT5_PASSWORD=hunter2\n"
            "MT5_SERVER=MetaQuotes-Demo\n"
        )
        result = _check_secrets_env(secrets)
        assert result.passed is True

    def test_fail_file_missing(self, tmp_path):
        result = _check_secrets_env(tmp_path / "nonexistent.env")
        assert result.passed is False
        assert "does not exist" in result.detail

    def test_fail_empty_values(self, tmp_path):
        secrets = tmp_path / "secrets.env"
        secrets.write_text(
            "MT5_LOGIN=\n"
            "MT5_PASSWORD=\n"
            "MT5_SERVER=\n"
        )
        result = _check_secrets_env(secrets)
        assert result.passed is False
        assert "empty values" in result.detail

    def test_fail_missing_keys(self, tmp_path):
        secrets = tmp_path / "secrets.env"
        secrets.write_text("MT5_LOGIN=12345\n")
        result = _check_secrets_env(secrets)
        assert result.passed is False
        assert "MT5_PASSWORD" in result.detail
        assert "MT5_SERVER" in result.detail

    def test_ignores_comments_and_blanks(self, tmp_path):
        secrets = tmp_path / "secrets.env"
        secrets.write_text(
            "# Apex V4 secrets\n"
            "\n"
            "MT5_LOGIN=12345\n"
            "MT5_PASSWORD=hunter2\n"
            "MT5_SERVER=MetaQuotes-Demo\n"
            "\n"
            "# Postgres\n"
            "POSTGRES_USER=apex\n"
        )
        result = _check_secrets_env(secrets)
        assert result.passed is True


# ── run_preflight() integration ──────────────────────────────────────────


class TestRunPreflight:
    """Test the orchestrator — all checks mocked to isolate the flow."""

    def _all_passing_settings(self) -> dict:
        return {"risk": {"capital_allocation_pct": 0.10}}

    def _make_all_pass_patches(self):
        """Return patches that make all 10 checks pass."""
        ok = PreflightResult(name="stub", passed=True, detail="ok")
        return [
            patch("src.pipeline._check_v3_data_imported", return_value=ok),
            patch("src.pipeline._check_segment_counts", return_value=ok),
            patch("src.pipeline._check_kill_switch", return_value=ok),
            patch("src.pipeline._check_redis", return_value=ok),
            patch("src.pipeline._check_postgres", return_value=ok),
            patch("src.pipeline._check_mt5", return_value=ok),
            patch("src.pipeline._check_paper_duration", return_value=ok),
            patch("src.pipeline._check_no_state_drift", return_value=ok),
            patch("src.pipeline._check_capital_allocation", return_value=ok),
            patch("src.pipeline._check_secrets_env", return_value=ok),
        ]

    def test_all_pass_correct_confirmation(self):
        settings = self._all_passing_settings()
        patches = self._make_all_pass_patches()
        for p in patches:
            p.start()
        try:
            cap = run_preflight(
                settings,
                session_factory=MagicMock(),
                _input_fn=lambda _: "CONFIRMED 0.1",
            )
            assert cap == 0.1
        finally:
            for p in patches:
                p.stop()

    def test_all_pass_wrong_confirmation_aborts(self):
        settings = self._all_passing_settings()
        patches = self._make_all_pass_patches()
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc_info:
                run_preflight(
                    settings,
                    session_factory=MagicMock(),
                    _input_fn=lambda _: "YOLO",
                )
            assert exc_info.value.code == 1
        finally:
            for p in patches:
                p.stop()

    def test_any_check_fails_aborts(self):
        settings = self._all_passing_settings()
        ok = PreflightResult(name="stub", passed=True, detail="ok")
        fail = PreflightResult(name="redis", passed=False, detail="down", fix="start redis")
        patches = [
            patch("src.pipeline._check_v3_data_imported", return_value=ok),
            patch("src.pipeline._check_segment_counts", return_value=ok),
            patch("src.pipeline._check_kill_switch", return_value=ok),
            patch("src.pipeline._check_redis", return_value=fail),  # <-- this one fails
            patch("src.pipeline._check_postgres", return_value=ok),
            patch("src.pipeline._check_mt5", return_value=ok),
            patch("src.pipeline._check_paper_duration", return_value=ok),
            patch("src.pipeline._check_no_state_drift", return_value=ok),
            patch("src.pipeline._check_capital_allocation", return_value=ok),
            patch("src.pipeline._check_secrets_env", return_value=ok),
        ]
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit) as exc_info:
                run_preflight(
                    settings,
                    session_factory=MagicMock(),
                    _input_fn=lambda _: "CONFIRMED 0.1",
                )
            assert exc_info.value.code == 1
        finally:
            for p in patches:
                p.stop()

    def test_eof_aborts(self):
        settings = self._all_passing_settings()
        patches = self._make_all_pass_patches()
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit):
                run_preflight(
                    settings,
                    session_factory=MagicMock(),
                    _input_fn=MagicMock(side_effect=EOFError),
                )
        finally:
            for p in patches:
                p.stop()

    def test_keyboard_interrupt_aborts(self):
        settings = self._all_passing_settings()
        patches = self._make_all_pass_patches()
        for p in patches:
            p.start()
        try:
            with pytest.raises(SystemExit):
                run_preflight(
                    settings,
                    session_factory=MagicMock(),
                    _input_fn=MagicMock(side_effect=KeyboardInterrupt),
                )
        finally:
            for p in patches:
                p.stop()
