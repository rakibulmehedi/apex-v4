"""
src/risk/reconciler.py — State reconciliation service (ADR-004).

Phase 3 (P3.5): Implementation pending.
Runs on 5-second heartbeat.
Diffs Redis open_positions vs MT5 broker live positions.
On ANY mismatch → trigger HARD kill switch.
Broker state is always truth (ADR-004).
Writes reconciliation events to PostgreSQL reconciliation_log table.
"""
# TODO: Phase 3 — P3.5 implementation
