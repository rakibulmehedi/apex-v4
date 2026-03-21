"""
src/calibration/engine.py — Edge calculation + position sizing.

Phase 3 (P3.2): Implementation pending.
Formula locked in APEX_V4_STRATEGY.md Section 7.1:
  f* = (p × b - q) / b
  f_quarter = f* × 0.25
  f_final = min(f_quarter, 0.02)
  size = f_final × dd_scalar × correlation_scalar × portfolio_equity

Output: CalibratedTradeIntent (or None if edge ≤ 0)
"""
# TODO: Phase 3 — P3.2 implementation
