# /phase-gate — Phase Completion Quality Check

Verify that a build phase meets its exit criteria before advancing.

## Protocol

1. Read `APEX_V4_STRATEGY.md` Section 8 — find exit criteria for the phase
2. Run all tests for that phase
3. Check each criterion:
   - Phase 0: `pytest tests/unit/test_v3_fixes.py` → 4 PASSED
   - Phase 1: `pytest tests/unit/test_phase1.py` → ALL PASSED + MarketSnapshot flows E2E
   - Phase 2: Backtest regime distribution within expected ranges
   - Phase 3: `pytest tests/chaos/` → ALL PASSED
   - Phase 4: Full integration test passes in simulation
   - Phase 5: 7 days paper trading, 0 crashes, 0 state drift
   - Phase 6: 10-item go-live checklist 100% complete
4. Report: GATE PASSED or GATE FAILED with blocking items

## Arguments

Pass phase number: `/phase-gate 1`
