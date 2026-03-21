# /phase-gate — Phase Completion Quality Check

Run the quality gate checklist for the specified phase from
APEX_V4_STRATEGY.md Section 9. Execute ALL tests. Produce a
PASS/FAIL report with evidence for every criterion.

## Arguments

Pass the phase number: `/phase-gate 1`

## Protocol

### 1. Load Phase Criteria
1. Read `APEX_V4_STRATEGY.md` Section 8 (Build Phases) — find the exact
   exit criteria for Phase $ARGUMENTS
2. Read `APEX_V4_STRATEGY.md` Section 9 (Quality Gates) — find the gate
   checklist for this phase
3. Read `tasks/todo.md` — verify all planned work for this phase is marked complete
4. Read `tasks/lessons.md` — check for known issues that could affect the gate

### 2. Execute Test Suite
5. Run all tests for this phase:
   ```
   venv/bin/pytest tests/ -v --tb=short
   ```
6. Capture full output — every pass and every failure is evidence
7. If any tests fail, list each failure with its traceback

### 3. Check Each Gate Criterion
8. For each criterion in the phase gate, verify with evidence:
   - **Test evidence**: which test(s) prove this criterion is met
   - **Code evidence**: which file(s) and line(s) implement this
   - **Manual verification**: anything that can't be tested automatically
9. Be rigorous — "it probably works" is not evidence

### Phase-Specific Checks

**Phase 0 — V3 Bug Fixes:**
- [ ] All 4 critical V3 bugs fixed (Section 1.2)
- [ ] Each fix has its own isolated commit: `fix(v3): <desc>`
- [ ] V3 tests pass after fixes
- [ ] No V3 refactoring, renaming, or feature additions

**Phase 1 — Data & Feature Layer:**
- [ ] MT5 abstraction layer complete with stub and real client
- [ ] MarketSnapshot dataclass populated from MT5 + TA-Lib
- [ ] Feature fabric produces all features from Section 3
- [ ] Redis snapshot store reads/writes correctly
- [ ] All unit tests pass
- [ ] Integration test: MT5 → Features → Redis end-to-end

**Phase 2 — Alpha & Calibration:**
- [ ] Regime detector classifies trending/ranging/volatile correctly
- [ ] Mean reversion and trend alpha signals produce valid output
- [ ] Kelly criterion implementation matches Section 7.1 exactly
- [ ] Calibration engine produces valid position sizes
- [ ] Backtest regime distribution within expected ranges
- [ ] All unit tests pass, all formulas verified via `/risk-verify`

**Phase 3 — Risk & Execution:**
- [ ] Risk governor enforces all limits (VaR, correlation, drawdown)
- [ ] Kill switch operates at all 3 levels
- [ ] Execution manager handles order lifecycle correctly
- [ ] State reconciliation detects and resolves drift
- [ ] Chaos tests pass (network failure, MT5 disconnect, Redis down)
- [ ] All formulas verified via `/risk-verify`

**Phase 4 — Orchestration:**
- [ ] Main loop runs the full pipeline: data → features → alpha → risk → execution
- [ ] Kill switch integration tested end-to-end
- [ ] Health monitoring produces correct status
- [ ] Full integration test passes in simulation mode

**Phase 5 — Paper Trading:**
- [ ] 7 consecutive days of paper trading completed
- [ ] Zero crashes, zero unhandled exceptions
- [ ] Zero state drift between Redis and MT5
- [ ] Performance metrics within expected bounds

**Phase 6 — Go-Live:**
- [ ] 10-item go-live checklist 100% complete
- [ ] `/hardening` passes with PRODUCTION READY status
- [ ] All runbooks documented
- [ ] Monitoring and alerting configured

### 4. Produce Report

```
═══════════════════════════════════════════════
  APEX V4 — PHASE <N> QUALITY GATE
  Date: <date>
═══════════════════════════════════════════════

TEST RESULTS
──────────────────────────────────────────────
Total: <N>  Passed: <N>  Failed: <N>  Skipped: <N>

GATE CRITERIA
──────────────────────────────────────────────
1. [PASS|FAIL] <criterion> — <evidence>
2. [PASS|FAIL] <criterion> — <evidence>
...

BLOCKING ITEMS (if any)
──────────────────────────────────────────────
1. <item> — <what needs to happen>
...

RESULT: GATE PASSED ✓ | GATE FAILED ✗
──────────────────────────────────────────────
<summary statement>
```

## Rules

- A single FAIL on any criterion means GATE FAILED — no exceptions
- "Not yet implemented" counts as FAIL, not SKIP
- Every PASS must cite evidence (test name, file:line, or output)
- If the gate fails, list the exact blocking items with what must be done
