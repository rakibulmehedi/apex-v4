"""
src/risk/covariance.py — EWMA covariance matrix + eigenvalue shrinkage.

Phase 3 (P3.3): Implementation pending.
Formula locked in APEX_V4_STRATEGY.md Section 7.4:
  Σ_t = λ × Σ_{t-1} + (1-λ) × (r_t × r_t^T)
  λ = 0.999 — updated at H1 candle close, NOT tick frequency

Eigenvalue shrinkage when κ > 15.0.
Correlation scalar Φ(κ) → 0.0 when κ ≥ 30.0.
"""
# TODO: Phase 3 — P3.3 implementation
