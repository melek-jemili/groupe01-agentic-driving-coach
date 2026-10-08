"""ReplayCoach backend: replays a recorded trace of model responses.

A trace is a JSON-Lines file, one object per inference request, exactly as
written by ``recording.RunRecorder`` during a live run (results/<run>/trace.jsonl):

    {"request_id": 0, "distance_m": 95.0, "velocity_mps": 10.0,
     "raw_response": "NONE|", "latency_ms": 141.2, "error": null}

Responses are matched to requests **by sequence number**. Each replayed
response carries its recorded latency, which the inference reactor turns into
a logical-time delay - so a replayed run reproduces the timing behavior of
the original run (including deadline misses) deterministically, with no model
server, and safely under ``fast=True``.

If the simulation asks for more requests than the trace holds, the extra
requests are answered by the deterministic fallback (recorded as such). If a
replayed snapshot disagrees with the observed one beyond a small tolerance,
a replay mismatch is counted - a hint that the trace came from a different
configuration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..messages import CoachToken, InferenceRequest, InferenceResult
from ..parsing import parse_coach_output
from .base import InferenceBackend

SNAPSHOT_TOLERANCE = 1e-6


class ReplayTraceError(ValueError):
    """The replay trace file is missing fields or malformed."""


@dataclass(frozen=True)
class TraceEntry:
    request_id: int
    distance_m: float
    velocity_mps: float
    raw_response: str
    #: None means the response was never observed in the recorded run
    #: (deadline miss with no late arrival); replay resolves such requests
    #: through the deadline/fallback path only.
    latency_ms: float | None
    error: str | None = None


def load_trace(path: Path) -> list[TraceEntry]:
    entries: list[TraceEntry] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
                latency = obj["latency_ms"]
                entries.append(
                    TraceEntry(
                        request_id=int(obj["request_id"]),
                        distance_m=float(obj["distance_m"]),
                        velocity_mps=float(obj["velocity_mps"]),
                        raw_response=str(obj["raw_response"]),
                        latency_ms=float(latency) if latency is not None else None,
                        error=obj.get("error"),
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ReplayTraceError(f"{path}:{lineno}: invalid trace entry: {exc}") from exc
    if not entries:
        raise ReplayTraceError(f"{path}: trace contains no entries")
    return entries


class ReplayBackend(InferenceBackend):
    name = "replay"
    is_async = False

    def __init__(self, trace_path: Path) -> None:
        self._entries = load_trace(trace_path)
        self._next_index = 0
        self.mismatches = 0
        self.model_label = f"replay:{trace_path.name}"

    def compute(self, request: InferenceRequest) -> InferenceResult:
        if self._next_index >= len(self._entries):
            # Trace exhausted: report an error result; the inference reactor
            # substitutes the deterministic fallback and records it.
            return InferenceResult(
                request_id=request.request_id,
                raw_response="",
                token=CoachToken.NONE,
                message="",
                latency_ms=0.0,
                malformed=True,
                error="replay trace exhausted",
            )

        entry = self._entries[self._next_index]
        self._next_index += 1

        if (
            abs(entry.distance_m - request.snapshot.distance_m) > SNAPSHOT_TOLERANCE
            or abs(entry.velocity_mps - request.snapshot.velocity_mps) > SNAPSHOT_TOLERANCE
        ):
            self.mismatches += 1

        parsed = parse_coach_output(entry.raw_response)
        return InferenceResult(
            request_id=request.request_id,
            raw_response=entry.raw_response,
            token=parsed.token if parsed.token is not None else CoachToken.NONE,
            message=parsed.message,
            latency_ms=entry.latency_ms,
            malformed=parsed.malformed,
            error=entry.error,
        )

    def close(self) -> None:
        pass
