# APEX V4 — Deferred Work

## Phase 5 follow-ups

### Grafana dashboard JSON for APEX metrics
- **Why:** Metrics without dashboards are invisible. Need at-a-glance view of signal rates, gate rejections, VaR/drawdown gauges, slippage distributions.
- **Pros:** One-time setup, makes observability layer immediately useful.
- **Cons:** Requires Grafana infrastructure (Docker compose or cloud).
- **Context:** Natural follow-up after `src/observability/metrics.py` ships. All 14 Prometheus metrics need corresponding panels. Consider provisioning dashboard as code (JSON model in `infra/grafana/`).
- **Depends on:** Phase 5 metrics implementation.
- **Added:** 2026-03-25 via /plan-eng-review

### Alertmanager rules for critical APEX thresholds
- **Why:** Metrics without alerts means manual dashboard watching. Automated notification on risk limit breaches is essential for a production trading system.
- **Suggested rules:** `kill_switch_total` increase in 5min, `portfolio_var_pct > 0.04`, `current_drawdown_pct > 0.06`, `state_drift_total` increase.
- **Pros:** Closes the observability loop: metrics → dashboards → alerts.
- **Cons:** Needs Alertmanager infra + notification channel (Slack/email/PagerDuty).
- **Context:** Critical for production safety. A trading system that silently breaches risk limits is worse than one with no metrics at all.
- **Depends on:** Grafana dashboard + Phase 5 metrics implementation.
- **Added:** 2026-03-25 via /plan-eng-review
