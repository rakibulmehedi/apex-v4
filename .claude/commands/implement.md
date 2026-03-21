# /implement — Autonomous Module Implementation

You are implementing a component for APEX V4. Operate fully autonomously —
no asking for guidance, no stopping for clarification. Read the spec, build it,
test it, verify it.

## Arguments

Pass the phase and component: `/implement Phase 1 src/features/fabric.py`
If only a file path is given, infer the phase from APEX_V4_STRATEGY.md Section 8.

## Protocol

### 1. Context Load
1. Read `APEX_V4_STRATEGY.md` — find the **exact** spec for this component
2. Read `tasks/lessons.md` — apply all accumulated rules
3. Read `tasks/todo.md` — check for prior work or dependencies
4. Identify all interfaces this component depends on or exposes

### 2. Plan
5. Write a detailed implementation plan to `tasks/todo.md` with checkable items
6. List every class, method, and data flow you will create
7. Identify which formulas from Section 7 (if any) are involved
8. Identify all dependencies that must be injected (MT5, Redis, DB, etc.)

### 3. Implement
9. Follow code standards strictly:
   - Python 3.11, PEP 8, full type hints on ALL public functions
   - structlog only — no bare `print()` anywhere in `src/`
   - All external deps (MT5, Redis, PostgreSQL) **injected** — never instantiated inside
   - Secrets from environment variables only — never hardcoded
   - stdlib → third-party → internal imports, one blank line between groups
10. Build the component method by method
11. After each logical unit, re-read what you wrote — catch bugs before they exist
12. If the component touches risk or calibration formulas, implement them **exactly**
    as written in Section 7 — no approximations, no silent deviations

### 4. Test
13. Write comprehensive unit tests in `tests/unit/`
14. Mock ALL external dependencies — no real MT5, Redis, or DB in unit tests
15. Cover: happy path, edge cases, error paths, boundary conditions
16. Run tests: `venv/bin/pytest <test_file> -v`
17. All tests must pass. If any fail, diagnose and fix — do not stop and ask.

### 5. Verify
18. Run the full test suite to check for regressions: `venv/bin/pytest tests/ -v`
19. If this component contains Section 7 formulas, run `/risk-verify`
20. Re-read `APEX_V4_STRATEGY.md` for this component — confirm nothing was missed
21. Ask yourself: "Would a staff engineer approve this?"

### 6. Close
22. Mark all tasks complete in `tasks/todo.md`
23. Write a review section summarizing what was built, test results, and any decisions made
24. Update `tasks/lessons.md` if any new patterns were discovered

## Quality Bar

- Zero failing tests
- 100% type hint coverage on public functions
- No hardcoded secrets or configuration
- All external deps injected, never imported at module level
- Every formula matches Section 7 exactly (if applicable)
- structlog used for all logging
