"""
src/alpha/ou_calibration.py — OU MLE parameter estimation and conviction score.

Phase 2 (P2.5, P2.6): Exact formulas from APEX_V4_STRATEGY.md Section 7.2/7.3.

Section 7.2 — OU Process Parameters (MLE):
    ρ         = lag-1 autocorrelation of filtered state X
    θ         = -ln(ρ) / Δt
    μ         = mean(X)
    ε_i       = X[i+1] - X[i]·e^(-θΔt) - μ(1 - e^(-θΔt))
    σ²        = (2θ / (T(1-e^(-2θΔt)))) × Σ(ε_i²)
    half_life = ln(2) / θ

Section 7.3 — Conviction Score:
    σ_eq = sqrt(σ² / (2θ))
    z    = (x_current - μ) / σ_eq
    if |z| > 3.0: return None — regime break
    C    = erf(|z| / sqrt(2))
    if C < 0.65: return None — insufficient edge
"""
from __future__ import annotations

from dataclasses import dataclass
from math import erf, exp, log, sqrt

import numpy as np
import structlog

logger = structlog.get_logger(__name__)

# Δt = 1 (H1 candles, 1-hour intervals).
_DELTA_T = 1.0

# Maximum half-life in H1 candles (48 = 2 days).
_MAX_HALF_LIFE = 48.0

# Z-score guard for regime break detection.
_ZSCORE_GUARD = 3.0

# Minimum conviction to emit a signal.
_MIN_CONVICTION = 0.65


@dataclass(frozen=True)
class OUParams:
    """Ornstein–Uhlenbeck process parameters from MLE."""

    rho: float       # lag-1 autocorrelation
    theta: float     # mean-reversion speed
    mu: float        # long-run mean
    sigma_sq: float  # process variance
    half_life: float # ln(2) / θ in H1 candles


@dataclass(frozen=True)
class ConvictionResult:
    """Conviction score output."""

    z_score: float     # (x_current - μ) / σ_eq
    sigma_eq: float    # equilibrium std dev
    conviction: float  # erf(|z| / sqrt(2)), bounded [0, 1]


def fit_ou(states: np.ndarray) -> OUParams | None:
    """Estimate OU parameters via MLE from Kalman-filtered states.

    Implements Section 7.2 exactly.

    Parameters
    ----------
    states : np.ndarray
        Kalman-filtered state estimates (H1 close series).

    Returns
    -------
    OUParams | None
        Estimated parameters, or None if the series shows no
        mean reversion (ρ ≤ 0) or reverts too slowly (half_life > 48).
    """
    n = len(states)
    if n < 3:
        logger.info("ou_rejected", reason="insufficient_states", n=n)
        return None

    # ── ρ: lag-1 autocorrelation of X ─────────────────────────────
    x = states - np.mean(states)
    autocov_0 = np.sum(x[:-1] * x[:-1])
    if autocov_0 == 0:
        logger.info("ou_rejected", reason="zero_variance")
        return None
    rho = float(np.sum(x[:-1] * x[1:]) / autocov_0)

    # Gate: ρ ≤ 0 → no mean reversion.
    if rho <= 0:
        logger.info("ou_rejected", reason="rho_non_positive", rho=rho)
        return None

    # ── θ = -ln(ρ) / Δt ──────────────────────────────────────────
    theta = -log(rho) / _DELTA_T

    # ── μ = mean(X) ───────────────────────────────────────────────
    mu = float(np.mean(states))

    # ── ε_i = X[i+1] - X[i]·e^(-θΔt) - μ(1 - e^(-θΔt)) ────────
    e_neg_theta_dt = exp(-theta * _DELTA_T)
    residuals = (
        states[1:]
        - states[:-1] * e_neg_theta_dt
        - mu * (1.0 - e_neg_theta_dt)
    )

    # ── σ² = (2θ / (T(1 - e^(-2θΔt)))) × Σ(ε_i²) ──────────────
    T = len(residuals)
    e_neg_2theta_dt = exp(-2.0 * theta * _DELTA_T)
    denom = T * (1.0 - e_neg_2theta_dt)
    if denom == 0:
        logger.info("ou_rejected", reason="zero_denominator_sigma")
        return None
    sigma_sq = float((2.0 * theta / denom) * np.sum(residuals ** 2))

    # ── half_life = ln(2) / θ ────────────────────────────────────
    half_life = log(2.0) / theta

    # Gate: half_life > 48 H1 candles → too slow.
    if half_life > _MAX_HALF_LIFE:
        logger.info(
            "ou_rejected", reason="half_life_too_long",
            half_life=round(half_life, 2),
        )
        return None

    params = OUParams(
        rho=round(rho, 6),
        theta=round(theta, 6),
        mu=round(mu, 6),
        sigma_sq=round(sigma_sq, 10),
        half_life=round(half_life, 2),
    )

    logger.info(
        "ou_fitted",
        rho=params.rho,
        theta=params.theta,
        mu=params.mu,
        sigma_sq=params.sigma_sq,
        half_life=params.half_life,
    )
    return params


def compute_conviction(
    x_current: float,
    params: OUParams,
    zscore_guard: float = _ZSCORE_GUARD,
    min_conviction: float = _MIN_CONVICTION,
) -> ConvictionResult | None:
    """Compute conviction score per Section 7.3.

    Parameters
    ----------
    x_current : float
        Current Kalman-filtered state value.
    params : OUParams
        Fitted OU parameters.
    zscore_guard : float
        Maximum |z| before regime break rejection (default 3.0).
    min_conviction : float
        Minimum conviction to accept (default 0.65).

    Returns
    -------
    ConvictionResult | None
        Conviction result, or None if regime break or insufficient edge.
    """
    # σ_eq = sqrt(σ² / (2θ))
    if params.theta <= 0:
        logger.info("conviction_rejected", reason="theta_non_positive")
        return None

    sigma_eq = sqrt(params.sigma_sq / (2.0 * params.theta))
    if sigma_eq == 0:
        logger.info("conviction_rejected", reason="zero_sigma_eq")
        return None

    # z = (x_current - μ) / σ_eq
    z = (x_current - params.mu) / sigma_eq

    # Gate: |z| > 3.0 → regime break suspected.
    if abs(z) > zscore_guard:
        logger.info(
            "conviction_rejected",
            reason="regime_break_suspected",
            z_score=round(z, 4),
        )
        return None

    # C = erf(|z| / sqrt(2))
    conviction = erf(abs(z) / sqrt(2.0))

    # Gate: C < 0.65 → insufficient edge.
    if conviction < min_conviction:
        logger.info(
            "conviction_rejected",
            reason="insufficient_edge",
            conviction=round(conviction, 4),
            z_score=round(z, 4),
        )
        return None

    result = ConvictionResult(
        z_score=round(z, 6),
        sigma_eq=round(sigma_eq, 8),
        conviction=round(conviction, 6),
    )

    logger.info(
        "conviction_computed",
        z_score=result.z_score,
        sigma_eq=result.sigma_eq,
        conviction=result.conviction,
    )
    return result
