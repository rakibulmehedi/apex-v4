# /risk-verify — Mathematical Formula Verification

Verify all risk/calibration formulas against APEX_V4_STRATEGY.md Section 7.

## Protocol

1. Read `APEX_V4_STRATEGY.md` Section 7 (Mathematics Reference) in full
2. For each formula (7.1–7.5), find the implementation:
   - 7.1 Kelly Criterion → `src/calibration/engine.py`
   - 7.2 OU Process MLE → `src/alpha/mean_reversion.py`
   - 7.3 Conviction Score → `src/alpha/mean_reversion.py`
   - 7.4 EWMA Covariance → `src/risk/covariance.py`
   - 7.5 Portfolio VaR → `src/risk/governor.py`
3. Extract every constant, coefficient, and operator from the implementation
4. Compare against Section 7 — flag ANY deviation
5. Acceptable: documented justification in a code comment
6. Unacceptable: silent deviation, approximation, rounding without comment

## Pass Criteria

100% match on all formulas. No silent deviations.

## Output Format

```
7.1 Kelly:       [PASS|FAIL] — note
7.2 OU MLE:      [PASS|FAIL] — note
7.3 Conviction:  [PASS|FAIL] — note
7.4 EWMA Cov:    [PASS|FAIL] — note
7.5 VaR 99%:     [PASS|FAIL] — note
OVERALL: [VERIFIED | DEVIATIONS FOUND]
```
