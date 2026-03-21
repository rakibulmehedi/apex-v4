"""
src/regime/classifier.py — Hard ADX-based regime classification.

Phase 2 (P2.1): Implementation pending.

Rules (ADR-001):
  TRENDING_UP   → ADX > 25 AND close > EMA200 → Momentum engine
  TRENDING_DOWN → ADX > 25 AND close < EMA200 → Momentum engine
  RANGING       → ADX < 20                    → Mean Reversion engine
  UNDEFINED     → ADX 20-25, news, spread     → NO TRADE

No ML. No probabilities.
"""
# TODO: Phase 2 — P2.1 implementation
