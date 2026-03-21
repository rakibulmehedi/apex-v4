"""
src/risk/governor.py — 7-gate sequential risk governor.

Phase 3 (P3.6): Implementation pending.
Gates (fail-fast, in order):
  Gate 1: Kill switch state check
  Gate 2: Spread OK
  Gate 3: News blackout
  Gate 4: Drawdown scalar (0.0 if dd ≥ 5%)
  Gate 5: Portfolio VaR 99% < 5% (hard), < 3% (soft kill switch)
  Gate 6: Minimum sample gate (≥ 30 trades in segment)
  Gate 7: Positive edge (edge > 0)

Output: RiskDecision (APPROVE | REJECT | REDUCE)
"""
# TODO: Phase 3 — P3.6 implementation
