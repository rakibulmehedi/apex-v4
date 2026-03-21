# APEX Workspace

## Repositories

| Name | Path | Status |
|---|---|---|
| APEX V3 | `~/Desktop/apex_v3` | Production — live trading |
| APEX V4 | `~/Desktop/apex_v4` | Active development |

---

## V3 Rules

- Read freely for reference and pattern study
- Only permitted changes: 4 critical bug fixes from `APEX_V4_STRATEGY.md` Section 1.2
- Never refactor, never restructure, never rename
- Every V3 change gets its own isolated commit: `fix(v3): <description>`

---

## V4 Rules

- This is the build target — all new code goes here
- Architecture law: `APEX_V4_STRATEGY.md` — no deviation without updating the spec first
- Never copy V3 code wholesale — understand it, then build it correctly in V4
- Commit format: `feat:`, `fix:`, `fix(v3):`, `test:`, `refactor:`, `chore:`, `docs:`

---

## Reference Documents

| Document | Purpose |
|---|---|
| `CLAUDE.md` | Agent instructions, workflow rules, session protocol |
| `APEX_V4_STRATEGY.md` | Architecture decisions, mathematics, build sequence |
| `APEX_V4_WORKFLOW_ORCHESTRATION.md` | Goal templates for every phase |
| `tasks/todo.md` | Active task plan — managed by Claude |
| `tasks/lessons.md` | Accumulated lessons — managed by Claude |

---

## Session Start Protocol

`CLAUDE.md` is the canonical session protocol.
Claude Code loads it automatically at session start.

To begin any session manually, use this prompt:

```
Read CLAUDE.md. Follow the session start protocol exactly.
V3 is at ~/Desktop/apex_v3 — reference only.
V4 is at ~/Desktop/apex_v4 — build target.
Today's goal: [your goal here]
```
