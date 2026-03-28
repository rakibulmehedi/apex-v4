"""Unit tests for src/risk/covariance.py — EWMACovarianceMatrix.

Tests cover:
  - EWMA update formula (Section 7.4)
  - Eigenvalue shrinkage (κ > 15)
  - Decay multiplier Φ(κ) branches
  - Portfolio VaR (Section 7.5)
  - Edge cases: single pair, identity, zero returns
"""

from __future__ import annotations

from math import exp, sqrt

import numpy as np
import pytest

from src.risk.covariance import EWMACovarianceMatrix


PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"]


# ── EWMA update ──────────────────────────────────────────────────────────


class TestEWMAUpdate:
    """Verify Σ_t = λ × Σ_{t-1} + (1-λ) × (r_t × r_t^T)."""

    def test_first_update(self):
        """After one update, Σ = λ×I×1e-6 + (1-λ)×(r×r^T)."""
        cov = EWMACovarianceMatrix(PAIRS)
        r = {"EURUSD": 0.001, "GBPUSD": 0.002, "USDJPY": -0.001, "AUDUSD": 0.0005}
        cov.update(r)

        rv = np.array([0.001, 0.002, -0.001, 0.0005])
        expected = 0.999 * np.eye(4) * 1e-6 + 0.001 * np.outer(rv, rv)

        np.testing.assert_allclose(cov.raw_matrix, expected, atol=1e-15)
        assert cov.update_count == 1

    def test_two_updates(self):
        """Two sequential updates compose correctly."""
        cov = EWMACovarianceMatrix(PAIRS)
        r1 = {"EURUSD": 0.001, "GBPUSD": 0.002, "USDJPY": 0.0, "AUDUSD": 0.0}
        r2 = {"EURUSD": -0.001, "GBPUSD": 0.001, "USDJPY": 0.003, "AUDUSD": 0.0}

        cov.update(r1)
        sigma_1 = cov.raw_matrix.copy()

        cov.update(r2)
        rv2 = np.array([-0.001, 0.001, 0.003, 0.0])
        expected = 0.999 * sigma_1 + 0.001 * np.outer(rv2, rv2)

        np.testing.assert_allclose(cov.raw_matrix, expected, atol=1e-15)
        assert cov.update_count == 2

    def test_missing_pair_treated_as_zero(self):
        """Pairs not in returns dict get 0.0 return."""
        cov = EWMACovarianceMatrix(PAIRS)
        cov.update({"EURUSD": 0.005})  # only 1 of 4

        rv = np.array([0.005, 0.0, 0.0, 0.0])
        expected = 0.999 * np.eye(4) * 1e-6 + 0.001 * np.outer(rv, rv)
        np.testing.assert_allclose(cov.raw_matrix, expected, atol=1e-15)

    def test_unknown_pair_ignored(self):
        """Pairs not in the matrix are silently ignored."""
        cov = EWMACovarianceMatrix(PAIRS)
        cov.update({"NZDCAD": 0.01, "EURUSD": 0.001})

        rv = np.array([0.001, 0.0, 0.0, 0.0])
        expected = 0.999 * np.eye(4) * 1e-6 + 0.001 * np.outer(rv, rv)
        np.testing.assert_allclose(cov.raw_matrix, expected, atol=1e-15)

    def test_zero_returns(self):
        """All-zero returns shrink Σ toward zero."""
        cov = EWMACovarianceMatrix(PAIRS)
        cov.update({"EURUSD": 0.0, "GBPUSD": 0.0, "USDJPY": 0.0, "AUDUSD": 0.0})

        # Σ = 0.999 × I×1e-6 + 0.001 × 0 = 0.999 × I × 1e-6
        expected = 0.999 * np.eye(4) * 1e-6
        np.testing.assert_allclose(cov.raw_matrix, expected, atol=1e-15)

    def test_lambda_value(self):
        """Confirm λ = 0.999 by default."""
        cov = EWMACovarianceMatrix(PAIRS)
        assert cov._lambda == 0.999

    def test_symmetry_preserved(self):
        """Covariance matrix stays symmetric after updates."""
        cov = EWMACovarianceMatrix(PAIRS)
        rng = np.random.default_rng(42)
        for _ in range(50):
            r = {p: float(rng.normal(0, 0.001)) for p in PAIRS}
            cov.update(r)

        sigma = cov.raw_matrix
        np.testing.assert_allclose(sigma, sigma.T, atol=1e-15)


# ── eigenvalue shrinkage ─────────────────────────────────────────────────


class TestEigenvalueShrinkage:
    """Verify shrinkage when κ > κ_warn (15.0)."""

    def test_well_conditioned_no_shrinkage(self):
        """Identity-like matrix → no shrinkage applied."""
        cov = EWMACovarianceMatrix(PAIRS)
        # Initial state is I×1e-6, κ = 1.0
        sigma_reg = cov.regularize()
        np.testing.assert_allclose(sigma_reg, cov.raw_matrix, atol=1e-15)

    def test_ill_conditioned_shrinkage(self):
        """Force high κ, verify floor is applied."""
        cov = EWMACovarianceMatrix(["A", "B"])
        # Manually set Σ with extreme condition number.
        # eigenvalues: 1.0 and 1e-4 → κ = 10000.
        cov._sigma = np.array([[1.0, 0.0], [0.0, 1e-4]])

        sigma_reg = cov.regularize()
        eigs = np.linalg.eigvalsh(sigma_reg)

        # floor = max_eig / 15.0 = 1.0 / 15.0 ≈ 0.0667
        floor = 1.0 / 15.0
        assert eigs[0] >= floor - 1e-10  # min eigenvalue clipped to floor
        # Condition number of regularized matrix should be ≤ 15.0
        kappa_reg = eigs[-1] / max(eigs[0], 1e-8)
        assert kappa_reg <= 15.0 + 1e-6

    def test_shrinkage_preserves_eigenvectors(self):
        """Shrinkage only changes eigenvalues, not eigenvectors."""
        cov = EWMACovarianceMatrix(["A", "B"])
        # Diagonal matrix — eigenvectors are standard basis.
        cov._sigma = np.array([[1.0, 0.0], [0.0, 1e-6]])

        sigma_reg = cov.regularize()
        # Should still be diagonal (eigenvectors preserved).
        assert abs(sigma_reg[0, 1]) < 1e-10
        assert abs(sigma_reg[1, 0]) < 1e-10

    def test_shrinkage_floor_formula(self):
        """floor = max(eigenvalues) / κ_warn."""
        cov = EWMACovarianceMatrix(["A", "B", "C"])
        # eigenvalues: 0.09, 0.01, 0.0001
        cov._sigma = np.diag([0.09, 0.01, 0.0001])

        sigma_reg = cov.regularize()
        eigs = sorted(np.linalg.eigvalsh(sigma_reg))

        floor = 0.09 / 15.0  # = 0.006
        # All eigenvalues should be >= floor
        for e in eigs:
            assert e >= floor - 1e-10

    def test_kappa_at_boundary_15(self):
        """κ exactly 15.0 → no shrinkage (only > 15 triggers it)."""
        cov = EWMACovarianceMatrix(["A", "B"])
        # eigenvalues: 15.0 and 1.0 → κ = 15.0
        cov._sigma = np.diag([15.0, 1.0])

        sigma_reg = cov.regularize()
        np.testing.assert_allclose(sigma_reg, cov._sigma, atol=1e-10)


# ── condition number ─────────────────────────────────────────────────────


class TestConditionNumber:
    """κ = max_eigenvalue / max(min_eigenvalue, 1e-8)."""

    def test_identity_kappa_1(self):
        """Identity matrix → κ = 1.0."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.eye(2)
        assert cov.condition_number() == pytest.approx(1.0)

    def test_diagonal_kappa(self):
        """Diagonal [4, 2] → κ = 2.0."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.diag([4.0, 2.0])
        assert cov.condition_number() == pytest.approx(2.0)

    def test_near_singular_kappa(self):
        """Very small min eigenvalue uses 1e-8 floor."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.diag([1.0, 0.0])
        # κ = 1.0 / max(0.0, 1e-8) = 1.0 / 1e-8 = 1e8
        assert cov.condition_number() == pytest.approx(1e8)


# ── decay multiplier Φ(κ) ────────────────────────────────────────────────


class TestDecayMultiplier:
    """Φ(κ): 1.0 if κ≤15, exp(-0.5×(κ-15)) if 15<κ<30, 0.0 if κ≥30."""

    def test_well_conditioned(self):
        """κ = 1 → Φ = 1.0."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.eye(2)
        assert cov.decay_multiplier() == pytest.approx(1.0)

    def test_kappa_exactly_15(self):
        """κ = 15.0 → Φ = 1.0 (boundary: ≤)."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.diag([15.0, 1.0])
        assert cov.decay_multiplier() == pytest.approx(1.0)

    def test_kappa_20(self):
        """κ = 20 → Φ = exp(-0.5 × 5) = exp(-2.5)."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.diag([20.0, 1.0])
        expected = exp(-0.5 * (20.0 - 15.0))
        assert cov.decay_multiplier() == pytest.approx(expected)

    def test_kappa_25(self):
        """κ = 25 → Φ = exp(-0.5 × 10) = exp(-5.0)."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.diag([25.0, 1.0])
        expected = exp(-0.5 * (25.0 - 15.0))
        assert cov.decay_multiplier() == pytest.approx(expected)

    def test_kappa_exactly_30(self):
        """κ = 30.0 → Φ = 0.0 (boundary: ≥)."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.diag([30.0, 1.0])
        assert cov.decay_multiplier() == pytest.approx(0.0)

    def test_kappa_above_30(self):
        """κ = 100 → Φ = 0.0."""
        cov = EWMACovarianceMatrix(["A", "B"])
        cov._sigma = np.diag([100.0, 1.0])
        assert cov.decay_multiplier() == pytest.approx(0.0)

    def test_phi_direct(self):
        """Verify _phi static logic across all branches."""
        cov = EWMACovarianceMatrix(["A"])
        assert cov._phi(1.0) == 1.0
        assert cov._phi(15.0) == 1.0
        assert cov._phi(15.01) == pytest.approx(exp(-0.5 * 0.01))
        assert cov._phi(29.99) == pytest.approx(exp(-0.5 * 14.99))
        assert cov._phi(30.0) == 0.0
        assert cov._phi(999.0) == 0.0


# ── Portfolio VaR (Section 7.5) ──────────────────────────────────────────


class TestPortfolioVaR:
    """VaR_99 = 2.326 × sqrt(W^T × Σ_reg × W) × portfolio_value."""

    def test_single_pair_var(self):
        """One pair, known variance → VaR is analytically computable."""
        cov = EWMACovarianceMatrix(["EURUSD"])
        # Set σ² = 0.0001 (1% daily vol squared)
        cov._sigma = np.array([[0.0001]])

        var_99 = cov.portfolio_var({"EURUSD": 0.01}, portfolio_value=100_000)

        # σ²_p = 0.01² × 0.0001 = 1e-8
        # VaR = 2.326 × sqrt(1e-8) × 100000 = 2.326 × 0.0001 × 100000 = 23.26
        expected = 2.326 * sqrt(0.01**2 * 0.0001) * 100_000
        assert var_99 == pytest.approx(expected, rel=1e-6)

    def test_two_uncorrelated_pairs(self):
        """Diagonal Σ → VaR from independent variances."""
        cov = EWMACovarianceMatrix(["EURUSD", "GBPUSD"])
        cov._sigma = np.diag([0.0001, 0.0004])

        weights = {"EURUSD": 0.01, "GBPUSD": 0.02}
        var_99 = cov.portfolio_var(weights, portfolio_value=100_000)

        # σ²_p = 0.01²×0.0001 + 0.02²×0.0004 = 1e-8 + 1.6e-7 = 1.7e-7
        w = np.array([0.01, 0.02])
        sigma = np.diag([0.0001, 0.0004])
        var_p = w @ sigma @ w
        expected = 2.326 * sqrt(var_p) * 100_000
        assert var_99 == pytest.approx(expected, rel=1e-6)

    def test_correlated_pairs_var(self):
        """Off-diagonal covariance increases VaR."""
        cov = EWMACovarianceMatrix(["EURUSD", "GBPUSD"])
        # Positive correlation
        cov._sigma = np.array([[0.0001, 0.00005], [0.00005, 0.0001]])

        weights = {"EURUSD": 0.01, "GBPUSD": 0.01}
        var_correlated = cov.portfolio_var(weights, portfolio_value=100_000)

        # Compare with uncorrelated
        cov._sigma = np.diag([0.0001, 0.0001])
        var_uncorrelated = cov.portfolio_var(weights, portfolio_value=100_000)

        assert var_correlated > var_uncorrelated

    def test_zero_weights_zero_var(self):
        """No positions → VaR = 0."""
        cov = EWMACovarianceMatrix(PAIRS)
        cov._sigma = np.eye(4) * 0.001

        var_99 = cov.portfolio_var({}, portfolio_value=100_000)
        assert var_99 == pytest.approx(0.0)

    def test_portfolio_value_scales_linearly(self):
        """VaR scales linearly with portfolio value."""
        cov = EWMACovarianceMatrix(["EURUSD"])
        cov._sigma = np.array([[0.0001]])
        w = {"EURUSD": 0.01}

        var_100k = cov.portfolio_var(w, portfolio_value=100_000)
        var_200k = cov.portfolio_var(w, portfolio_value=200_000)

        assert var_200k == pytest.approx(2.0 * var_100k, rel=1e-6)

    def test_var_uses_regularized_matrix(self):
        """VaR computation uses Σ_reg (shrunk), not raw Σ."""
        cov = EWMACovarianceMatrix(["A", "B"])
        # Highly ill-conditioned: κ = 1e6
        cov._sigma = np.diag([1.0, 1e-6])

        var_99 = cov.portfolio_var({"A": 0.01, "B": 0.01}, portfolio_value=100_000)

        # After shrinkage: min eigenvalue raised to 1.0/15.0
        # This should produce a different (larger) VaR than raw
        w = np.array([0.01, 0.01])
        raw_var_p = float(w @ cov._sigma @ w)
        reg_var_p = float(w @ cov.regularize() @ w)
        assert reg_var_p > raw_var_p  # shrinkage raises small eigenvalue


# ── properties and construction ──────────────────────────────────────────


class TestConstruction:
    """Constructor, properties, and edge cases."""

    def test_pairs_property(self):
        cov = EWMACovarianceMatrix(PAIRS)
        assert cov.pairs == PAIRS

    def test_update_count_starts_zero(self):
        cov = EWMACovarianceMatrix(PAIRS)
        assert cov.update_count == 0

    def test_initial_matrix_is_identity_scaled(self):
        """Initial Σ = I × 1e-6."""
        cov = EWMACovarianceMatrix(PAIRS)
        expected = np.eye(4) * 1e-6
        np.testing.assert_allclose(cov.raw_matrix, expected)

    def test_single_pair(self):
        """Works with a single pair (1×1 matrix)."""
        cov = EWMACovarianceMatrix(["EURUSD"])
        cov.update({"EURUSD": 0.002})

        expected_val = 0.999 * 1e-6 + 0.001 * 0.002**2
        assert cov.raw_matrix[0, 0] == pytest.approx(expected_val)

    def test_custom_lambda(self):
        """Custom λ is used in updates."""
        cov = EWMACovarianceMatrix(["A"], lambda_=0.9)
        cov.update({"A": 0.01})

        expected_val = 0.9 * 1e-6 + 0.1 * 0.01**2
        assert cov.raw_matrix[0, 0] == pytest.approx(expected_val)
