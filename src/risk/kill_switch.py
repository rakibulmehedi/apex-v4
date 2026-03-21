"""
src/risk/kill_switch.py — Three-level kill switch (ADR-005).

Phase 3 (P3.4): Implementation pending.
Levels:
  SOFT      → no new signals
  HARD      → flatten all positions
  EMERGENCY → disconnect broker, alert, write state to disk

State managed via asyncio.Lock() — NOT plain boolean (ADR-005).
Persisted to Redis AND PostgreSQL kill_switch_events table.
"""
# TODO: Phase 3 — P3.4 implementation
