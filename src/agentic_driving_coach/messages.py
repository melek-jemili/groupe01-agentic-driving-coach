"""Message types exchanged between reactors and backends.

The enums preserve the names and integer codes used by the original
agentic-driving-coach behavior files (1-4 accelerate, 5-6 brake), so the
recorded driver traces in ``data/driver/`` can be replayed unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum


class DriverAction(IntEnum):
    """One driver (or coach actuation) command per 100 ms tick."""

    COASTING = 1
    CRUISE = 2
    NORMAL_ACCELERATION = 3
    STRONG_ACCELERATION = 4
    GENTLE_BRAKING = 5
    EMERGENCY_BRAKING = 6

    @property
    def is_brake(self) -> bool:
        return self in (DriverAction.GENTLE_BRAKING, DriverAction.EMERGENCY_BRAKING)


#: Net acceleration (m/s^2) applied by the car for each action.
#: Values from the Car reactor in StopSign.lf (agentic-driving-coach).
ACCELERATION_MAP: dict[DriverAction, float] = {
    DriverAction.COASTING: -0.1,
    DriverAction.CRUISE: 0.1,
    DriverAction.NORMAL_ACCELERATION: 2.0,
    DriverAction.STRONG_ACCELERATION: 4.0,
    DriverAction.GENTLE_BRAKING: -3.0,
    DriverAction.EMERGENCY_BRAKING: -9.0,
}


class CoachToken(Enum):
    """The strict token part of the ``TOKEN|Message`` coach output contract."""

    NONE = "NONE"
    WARNING = "WARNING"
    ACTUATE = "ACTUATE"


class PlannerMode(Enum):
    """Explicit planner state, replacing Lingua Franca's modal reactors."""

    MONITORING = "MONITORING"
    WARNING = "WARNING"
    ACTUATE = "ACTUATE"


class DecisionSource(Enum):
    """Where a coach decision came from (for logging and analysis)."""

    RULE = "rule"          # deterministic rule backend
    MODEL = "model"        # validated live/replayed model output
    FALLBACK = "fallback"  # deterministic fallback (deadline miss or malformed)


@dataclass(frozen=True)
class StateSnapshot:
    """What the coach observes at one logical tag."""

    distance_m: float
    velocity_mps: float


@dataclass(frozen=True)
class InferenceRequest:
    """One inference opportunity handed to a coach backend."""

    request_id: int
    snapshot: StateSnapshot
    logical_elapsed_ms: float


@dataclass(frozen=True)
class InferenceResult:
    """What a backend returns (or delivers asynchronously) for a request.

    ``latency_ms`` is wall-clock inference latency for the live backend, the
    recorded latency for the replay backend, and 0.0 for the rule backend.
    ``None`` marks a replayed request whose response was never observed in
    the recorded run: it is resolved by the deadline/fallback path only.
    """

    request_id: int
    raw_response: str
    token: CoachToken
    message: str
    latency_ms: float | None
    malformed: bool = False
    error: str | None = None


@dataclass(frozen=True)
class CoachDecision:
    """The decision the inference reactor hands to the planner.

    Exactly one decision is emitted per accepted inference request: either the
    validated model/rule result or the deterministic fallback.
    """

    request_id: int
    token: CoachToken
    message: str
    source: DecisionSource
    snapshot: StateSnapshot
    latency_ms: float
    deadline_miss: bool = False
    fallback_used: bool = False
    malformed_response: bool = False
    raw_response: str = ""
    error: str | None = None


@dataclass
class InferenceStats:
    """Counters maintained by the inference reactor across a run."""

    requests: int = 0
    deadline_misses: int = 0
    fallbacks: int = 0
    malformed: int = 0
    late_responses_discarded: int = 0
    opportunities_skipped: int = 0
    errors: int = 0
    replay_mismatches: int = 0
    latencies_ms: list[float] = field(default_factory=list)
