# /hardening — Production Readiness Review

Pre-live hardening audit for APEX V4.

## Protocol

1. **Kill switch** — verify 3 levels, asyncio.Lock, survives process restart
2. **State reconciliation** — verify 5s heartbeat fires, HARD stop on drift
3. **Secrets** — grep src/ for hardcoded passwords, tokens, or API keys
4. **Logging** — verify structlog everywhere, no bare print() in src/
5. **Type hints** — verify all public functions have full type annotations
6. **Error handling** — check execution path for uncaught exceptions
7. **Conviction fallback** — verify returns 0.0 (not 1.0) on failure
8. **Fill tracking** — verify records only after TRADE_RETCODE_DONE
9. **Drawdown scalar** — verify 0.0 when dd ≥ 5% (no trades)
10. **Minimum sample gate** — verify 30-trade check enforced

## Output Format

Numbered list of PASS / WARN / FAIL for each item.
OVERALL: PRODUCTION READY | NOT READY (with blocking items).
