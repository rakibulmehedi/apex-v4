# /audit — Full Architecture Compliance Check

Perform a full compliance audit of APEX V4 against `APEX_V4_STRATEGY.md`.

## Protocol

1. Read `APEX_V4_STRATEGY.md` in full
2. For each ADR (ADR-001 through ADR-009), check implementation status:
   - IMPLEMENTED — verify correctness
   - PENDING — expected for current phase
   - VIOLATED — flag immediately
3. Check all Section 7 formulas in implemented modules via `/risk-verify`
4. Check code standards compliance (type hints, structlog, no secrets)
5. Check `.gitignore` covers `config/secrets.env` and `venv/`
6. Produce audit report: PASS / WARN / FAIL per ADR

## Output Format

```
ADR-001: [PASS|WARN|FAIL] — note
ADR-002: [PASS|WARN|FAIL] — note
...
OVERALL: [COMPLIANT | ISSUES FOUND]
```
