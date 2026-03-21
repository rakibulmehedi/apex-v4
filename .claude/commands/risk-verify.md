# /risk-verify — Mathematical Formula Verification

Extract every formula from the risk engine and calibration engine.
Verify each against Section 7 of APEX_V4_STRATEGY.md exactly.
Flag every deviation with the correct formula and required fix.

## Protocol

### 1. Load Reference Formulas
1. Read `APEX_V4_STRATEGY.md` Section 7 (Mathematics Reference) in full
2. Extract every formula, constant, coefficient, boundary condition, and
   special case documented in Section 7
3. Create a checklist of every mathematical element to verify

### 2. Extract Implementations
4. For each formula, locate the implementation:

| Formula | Section | Expected Location |
|---|---|---|
| Kelly Criterion | 7.1 | `src/calibration/engine.py` |
| OU Process MLE | 7.2 | `src/alpha/mean_reversion.py` |
| Conviction Score | 7.3 | `src/alpha/mean_reversion.py` |
| EWMA Covariance | 7.4 | `src/risk/covariance.py` |
| Portfolio VaR (99%) | 7.5 | `src/risk/governor.py` |

5. If a formula is implemented in a different location, find it via grep
6. Read the full implementation of each formula

### 3. Element-by-Element Comparison
7. For each formula, verify **every** element:
   - Operators: `+`, `-`, `*`, `/`, `**` — exact match
   - Constants: `0.5`, `2.0`, `0.99`, etc. — exact match
   - Variable names: do they map correctly to the spec variables?
   - Boundary conditions: division by zero guards, min/max clamps
   - Return values: correct type and scale
   - Special cases: what happens at extremes (0, negative, very large)?
8. Check for common implementation errors:
   - Integer division where float was intended
   - Missing parentheses changing operator precedence
   - Off-by-one in array indexing for time series
   - Using sample vs population variance incorrectly
   - Log vs ln confusion

### 4. Deviation Classification
9. Classify each deviation found:
   - **SILENT DEVIATION**: Formula differs from spec with no comment — **UNACCEPTABLE**
   - **DOCUMENTED DEVIATION**: Differs but has a code comment explaining why — **REVIEW**
   - **APPROXIMATION**: Rounding or simplification without justification — **UNACCEPTABLE**
   - **ENHANCEMENT**: Additional safety check not in spec — **ACCEPTABLE** if documented

### 5. Produce Report

```
═══════════════════════════════════════════════
  APEX V4 — FORMULA VERIFICATION REPORT
  Date: <date>
═══════════════════════════════════════════════

7.1 Kelly Criterion
──────────────────────────────────────────────
Location: <file>:<line range>
Status: [PASS | FAIL]
Spec formula: <formula from Section 7>
Implementation: <formula as coded>
Deviations: <none | list each>
Constants: <all match | list mismatches>
Boundaries: <correct | list issues>

7.2 OU Process MLE
──────────────────────────────────────────────
Location: <file>:<line range>
Status: [PASS | FAIL]
...

7.3 Conviction Score
──────────────────────────────────────────────
...

7.4 EWMA Covariance
──────────────────────────────────────────────
...

7.5 Portfolio VaR (99%)
──────────────────────────────────────────────
...

SUMMARY
──────────────────────────────────────────────
Formulas verified: <N>/5
Silent deviations: <N>
Documented deviations: <N>
OVERALL: [VERIFIED ✓ | DEVIATIONS FOUND ✗]

REQUIRED FIXES (if any):
1. <file>:<line> — <current> → <correct> — <why>
2. ...
```

## Pass Criteria

- **100% match** on all implemented formulas
- Zero silent deviations
- Zero undocumented approximations
- Every constant, operator, and boundary matches Section 7 exactly
- If a formula is not yet implemented, report as `NOT YET IMPLEMENTED` (not FAIL)
