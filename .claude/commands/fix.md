# /fix — Autonomous Bug Diagnosis and Repair

You are diagnosing and fixing a bug in APEX V4. Never ask for guidance.
Reason through it autonomously. Find the root cause, fix it, prove it's fixed.

## Arguments

Pass the error description, failing test, or log output:
- `/fix tests/unit/test_risk.py::test_kelly`
- `/fix "RuntimeError: Redis connection pool exhausted during reconciliation"`
- `/fix src/execution/manager.py — positions not closing on kill switch`

## Iron Law

**No fix is applied without identifying the root cause.**
If root cause is unclear after initial investigation, add diagnostic logging
or assertions first, reproduce, THEN fix.

## Protocol

### 1. Context Load
1. Read `tasks/lessons.md` — check if this bug pattern is already known
2. If a test path was given, read the test file
3. If a source file was given, read the source file
4. If an error message was given, grep the codebase for the origin

### 2. Reproduce
5. Run the failing test or replicate the error condition:
   `venv/bin/pytest <test_path> -v` or equivalent
6. Capture the full traceback and error message
7. If the error cannot be reproduced, investigate why — do not guess

### 3. Diagnose
8. Trace the error from symptom to root cause:
   - Read every file in the call chain
   - Check types, state assumptions, race conditions
   - Check if the bug is in the code or in the test
9. Form a hypothesis about root cause
10. Verify the hypothesis — don't just assume you're right
11. If multiple possible causes exist, add targeted assertions to narrow down

### 4. Fix
12. Fix the **root cause**, not the symptom
13. If the fix is in application code:
    - Ensure the fix doesn't break the module's contract
    - Check if the same pattern exists elsewhere (fix those too)
14. If the fix is in test code:
    - Ensure the test is actually testing the right behavior
    - Don't weaken the test to make it pass
15. Follow all code standards (type hints, structlog, no hardcoded values)

### 5. Verify
16. Re-run the originally failing test: `venv/bin/pytest <test_path> -v`
17. Run the full test suite to check for regressions: `venv/bin/pytest tests/ -v`
18. All tests must pass. If new failures appear, fix those too.

### 6. Close
19. Update `tasks/lessons.md` with:
    - The bug pattern (what went wrong)
    - The root cause (why it went wrong)
    - The rule to prevent recurrence
20. Update `tasks/todo.md` if this fix was part of a tracked task
21. Prepare commit message: `fix: <description of what was fixed and why>`

## Anti-Patterns (Never Do These)

- Silencing an exception to make a test pass
- Weakening an assertion to avoid a failure
- Adding a `try/except: pass` without handling the error
- Fixing symptoms while leaving root cause intact
- Guessing at a fix without reproducing first
