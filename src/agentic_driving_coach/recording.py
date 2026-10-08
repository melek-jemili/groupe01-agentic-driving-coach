"""Run recording: per-event rows, replayable inference trace, and manifest.

One RunRecorder instance accumulates everything a run produces and writes,
at the end of the run:

- ``run.csv``      one row per logical tag observed by the recorder reactor
- ``trace.jsonl``  one entry per inference request (replayable with
                   ``--coach replay --trace ...``)
- ``manifest.json``  software versions and all parameters of the run
- ``summary.json``   aggregate metrics (written by the CLI via metrics.py)

Field names include their units where applicable.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import platform
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import RunConfig
from .messages import CoachDecision, DecisionSource, InferenceResult
from .policies import desired_velocity, safe_velocity_band


@dataclass
class EventRow:
    """One recorded logical tag. Sparse: fields are None when no event of
    that kind occurred at the tag; physical state is forward-filled."""

    logical_time_ms: float
    wall_clock: str
    xronos_lag_ms: float
    xronos_slack_ms: float | None

    # Physical state (forward-filled from the latest car/environment events)
    distance_m: float | None = None
    velocity_mps: float | None = None
    #: Cumulative distance travelled (m); set only on tags with a velocity
    #: event, so it also marks "the car moved at this tag".
    travelled_m: float | None = None
    desired_velocity_mps: float | None = None
    safe_lower_mps: float | None = None
    safe_upper_mps: float | None = None

    # Driver / car events at this tag
    driver_action: str | None = None
    applied_action: str | None = None

    # Coach decision events at this tag
    request_id: int | None = None
    coach_token: str | None = None
    coach_message: str | None = None
    coach_source: str | None = None
    req_distance_m: float | None = None
    req_velocity_mps: float | None = None
    inference_latency_ms: float | None = None
    deadline_ms: float | None = None
    deadline_slack_ms: float | None = None
    deadline_miss: bool = False
    fallback_used: bool = False
    malformed_response: bool = False
    late_response_discarded: bool = False
    inference_opportunity_skipped: bool = False

    # Planner events at this tag
    planner_mode: str | None = None
    actuation: str | None = None
    instruction: str | None = None


@dataclass
class TraceRecord:
    """One inference request, in the replayable trace format."""

    request_id: int
    logical_elapsed_ms: float
    distance_m: float
    velocity_mps: float
    raw_response: str = ""
    latency_ms: float | None = None
    error: str | None = None
    deadline_miss: bool = False


_STATIC_COLUMNS = ("run_id", "scenario", "driver_level", "coach_backend", "model")


class RunRecorder:
    """Accumulates rows and trace entries; writes all artifacts at the end."""

    def __init__(self, config: RunConfig, model_label: str) -> None:
        self.config = config
        self.model_label = model_label
        self.run_id = config.run_id or uuid.uuid4().hex[:12]
        self.rows: list[EventRow] = []
        self._trace: dict[int, TraceRecord] = {}
        self._last_distance: float | None = None
        self._last_velocity: float | None = None
        self._last_mode: str | None = None
        self._travelled = 0.0

    # ------------------------------------------------------------------
    # Called from inside recorder-reactor handlers
    # ------------------------------------------------------------------
    def new_row(self, logical_time_ms: float, lag_ms: float, slack_ms: float | None) -> EventRow:
        row = EventRow(
            logical_time_ms=logical_time_ms,
            wall_clock=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            xronos_lag_ms=lag_ms,
            xronos_slack_ms=slack_ms,
            distance_m=self._last_distance,
            velocity_mps=self._last_velocity,
            planner_mode=self._last_mode,
        )
        self.rows.append(row)
        return row

    def observe_state(self, row: EventRow, distance: float | None, velocity: float | None) -> None:
        if distance is not None:
            self._last_distance = distance
        if velocity is not None:
            self._last_velocity = velocity
            # Mirrors the car's integration step (same velocity, same dt), so
            # overshoot past the sign stays measurable after the environment
            # clamps distance at zero.
            self._travelled += velocity * self.config.scenario.dt_s
            row.travelled_m = round(self._travelled, 6)
        row.distance_m = self._last_distance
        row.velocity_mps = self._last_velocity
        if self._last_distance is not None:
            policy = self.config.scenario.policy
            row.desired_velocity_mps = desired_velocity(self._last_distance, policy)
            band = safe_velocity_band(self._last_distance, policy)
            if band is not None:
                row.safe_lower_mps, row.safe_upper_mps = band

    def observe_mode(self, row: EventRow, mode: str) -> None:
        self._last_mode = mode
        row.planner_mode = mode

    def observe_decision(self, row: EventRow, decision: CoachDecision) -> None:
        row.request_id = decision.request_id
        row.coach_token = decision.token.value
        row.coach_message = decision.message
        row.coach_source = decision.source.value
        row.req_distance_m = decision.snapshot.distance_m
        row.req_velocity_mps = decision.snapshot.velocity_mps
        row.inference_latency_ms = decision.latency_ms
        row.deadline_ms = self.config.model.deadline_ms
        row.deadline_miss = decision.deadline_miss
        row.fallback_used = decision.fallback_used
        row.malformed_response = decision.malformed_response
        if decision.request_id >= 0:
            record = self._trace.setdefault(
                decision.request_id,
                TraceRecord(
                    request_id=decision.request_id,
                    logical_elapsed_ms=row.logical_time_ms - decision.latency_ms,
                    distance_m=decision.snapshot.distance_m,
                    velocity_mps=decision.snapshot.velocity_mps,
                ),
            )
            record.deadline_miss = decision.deadline_miss
            if decision.deadline_miss:
                # The model's true response (if any) arrives separately as a
                # late response; until then the trace records "never arrived".
                record.raw_response = ""
                record.latency_ms = None
                record.error = decision.error or "deadline miss; response not yet observed"
            else:
                record.raw_response = decision.raw_response
                record.latency_ms = decision.latency_ms
                record.error = decision.error
            row.deadline_slack_ms = self.config.model.deadline_ms - decision.latency_ms

    def observe_late_response(self, row: EventRow, result: InferenceResult) -> None:
        row.request_id = result.request_id
        row.late_response_discarded = True
        row.inference_latency_ms = result.latency_ms
        if result.latency_ms is not None:
            row.deadline_slack_ms = self.config.model.deadline_ms - result.latency_ms
        record = self._trace.get(result.request_id)
        if record is not None:
            record.raw_response = result.raw_response
            record.latency_ms = result.latency_ms
            record.error = result.error

    def observe_skip(self, row: EventRow) -> None:
        row.inference_opportunity_skipped = True

    def observe_actuation(self, row: EventRow, action_name: str) -> None:
        row.actuation = action_name

    def observe_instruction(self, row: EventRow, text: str) -> None:
        row.instruction = text

    def observe_driver_action(self, row: EventRow, action_name: str) -> None:
        row.driver_action = action_name

    def observe_applied_action(self, row: EventRow, action_name: str) -> None:
        row.applied_action = action_name

    # ------------------------------------------------------------------
    # Output files
    # ------------------------------------------------------------------
    def _static_values(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "scenario": self.config.scenario.name,
            "driver_level": self.config.driver_level,
            "coach_backend": self.config.coach_backend,
            "model": self.model_label,
        }

    def write_csv(self, path: Path) -> None:
        columns = list(_STATIC_COLUMNS) + [f.name for f in dataclasses.fields(EventRow)]
        static = self._static_values()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            for row in self.rows:
                record: dict[str, Any] = dict(static)
                record.update(dataclasses.asdict(row))
                writer.writerow(
                    {k: ("" if v is None else v) for k, v in record.items()}
                )

    def write_trace(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for request_id in sorted(self._trace):
                fh.write(json.dumps(dataclasses.asdict(self._trace[request_id])) + "\n")

    def write_manifest(self, path: Path, argv: list[str]) -> None:
        try:
            from importlib.metadata import version

            versions = {
                name: version(name) for name in ("xronos", "matplotlib", "ollama")
            }
        except Exception:  # pragma: no cover - metadata lookup is best-effort
            versions = {}
        manifest = {
            "run_id": self.run_id,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "argv": argv,
            "config": _config_as_dict(self.config),
            "model_label": self.model_label,
            "python": sys.version,
            "platform": platform.platform(),
            "package_versions": versions,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    @property
    def decisions(self) -> list[EventRow]:
        return [r for r in self.rows if r.coach_token is not None]

    @property
    def model_decision_sources(self) -> list[DecisionSource]:
        return [DecisionSource(r.coach_source) for r in self.decisions if r.coach_source]


def _config_as_dict(config: RunConfig) -> dict[str, Any]:
    def convert(value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {f.name: convert(getattr(value, f.name)) for f in dataclasses.fields(value)}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, tuple):
            return list(value)
        return value

    return convert(config)
