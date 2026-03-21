# /implement — Autonomous Module Implementation

You are implementing a module for APEX V4.

## Protocol

1. Read `APEX_V4_STRATEGY.md` — find the exact spec for this module
2. Read `tasks/lessons.md` — apply all accumulated rules
3. Write implementation plan to `tasks/todo.md` with checkable items
4. Implement following code standards:
   - Python 3.11, PEP 8, full type hints on all public functions
   - structlog only — no bare print() in src/
   - All external deps (MT5, Redis, PostgreSQL) injected — never instantiated inside module
5. Write unit tests in `tests/unit/` — mock all external dependencies
6. Run tests: `venv/bin/pytest <test_file> -v`
7. All tests pass → mark tasks complete
8. Run `/risk-verify` if module contains any formulas from Section 7

## Arguments

Pass the module path: `/implement src/features/fabric.py`
