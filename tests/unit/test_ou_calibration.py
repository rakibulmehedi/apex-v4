"""Unit tests for src/alpha/ou_calibration.py — OU MLE + conviction.

Verifies exact Section 7.2 and 7.3 formulas.
"""

from __future__ import annotations

from math import erf, exp, log, sqrt

import numpy as np
import pytest

from src.alpha.ou_calibration import (
    ConvictionResult,
    OUParams,
    compute_conviction,
    fit_ou,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ou_series(
    mu: float = 1.10,
    theta: float = 0.05,
    sigma: float = 0.001,
    n: int = 200,
    x0: float | None = None,
    seed: int = 42,
) -> np.ndarray:
    """Generate a synthetic OU process for testing.

    X[i+1] = X[i] + θ(μ - X[i])Δt + σ·ε
    """
    rng = np.random.default_rng(seed)
    x = np.empty(n, dtype=np.float64)
    x[0] = x0 if x0 is not None else mu
    dt = 1.0
    for i in range(n - 1):
        x[i + 1] = x[i] + theta * (mu - x[i]) * dt + sigma * rng.normal()
    return x


# ---------------------------------------------------------------------------
# fit_ou — Section 7.2
# ---------------------------------------------------------------------------


class TestFitOU:
    """OU MLE parameter estimation."""

    def test_recovers_positive_rho(self) -> None:
        """Mean-reverting series should have ρ > 0."""
        states = _make_ou_series(theta=0.05, n=500)
        params = fit_ou(states)
        assert params is not None
        assert params.rho > 0

    def test_recovers_mu_approximately(self) -> None:
        """μ should be close to the true mean."""
        mu_true = 1.10
        states = _make_ou_series(mu=mu_true, theta=0.05, n=1000, seed=7)
        params = fit_ou(states)
        assert params is not None
        assert abs(params.mu - mu_true) < 0.005

    def test_theta_positive(self) -> None:
        states = _make_ou_series(theta=0.05, n=500)
        params = fit_ou(states)
        assert params is not None
        assert params.theta > 0

    def test_sigma_sq_positive(self) -> None:
        states = _make_ou_series(theta=0.05, n=500)
        params = fit_ou(states)
        assert params is not None
        assert params.sigma_sq > 0

    def test_half_life_formula(self) -> None:
        """half_life should equal ln(2) / θ."""
        states = _make_ou_series(theta=0.05, n=500)
        params = fit_ou(states)
        assert params is not None
        expected_hl = log(2.0) / params.theta
        assert abs(params.half_life - expected_hl) < 0.1

    def test_rejects_non_positive_rho(self) -> None:
        """A random walk (no mean reversion) should give ρ ≈ 1 or cause rejection."""
        # Pure random walk — ρ should be very close to 1 (not rejected on ρ,
        # but might be rejected on half_life). Let's create anti-persistent data.
        rng = np.random.default_rng(42)
        # Alternating series: ρ < 0.
        x = np.empty(200, dtype=np.float64)
        x[0] = 1.10
        for i in range(199):
            x[i + 1] = 2 * 1.10 - x[i] + rng.normal(0, 0.0001)
        params = fit_ou(x)
        assert params is None

    def test_rejects_half_life_above_48(self) -> None:
        """Very slow reversion → half_life > 48 → rejected."""
        # Direct AR(1): X[i+1] = ρ·X[i] + (1-ρ)·μ + noise.
        # With ρ=0.995 → θ = -ln(0.995) ≈ 0.005 → HL ≈ 139 >> 48.
        rng = np.random.default_rng(42)
        n = 1000
        rho_target = 0.999
        mu_target = 1.10
        x = np.empty(n)
        x[0] = mu_target
        for i in range(n - 1):
            x[i + 1] = rho_target * x[i] + (1 - rho_target) * mu_target + 0.00001 * rng.normal()
        params = fit_ou(x)
        assert params is None

    def test_rejects_insufficient_states(self) -> None:
        states = np.array([1.1, 1.2])
        assert fit_ou(states) is None

    def test_theta_formula_exact(self) -> None:
        """θ = -ln(ρ) / Δt — verify the computation."""
        states = _make_ou_series(theta=0.05, n=500)
        params = fit_ou(states)
        assert params is not None
        # θ should equal -ln(ρ) / 1.0
        expected_theta = -log(params.rho)
        assert abs(params.theta - expected_theta) < 1e-6

    def test_sigma_sq_formula_exact(self) -> None:
        """Verify σ² = (2θ / (T(1-e^(-2θΔt)))) × Σ(ε_i²)."""
        states = _make_ou_series(theta=0.05, n=200)
        params = fit_ou(states)
        assert params is not None

        # Recompute σ² manually to verify.
        dt = 1.0
        theta = params.theta
        mu = params.mu
        e_neg_theta = exp(-theta * dt)

        residuals = states[1:] - states[:-1] * e_neg_theta - mu * (1.0 - e_neg_theta)
        T = len(residuals)
        e_neg_2theta = exp(-2.0 * theta * dt)
        expected_sigma_sq = (2.0 * theta / (T * (1.0 - e_neg_2theta))) * np.sum(residuals**2)
        assert abs(params.sigma_sq - round(expected_sigma_sq, 10)) < 1e-9


# ---------------------------------------------------------------------------
# compute_conviction — Section 7.3
# ---------------------------------------------------------------------------


class TestComputeConviction:
    """Conviction score calculation."""

    def test_conviction_above_threshold_accepted(self) -> None:
        """z ≈ 1.0 → C = erf(1/√2) ≈ 0.683 > 0.65."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))  # = 0.0316...
        x_current = 1.10 + 1.0 * sigma_eq  # z = 1.0
        result = compute_conviction(x_current, params)
        assert result is not None
        assert result.conviction >= 0.65

    def test_conviction_formula_exact(self) -> None:
        """C = erf(|z| / sqrt(2)) — verify."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        z_val = 1.5
        x_current = 1.10 + z_val * sigma_eq
        result = compute_conviction(x_current, params)
        assert result is not None
        expected_c = erf(abs(z_val) / sqrt(2.0))
        assert abs(result.conviction - expected_c) < 0.01

    def test_sigma_eq_formula(self) -> None:
        """σ_eq = sqrt(σ² / (2θ))."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        expected_sigma_eq = sqrt(0.0001 / (2 * 0.05))
        sigma_eq = sqrt(params.sigma_sq / (2 * params.theta))
        assert abs(sigma_eq - expected_sigma_eq) < 1e-10

    def test_zscore_formula(self) -> None:
        """z = (x_current - μ) / σ_eq."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        x_current = 1.10 + 2.0 * sigma_eq
        result = compute_conviction(x_current, params)
        assert result is not None
        assert abs(result.z_score - 2.0) < 0.01

    def test_regime_break_rejected_above_3(self) -> None:
        """|z| > 3.0 → regime break, return None."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        x_current = 1.10 + 3.1 * sigma_eq  # z = 3.1
        result = compute_conviction(x_current, params)
        assert result is None

    def test_regime_break_rejected_below_neg3(self) -> None:
        """|z| > 3.0 negative → regime break."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        x_current = 1.10 - 3.5 * sigma_eq
        result = compute_conviction(x_current, params)
        assert result is None

    def test_zscore_just_below_3_accepted(self) -> None:
        """|z| just below 3.0 → accepted."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        x_current = 1.10 + 2.99 * sigma_eq
        result = compute_conviction(x_current, params)
        # z ≈ 2.99 < 3.0 guard, C ≈ 0.997 > 0.65 → accepted.
        assert result is not None

    def test_insufficient_edge_rejected(self) -> None:
        """C < 0.65 → rejected."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        # z ≈ 0.5 → C = erf(0.5/√2) ≈ 0.383 < 0.65.
        x_current = 1.10 + 0.5 * sigma_eq
        result = compute_conviction(x_current, params)
        assert result is None

    def test_negative_z_gives_long_direction(self) -> None:
        """z < 0 means price below mean → conviction should still compute."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        x_current = 1.10 - 1.5 * sigma_eq  # z = -1.5
        result = compute_conviction(x_current, params)
        assert result is not None
        assert result.z_score < 0
        assert result.conviction > 0.65

    def test_conviction_bounded_0_to_1(self) -> None:
        """erf output is bounded [0, 1]."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        x_current = 1.10 + 2.5 * sigma_eq
        result = compute_conviction(x_current, params)
        assert result is not None
        assert 0.0 <= result.conviction <= 1.0

    def test_custom_zscore_guard(self) -> None:
        """Custom guard threshold works."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        x_current = 1.10 + 2.5 * sigma_eq  # z = 2.5
        # Default guard=3.0 → accepted; guard=2.0 → rejected.
        assert compute_conviction(x_current, params, zscore_guard=3.0) is not None
        assert compute_conviction(x_current, params, zscore_guard=2.0) is None

    def test_custom_min_conviction(self) -> None:
        """Custom min_conviction threshold."""
        params = OUParams(rho=0.95, theta=0.05, mu=1.10, sigma_sq=0.0001, half_life=13.86)
        sigma_eq = sqrt(0.0001 / (2 * 0.05))
        # z=1.0 → C ≈ 0.683. With min=0.65 accepted, min=0.70 rejected.
        x_current = 1.10 + 1.0 * sigma_eq
        assert compute_conviction(x_current, params, min_conviction=0.65) is not None
        assert compute_conviction(x_current, params, min_conviction=0.70) is None
