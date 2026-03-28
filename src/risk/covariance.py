"""EWMACovarianceMatrix — EWMA covariance with eigenvalue shrinkage.

Phase 3 (P3.3).
Exact Section 7.4 / 7.5 formulas — no deviation:

  Σ_t = λ × Σ_{t-1} + (1-λ) × (r_t × r_t^T)
  λ   = 0.999  — updated on H1 candle close only, never on tick

  Eigenvalue shrinkage when κ > κ_warn (15.0):
    floor       = max(eigenvalue) / κ_warn
    eigenvalues = where(eigenvalues < floor, floor, eigenvalues)
    Σ_reg       = U @ diag(clipped) @ U^T

  Decay multiplier Φ(κ):
    κ ≤ 15.0                → 1.0
    15.0 < κ < 30.0         → exp(-γ × (κ - 15.0)),  γ = 0.5
    κ ≥ 30.0                → 0.0

  Portfolio VaR (Section 7.5):
    σ²_portfolio = W^T × Σ_reg × W
    VaR_99       = 2.326 × sqrt(σ²_portfolio) × portfolio_value

Architecture ref: APEX_V4_STRATEGY.md Section 7.4, 7.5
"""

from __future__ import annotations

from math import exp, sqrt

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

# ── constants (locked to Section 7.4) ─────────────────────────────────

_LAMBDA = 0.999
_KAPPA_WARN = 15.0
_KAPPA_MAX = 30.0
_GAMMA = 0.5
_EIGENVALUE_FLOOR_GUARD = 1e-8
_VAR_Z = 2.326  # 99th percentile z-score


class EWMACovarianceMatrix:
    """EWMA covariance matrix with eigenvalue shrinkage.

    Parameters
    ----------
    pairs
        Ordered list of currency pairs tracked (e.g. ["EURUSD", "GBPUSD", ...]).
    lambda_
        EWMA decay factor (default 0.999 per Section 7.4).
    kappa_warn
        Condition number threshold for eigenvalue shrinkage (default 15.0).
    kappa_max
        Condition number at which decay multiplier hits 0.0 (default 30.0).
    gamma
        Decay rate for Φ(κ) exponential region (default 0.5).
    """

    def __init__(
        self,
        pairs: list[str],
        lambda_: float = _LAMBDA,
        kappa_warn: float = _KAPPA_WARN,
        kappa_max: float = _KAPPA_MAX,
        gamma: float = _GAMMA,
    ) -> None:
        self._pairs = list(pairs)
        self._n = len(pairs)
        self._pair_index = {p: i for i, p in enumerate(pairs)}
        self._lambda = lambda_
        self._kappa_warn = kappa_warn
        self._kappa_max = kappa_max
        self._gamma = gamma

        # Initialize Σ as identity × small variance (no information yet).
        self._sigma: np.ndarray = np.eye(self._n, dtype=np.float64) * 1e-6
        self._update_count: int = 0

    # ── properties ─────────────────────────────────────────────────

    @property
    def pairs(self) -> list[str]:
        return list(self._pairs)

    @property
    def update_count(self) -> int:
        return self._update_count

    @property
    def raw_matrix(self) -> np.ndarray:
        """Current raw (un-regularized) covariance matrix."""
        return self._sigma.copy()

    # ── EWMA update (Section 7.4) ─────────────────────────────────

    def update(self, returns: dict[str, float]) -> None:
        """Update covariance with a new H1 return vector.

        Parameters
        ----------
        returns
            Dict mapping pair name → H1 log return.
            Missing pairs are treated as 0.0 return.
        """
        r = np.zeros(self._n, dtype=np.float64)
        for pair, ret in returns.items():
            idx = self._pair_index.get(pair)
            if idx is not None:
                r[idx] = ret

        # Σ_t = λ × Σ_{t-1} + (1-λ) × (r_t × r_t^T)
        outer = np.outer(r, r)
        self._sigma = self._lambda * self._sigma + (1.0 - self._lambda) * outer
        self._update_count += 1

    # ── eigenvalue shrinkage (Section 7.4) ─────────────────────────

    def regularize(self) -> np.ndarray:
        """Return Σ_reg after eigenvalue shrinkage if κ > κ_warn.

        Returns
        -------
        np.ndarray
            Regularized covariance matrix (n × n).
        """
        eigenvalues, eigenvectors = np.linalg.eigh(self._sigma)

        # κ = max_eigenvalue / max(min_eigenvalue, 1e-8)
        max_eig = eigenvalues[-1]  # eigh returns sorted ascending
        min_eig = eigenvalues[0]
        kappa = max_eig / max(min_eig, _EIGENVALUE_FLOOR_GUARD)

        if kappa > self._kappa_warn:
            # floor = max(eigenvalues) / κ_warn
            floor = max_eig / self._kappa_warn
            # clip all eigenvalues below floor to floor
            eigenvalues = np.where(eigenvalues < floor, floor, eigenvalues)
            # Σ_reg = U @ diag(clipped) @ U^T
            sigma_reg = eigenvectors @ np.diag(eigenvalues) @ eigenvectors.T

            logger.info(
                "eigenvalue_shrinkage_applied",
                kappa=round(float(kappa), 2),
                floor=float(floor),
            )
            return sigma_reg

        return self._sigma.copy()

    # ── condition number ───────────────────────────────────────────

    def condition_number(self) -> float:
        """Compute κ = max_eigenvalue / max(min_eigenvalue, 1e-8)."""
        eigenvalues = np.linalg.eigvalsh(self._sigma)
        return float(eigenvalues[-1] / max(eigenvalues[0], _EIGENVALUE_FLOOR_GUARD))

    # ── decay multiplier Φ(κ) (Section 7.4) ───────────────────────

    def decay_multiplier(self) -> float:
        """Compute Φ(κ) — the correlation-health decay multiplier.

        Returns
        -------
        float
            1.0 when matrix is well-conditioned, decays to 0.0 at κ_max.
        """
        kappa = self.condition_number()
        return self._phi(kappa)

    def _phi(self, kappa: float) -> float:
        """Φ(κ) per Section 7.4."""
        if kappa <= self._kappa_warn:
            return 1.0
        if kappa >= self._kappa_max:
            return 0.0
        return exp(-self._gamma * (kappa - self._kappa_warn))

    # ── Portfolio VaR (Section 7.5) ────────────────────────────────

    def portfolio_var(
        self,
        weights: dict[str, float],
        portfolio_value: float,
    ) -> float:
        """Compute 99% portfolio VaR.

        Parameters
        ----------
        weights
            Dict mapping pair → position weight (fraction of portfolio).
        portfolio_value
            Total portfolio value in account currency.

        Returns
        -------
        float
            VaR_99 in account currency.
            VaR_99 = 2.326 × sqrt(W^T × Σ_reg × W) × portfolio_value
        """
        w = np.zeros(self._n, dtype=np.float64)
        for pair, weight in weights.items():
            idx = self._pair_index.get(pair)
            if idx is not None:
                w[idx] = weight

        sigma_reg = self.regularize()

        # σ²_portfolio = W^T × Σ_reg × W
        var_portfolio = float(w @ sigma_reg @ w)

        # Guard against negative variance from numerical noise.
        if var_portfolio < 0:
            logger.warning(
                "negative_portfolio_variance",
                var_portfolio=var_portfolio,
            )
            var_portfolio = 0.0

        # VaR_99 = 2.326 × sqrt(σ²_portfolio) × portfolio_value
        var_99 = _VAR_Z * sqrt(var_portfolio) * portfolio_value

        logger.info(
            "portfolio_var_computed",
            var_99=round(var_99, 2),
            var_portfolio=round(var_portfolio, 10),
            portfolio_value=portfolio_value,
        )
        return var_99
