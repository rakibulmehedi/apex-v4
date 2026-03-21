# APEX V4

Production-grade hybrid regime-based algorithmic Forex trading system built on MT5.

**Status:** Active build — Phase 0 (V3 bug fixes)

## Architecture

See `APEX_V4_STRATEGY.md` for the full architectural strategy document.

## Setup

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> **macOS requirement:** `brew install ta-lib` before installing TA-Lib Python bindings.

## Build Phases

| Phase | Description | Status |
|---|---|---|
| 0 | V3 bug fixes | Pending |
| 1 | Foundation (DB, schemas, feed, features) | Pending |
| 2 | Alpha engines (regime, momentum, mean reversion) | Pending |
| 3 | Risk engine (calibration, covariance, kill switch) | Pending |
| 4 | Execution + learning loop | Pending |
| 5 | Observability + paper trading | Pending |
| 6 | Live migration | Pending |
