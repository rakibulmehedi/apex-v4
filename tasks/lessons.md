# APEX V4 — Accumulated Lessons

_This file is updated after every correction. Read at the start of every session._

## Rules

### L1: Production target is Windows VPS, not Linux
APEX V4 deploys to a **Windows VPS** where the MT5 terminal runs natively.
Do NOT generate Linux-specific ops artifacts (systemd, bash scripts).
Use Windows equivalents: NSSM for services, PowerShell for scripts,
Windows paths (`C:\apex_v4`). The MT5 terminal requires Windows — this
is a hard constraint, not a preference.
