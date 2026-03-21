# /fix — Autonomous Bug Diagnosis and Repair

You are diagnosing and fixing a bug in APEX V4.

## Protocol

1. Read `tasks/lessons.md` — check if this bug pattern is known
2. Read the failing file and its test
3. Run the failing test to confirm the error
4. Diagnose root cause — no guessing, no assumptions
5. Fix the root cause — not symptoms
6. Re-run tests — all must pass
7. Update `tasks/lessons.md` if this was a new pattern
8. Commit: `fix: <description>`

## Iron Law

No fix is applied without identifying the root cause.
If root cause is unclear, add diagnostic logging first — then fix.

## Arguments

Pass the bug description or failing test path: `/fix tests/unit/test_risk.py::test_kelly`
