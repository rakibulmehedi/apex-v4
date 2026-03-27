# APEX V4 — Accumulated Lessons

_This file is updated after every correction. Read at the start of every session._

## Rules

### L1: Production target is Windows VPS, not Linux
APEX V4 deploys to a **Windows VPS** where the MT5 terminal runs natively.
Do NOT generate Linux-specific ops artifacts (systemd, bash scripts).
Use Windows equivalents: NSSM for services, PowerShell for scripts,
Windows paths (`C:\apex_v4`). The MT5 terminal requires Windows — this
is a hard constraint, not a preference.

### L2: init_context() must create default dependencies
`init_context()` accepts optional `session_factory` and `redis_client` for
DI in tests, but when called from `_async_main()` without overrides, these
default to `None` — which cascades to every component (KillSwitch,
StateReconciler, etc.), causing `TypeError: 'NoneType' object is not callable`
and `AttributeError: 'NoneType' has no attribute 'get'`.

**Rule:** Any function that accepts optional DI parameters and is called from
production code paths must create sensible defaults when `None` is passed.
Check this pattern whenever adding new DI-style parameters.

### L3: Mock return values must match the real function's type
When patching `run_preflight` (returns `float`) the mock must set
`return_value=0.10`, not leave it as `MagicMock`. Otherwise f-string format
specs like `:.0f` crash with `TypeError: unsupported format string`.

**Rule:** Every `patch()` of a function whose return value is consumed
downstream must set `return_value` to a type-correct value.

### L4: Windows asyncio requires SelectorEventLoopPolicy for pyzmq
Python 3.10+ on Windows defaults to `ProactorEventLoop`, which does not
implement `add_reader()`/`remove_reader()` required by pyzmq async sockets.
This causes `RuntimeError: Proactor event loop does not implement add_reader`.

**Rule:** Any entry point that uses `asyncio.run()` with pyzmq async sockets
must set `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`
on Windows (`sys.platform == "win32"`) before calling `asyncio.run()`.
