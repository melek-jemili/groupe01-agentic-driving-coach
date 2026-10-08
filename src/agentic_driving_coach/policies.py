"""Deterministic stop-sign coaching policy.

This is the executable ``fallback_policy()`` from StopSign.lf in
agentic-driving-coach, with the boundary conditions of that function:

- d <= 0                          -> NONE   (sign already passed)
- d <= 25  and v > 2.5            -> ACTUATE
- 50 < d <= 60 and v outside [8, 10] -> WARNING
- d >= 99  and v outside [8, 12]  -> WARNING
- otherwise                       -> NONE

The same function serves three roles: the RuleCoach backend, the deadline /
malformed-output fallback, and the oracle used to score model decisions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .messages import CoachToken

ACTUATE_MESSAGE = "Brake now."
MID_WARNING_MESSAGE = "Adjust speed and prepare to stop."
FAR_WARNING_MESSAGE = "Stop sign ahead."


@dataclass(frozen=True)
class StopSignPolicyParams:
    """Boundary parameters of the stop-sign policy (configs/stop_sign.toml)."""

    actuate_max_distance_m: float = 25.0
    actuate_min_velocity_mps: float = 2.5
    mid_band_min_distance_m: float = 50.0   # exclusive lower bound
    mid_band_max_distance_m: float = 60.0   # inclusive upper bound
    mid_band_velocity_mps: tuple[float, float] = (8.0, 10.0)
    far_min_distance_m: float = 99.0        # inclusive lower bound
    far_band_velocity_mps: tuple[float, float] = (8.0, 12.0)


def stop_sign_policy(
    distance_m: float,
    velocity_mps: float,
    params: StopSignPolicyParams | None = None,
) -> tuple[CoachToken, str]:
    """Return the deterministic (token, message) for one state snapshot."""
    p = params or StopSignPolicyParams()

    if distance_m <= 0.0:
        return CoachToken.NONE, ""

    if distance_m <= p.actuate_max_distance_m and velocity_mps > p.actuate_min_velocity_mps:
        return CoachToken.ACTUATE, ACTUATE_MESSAGE

    lo, hi = p.mid_band_velocity_mps
    if (
        p.mid_band_min_distance_m < distance_m <= p.mid_band_max_distance_m
        and not (lo <= velocity_mps <= hi)
    ):
        return CoachToken.WARNING, MID_WARNING_MESSAGE

    lo, hi = p.far_band_velocity_mps
    if distance_m >= p.far_min_distance_m and not (lo <= velocity_mps <= hi):
        return CoachToken.WARNING, FAR_WARNING_MESSAGE

    return CoachToken.NONE, ""


def safe_velocity_band(
    distance_m: float,
    params: StopSignPolicyParams | None = None,
) -> tuple[float, float] | None:
    """The velocity band the policy considers safe at this distance.

    Returns (lower, upper) in m/s, or None where the policy imposes no band.
    Near the sign (d <= 25 m) the "band" is [0, actuate_min_velocity]:
    faster than that triggers emergency actuation.
    """
    p = params or StopSignPolicyParams()
    if distance_m <= 0.0:
        return None
    if distance_m <= p.actuate_max_distance_m:
        return (0.0, p.actuate_min_velocity_mps)
    if p.mid_band_min_distance_m < distance_m <= p.mid_band_max_distance_m:
        return p.mid_band_velocity_mps
    if distance_m >= p.far_min_distance_m:
        return p.far_band_velocity_mps
    return None


def desired_velocity(
    distance_m: float,
    params: StopSignPolicyParams | None = None,
) -> float | None:
    """A reference velocity for logging/plots: the center of the active band
    (0 near the sign, where the goal is to stop). None where no band applies.
    """
    band = safe_velocity_band(distance_m, params)
    if band is None:
        return None
    p = params or StopSignPolicyParams()
    if distance_m <= p.actuate_max_distance_m:
        return 0.0
    return (band[0] + band[1]) / 2.0
