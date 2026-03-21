# /hardening — Production Readiness Review

Review the entire APEX V4 codebase for production readiness.
Check error handling, logging, secrets, thread safety, and
operational concerns. Produce a prioritized fix list.

## Protocol

### 1. Context Load
1. Read `APEX_V4_STRATEGY.md` — understand the full production architecture
2. Read `tasks/lessons.md` — incorporate known issues
3. Run `git log --oneline -20` — understand what's been built

### 2. Critical Safety Systems

**Kill Switch (3 levels):**
- [ ] Level 1 (pair): disables trading for a single pair
- [ ] Level 2 (strategy): disables an entire strategy
- [ ] Level 3 (global): halts all trading immediately
- [ ] Uses `asyncio.Lock` (not threading.Lock) for async safety
- [ ] State survives process restart (persisted to Redis/disk)
- [ ] Kill switch cannot be accidentally re-enabled by normal operation

**State Reconciliation:**
- [ ] 5-second heartbeat fires reliably
- [ ] Compares Redis state against MT5 broker state
- [ ] HARD stop on state drift (not soft warning — full halt)
- [ ] Broker is always truth (ADR: MT5 wins in any conflict)

**Conviction Fallback:**
- [ ] Returns `0.0` on any failure (not `1.0` — that would trade at full size)
- [ ] Logged when fallback is triggered

**Drawdown Scalar:**
- [ ] Returns `0.0` when drawdown ≥ 5% (no trades allowed)
- [ ] Scalar is linear between 0% and 5% drawdown
- [ ] Edge case: exactly 5% returns 0.0

**Minimum Sample Gate:**
- [ ] 30-trade minimum enforced before Kelly calibration is trusted
- [ ] Below 30 trades: use conservative fixed sizing

### 3. Secrets & Configuration

**Scan for hardcoded secrets:**
4. Grep all of `src/` for patterns:
   - Hardcoded passwords, tokens, API keys
   - Connection strings with credentials embedded
   - Any string that looks like `password=`, `token=`, `secret=`, `key=`
5. Verify all secrets load from environment variables
6. Verify `config/secrets.env` is in `.gitignore`
7. Check that no secrets appear in test files (use fake values)

### 4. Logging & Observability

8. Verify structlog is used in every `src/` module — no bare `print()`
9. Check log levels are appropriate:
   - ERROR: unrecoverable failures, kill switch triggers
   - WARNING: degraded state, fallback activated, retry
   - INFO: normal lifecycle events (start, stop, trade executed)
   - DEBUG: detailed computation values (disabled in production)
10. Verify structured fields include: `component`, `pair`, `action` where relevant
11. Check that no sensitive data is logged (account credentials, full API keys)

### 5. Type Safety & Code Quality

12. Verify all public functions have full type annotations
13. Check for `Any` type usage — should be minimal and justified
14. Verify frozen dataclasses are used for immutable data (MarketSnapshot, etc.)
15. Check for mutable default arguments in function signatures

### 6. Concurrency & Thread Safety

16. Identify all concurrent components:
    - Async event loop (main trading loop)
    - Redis pub/sub listeners
    - MT5 polling threads
    - Health check endpoints
17. Verify shared state is protected:
    - `asyncio.Lock` for async contexts
    - No raw threading primitives mixed with asyncio
    - No shared mutable state without synchronization
18. Check for common async pitfalls:
    - Unawaited coroutines
    - Blocking calls in async context (use `run_in_executor`)
    - Missing `async with` for async context managers

### 7. Error Handling

19. Check execution path for uncaught exceptions:
    - MT5 connection failures
    - Redis connection failures
    - Database connection failures
    - Network timeouts
    - Malformed market data
20. Verify retry logic has:
    - Maximum retry count (not infinite)
    - Exponential backoff
    - Circuit breaker for persistent failures
21. Verify fill tracking records only after `TRADE_RETCODE_DONE`
22. Check that partial fills are handled correctly

### 8. Operational Readiness

23. Verify health check endpoint exists and reports meaningful status
24. Check graceful shutdown:
    - Pending orders are handled (cancelled or confirmed)
    - State is flushed to persistent storage
    - Connections are closed cleanly
25. Verify configuration can be changed without code deployment
26. Check that all timeouts have sensible defaults

### 9. Produce Report

```
═══════════════════════════════════════════════
  APEX V4 — PRODUCTION READINESS REPORT
  Date: <date>    Phase: <current phase>
═══════════════════════════════════════════════

CRITICAL SAFETY SYSTEMS
──────────────────────────────────────────────
Kill switch (3 levels):     [PASS|WARN|FAIL]
State reconciliation:       [PASS|WARN|FAIL]
Conviction fallback:        [PASS|WARN|FAIL]
Drawdown scalar:            [PASS|WARN|FAIL]
Minimum sample gate:        [PASS|WARN|FAIL]

SECURITY
──────────────────────────────────────────────
Hardcoded secrets:          [PASS|FAIL] — <count found>
Secrets from env vars:      [PASS|FAIL]
Secrets in .gitignore:      [PASS|FAIL]
Sensitive data in logs:     [PASS|FAIL]

CODE QUALITY
──────────────────────────────────────────────
structlog everywhere:       [PASS|FAIL] — <violations>
Type hints complete:        [PASS|FAIL] — <violations>
No mutable defaults:        [PASS|FAIL] — <violations>
Frozen dataclasses:         [PASS|FAIL] — <violations>

CONCURRENCY
──────────────────────────────────────────────
Async safety:               [PASS|WARN|FAIL]
No blocking in async:       [PASS|WARN|FAIL]
Shared state protected:     [PASS|WARN|FAIL]

ERROR HANDLING
──────────────────────────────────────────────
Uncaught exceptions:        [PASS|FAIL] — <count>
Retry with backoff:         [PASS|FAIL]
Fill tracking correct:      [PASS|FAIL]
Graceful shutdown:          [PASS|FAIL]

OVERALL: PRODUCTION READY ✓ | NOT READY ✗

PRIORITIZED FIX LIST
──────────────────────────────────────────────
P0 (BLOCKING — must fix before go-live):
1. <file>:<line> — <issue> — <fix>

P1 (HIGH — fix within 24h of go-live):
1. <file>:<line> — <issue> — <fix>

P2 (MEDIUM — fix within first week):
1. <file>:<line> — <issue> — <fix>

P3 (LOW — track for future):
1. <file>:<line> — <issue> — <fix>
```

## Rules

- Any P0 item means OVERALL = NOT READY
- Kill switch failure is always P0
- Hardcoded secrets is always P0
- Be thorough — this is the last check before real money trades
