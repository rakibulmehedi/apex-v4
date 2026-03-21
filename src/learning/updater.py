"""
src/learning/updater.py — Segment stats recalculator + Redis cache updater.

Phase 4 (P4.4): Implementation pending.
After each trade outcome is recorded:
  - Recalculate p_win, avg_R for affected segment
  - Update Redis segment:{strategy}:{regime}:{session} cache (TTL 3600s)
  - Update Kelly inputs for that segment (rolling 90-day window)
"""
# TODO: Phase 4 — P4.4 implementation
