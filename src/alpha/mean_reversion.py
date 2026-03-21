"""
src/alpha/mean_reversion.py — Mean reversion engine.

Phase 2 (P2.3–P2.7): Implementation pending.
Pipeline: ADF gate → filterpy Kalman → OU MLE → erf conviction → 3σ guard
Activated when: RANGING regime.
Output: AlphaHypothesis with strategy=MEAN_REVERSION, or None.

Key thresholds (Section 7):
  ADF p-value < 0.05 (stationarity gate)
  OU half-life ≤ 48 H1 candles
  Conviction ≥ 0.65
  |z-score| < 3.0 (3σ guard — no position on regime break)
"""
# TODO: Phase 2 — P2.3–P2.7 implementation
