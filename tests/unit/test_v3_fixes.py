"""
tests/unit/test_v3_fixes.py — Phase 0 exit gate.

Phase 0 (P0.6): 4 tests for V3 critical bug fixes.
Exit criteria: pytest tests/unit/test_v3_fixes.py → 4 PASSED

Fixes under test:
  1. portfolio_value uses mt5.account_info().equity (not hardcoded)
  2. conviction fallback returns 0.0 on failure (not 1.0)
  3. fill recorded only after TRADE_RETCODE_DONE
  4. Kelly is quarter-Kelly + 2% cap + drawdown scalar
"""
# TODO: Phase 0 — P0.6 implementation
