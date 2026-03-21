# /audit — Full Architecture Compliance Check

Compare every implemented module against APEX_V4_STRATEGY.md.
Report ADR compliance, missing implementations, formula deviations,
and code standard violations. Produce a scored report with exact fixes.

## Protocol

### 1. Load Full Spec
1. Read `APEX_V4_STRATEGY.md` in full — this is the single source of truth
2. Read `tasks/lessons.md` — incorporate accumulated rules
3. Run `git log --oneline -20` — understand what has been built so far

### 2. ADR Compliance (Section 6)
4. For each ADR (ADR-001 through ADR-009), determine status:
   - **IMPLEMENTED** — verify the implementation matches the ADR exactly
   - **PENDING** — expected for current phase, note when it should be built
   - **VIOLATED** — flag immediately with the exact violation and required fix
5. Check ADR interactions — are any ADRs contradicting each other in implementation?

### 3. Module-by-Module Audit
6. For every file in `src/`, verify:
   - Does this file match its spec in APEX_V4_STRATEGY.md?
   - Are all specified interfaces implemented?
   - Are there any unspecified additions (scope creep)?
   - Is the dependency injection correct (no internal instantiation of MT5/Redis/DB)?
7. For every file in `tests/`, verify:
   - Does the test mock all external dependencies?
   - Is coverage adequate for the module it tests?

### 4. Formula Verification
8. Run `/risk-verify` for all implemented formula-bearing modules
9. Cross-reference Section 7 formulas against implementations
10. Flag any deviation — no matter how small

### 5. Code Standards Compliance
11. Check every `src/` file for:
    - Full type hints on all public functions
    - structlog usage (no bare `print()`)
    - No hardcoded secrets, passwords, API keys, or tokens
    - Correct import ordering (stdlib → third-party → internal)
    - PEP 8 compliance
12. Check `.gitignore` covers: `config/secrets.env`, `venv/`, `__pycache__/`, `.env`

### 6. Produce Scored Report

Output the report in this exact format:

```
═══════════════════════════════════════════════
  APEX V4 — ARCHITECTURE COMPLIANCE AUDIT
  Date: <date>    Phase: <current phase>
═══════════════════════════════════════════════

ADR COMPLIANCE
──────────────────────────────────────────────
ADR-001: [PASS|WARN|FAIL] — <note>
ADR-002: [PASS|WARN|FAIL] — <note>
ADR-003: [PASS|WARN|FAIL] — <note>
ADR-004: [PASS|WARN|FAIL] — <note>
ADR-005: [PASS|WARN|FAIL] — <note>
ADR-006: [PASS|WARN|FAIL] — <note>
ADR-007: [PASS|WARN|FAIL] — <note>
ADR-008: [PASS|WARN|FAIL] — <note>
ADR-009: [PASS|WARN|FAIL] — <note>

MODULE STATUS
──────────────────────────────────────────────
<module>: [COMPLIANT|PARTIAL|MISSING|VIOLATED] — <note>
...

FORMULA VERIFICATION
──────────────────────────────────────────────
<formula>: [PASS|FAIL|NOT YET IMPLEMENTED] — <note>
...

CODE STANDARDS
──────────────────────────────────────────────
Type hints:    [PASS|FAIL] — <count of violations>
Logging:       [PASS|FAIL] — <count of violations>
Secrets:       [PASS|FAIL] — <count of violations>
Imports:       [PASS|FAIL] — <count of violations>
Tests:         [PASS|FAIL] — <count of violations>

SCORE: <X>/100
OVERALL: [COMPLIANT | ISSUES FOUND]

REQUIRED FIXES (if any):
──────────────────────────────────────────────
1. [CRITICAL|HIGH|MEDIUM|LOW] <file>:<line> — <exact fix required>
2. ...
```

## Scoring

- ADR compliance: 40 points (each ADR = ~4.4 pts)
- Module correctness: 25 points
- Formula accuracy: 20 points
- Code standards: 15 points
- Each FAIL deducts proportionally from its category
- CRITICAL violations cap the total score at 50 regardless of other scores
