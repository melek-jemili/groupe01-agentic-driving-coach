"""RuleCoach backend: the deterministic policy, no LLM, zero latency.

Used for smoke runs, as the reference behavior, and as the oracle that
model decisions are scored against.
"""

from __future__ import annotations

from ..messages import InferenceRequest, InferenceResult
from ..policies import StopSignPolicyParams, stop_sign_policy
from .base import InferenceBackend


class RuleBackend(InferenceBackend):
    name = "rule"
    model_label = "rule-policy"
    is_async = False

    def __init__(self, policy_params: StopSignPolicyParams | None = None) -> None:
        self._params = policy_params or StopSignPolicyParams()

    def compute(self, request: InferenceRequest) -> InferenceResult:
        token, message = stop_sign_policy(
            request.snapshot.distance_m, request.snapshot.velocity_mps, self._params
        )
        raw = f"{token.value}|{message}"
        return InferenceResult(
            request_id=request.request_id,
            raw_response=raw,
            token=token,
            message=message,
            latency_ms=0.0,
        )

    def close(self) -> None:
        pass
