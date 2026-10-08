"""Aggregate metrics for one run (summary.json).

The oracle used for agreement scoring is the deterministic rule policy
evaluated on the *request-time* snapshot of each decision.
"""

from __future__ import annotations

import dataclasses
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

from .config import RunConfig
from .messages import CoachToken, InferenceStats
from .policies import stop_sign_policy
from .recording import RunRecorder


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile (no numpy dependency for the core path)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, round(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True)
class LatencyStats:
    count: int
    median_ms: float | None
    p95_ms: float | None
    max_ms: float | None

    @staticmethod
    def from_values(values: list[float]) -> LatencyStats:
        p95 = _percentile(values, 0.95)
        return LatencyStats(
            count=len(values),
            median_ms=round(statistics.median(values), 3) if values else None,
            p95_ms=round(p95, 3) if p95 is not None else None,
            max_ms=round(max(values), 3) if values else None,
        )


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    scenario: str
    driver_level: str
    coach_backend: str
    model: str
    deadline_ms: float
    initial_velocity_mps: float
    initial_distance_m: float
    fast_mode: bool

    inference_requests: int
    inference_latency: LatencyStats
    xronos_lag: LatencyStats

    deadline_miss_count: int
    deadline_miss_rate: float | None
    malformed_response_count: int
    fallback_count: int
    late_response_count: int
    skipped_inference_count: int
    inference_error_count: int
    replay_mismatch_count: int

    oracle_agreement_count: int
    oracle_agreement_rate: float | None
    unsafe_false_negative_count: int

    warning_count: int
    actuation_count: int

    final_velocity_mps: float | None
    final_distance_m: float | None
    velocity_at_stop_line_mps: float | None
    min_distance_m: float | None
    stopped: bool
    stop_position_error_m: float | None

    safe_bound_violation_count: int
    safe_bound_violation_duration_s: float

    def to_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(dataclasses.asdict(self), indent=2) + "\n", encoding="utf-8"
        )


def compute_summary(
    recorder: RunRecorder, stats: InferenceStats, config: RunConfig
) -> RunSummary:
    rows = recorder.rows
    decisions = [r for r in rows if r.coach_token is not None and (r.request_id or 0) >= 0]

    # Oracle scoring on request-time snapshots.
    agreement = 0
    unsafe_false_negatives = 0
    scored = 0
    for row in decisions:
        if row.req_distance_m is None or row.req_velocity_mps is None:
            continue
        scored += 1
        oracle_token, _ = stop_sign_policy(
            row.req_distance_m, row.req_velocity_mps, config.scenario.policy
        )
        if row.coach_token == oracle_token.value:
            agreement += 1
        if oracle_token is CoachToken.ACTUATE and row.coach_token != CoachToken.ACTUATE.value:
            unsafe_false_negatives += 1

    # Physical outcome, from rows that carried an actual velocity event.
    motion = [r for r in rows if r.travelled_m is not None]
    final = motion[-1] if motion else None
    min_distance = min((r.distance_m for r in motion if r.distance_m is not None), default=None)

    # Velocity at the stop line: the crossing velocity if the line was
    # reached; otherwise the settled velocity at the closest approach (the
    # *last* row at the minimum distance, so a car that stopped short
    # reports 0.0, not the speed of its final rolling step).
    velocity_at_stop_line: float | None = None
    at_line = [r for r in motion if r.distance_m == 0.0]
    if at_line:
        velocity_at_stop_line = at_line[0].velocity_mps
    elif motion and min_distance is not None:
        nearest = [r for r in motion if r.distance_m == min_distance]
        velocity_at_stop_line = nearest[-1].velocity_mps if nearest else None

    stopped_rows = [r for r in motion if r.velocity_mps == 0.0]
    stop_position_error: float | None = None
    if stopped_rows:
        first_stop = stopped_rows[0]
        if first_stop.travelled_m is not None:
            stop_position_error = round(
                first_stop.travelled_m - config.scenario.initial_distance_m, 3
            )

    violations = [
        r
        for r in motion
        if r.safe_lower_mps is not None
        and r.safe_upper_mps is not None
        and r.velocity_mps is not None
        and not (r.safe_lower_mps <= r.velocity_mps <= r.safe_upper_mps)
    ]

    warning_count = sum(1 for r in decisions if r.coach_token == CoachToken.WARNING.value)
    actuation_count = sum(1 for r in rows if r.actuation is not None)

    requests = stats.requests
    return RunSummary(
        run_id=recorder.run_id,
        scenario=config.scenario.name,
        driver_level=config.driver_level,
        coach_backend=config.coach_backend,
        model=recorder.model_label,
        deadline_ms=config.model.deadline_ms,
        initial_velocity_mps=config.scenario.initial_velocity_mps,
        initial_distance_m=config.scenario.initial_distance_m,
        fast_mode=config.fast,
        inference_requests=requests,
        inference_latency=LatencyStats.from_values(stats.latencies_ms),
        xronos_lag=LatencyStats.from_values([r.xronos_lag_ms for r in rows]),
        deadline_miss_count=stats.deadline_misses,
        deadline_miss_rate=round(stats.deadline_misses / requests, 4) if requests else None,
        malformed_response_count=stats.malformed,
        fallback_count=stats.fallbacks,
        late_response_count=stats.late_responses_discarded,
        skipped_inference_count=stats.opportunities_skipped,
        inference_error_count=stats.errors,
        replay_mismatch_count=stats.replay_mismatches,
        oracle_agreement_count=agreement,
        oracle_agreement_rate=round(agreement / scored, 4) if scored else None,
        unsafe_false_negative_count=unsafe_false_negatives,
        warning_count=warning_count,
        actuation_count=actuation_count,
        final_velocity_mps=final.velocity_mps if final else None,
        final_distance_m=final.distance_m if final else None,
        velocity_at_stop_line_mps=velocity_at_stop_line,
        min_distance_m=min_distance,
        stopped=bool(stopped_rows),
        stop_position_error_m=stop_position_error,
        safe_bound_violation_count=len(violations),
        safe_bound_violation_duration_s=round(
            len(violations) * config.scenario.dt_s, 3
        ),
    )


def format_terminal_summary(summary: RunSummary) -> str:
    """Concise human-readable end-of-run report."""
    lat = summary.inference_latency

    def fmt(value: float | None, suffix: str = "") -> str:
        return "n/a" if value is None else f"{value:.1f}{suffix}"

    def fmt_rate(value: float | None) -> str:
        return "n/a" if value is None else str(value)

    lines = [
        f"run {summary.run_id}: {summary.scenario} | driver={summary.driver_level} "
        f"| coach={summary.coach_backend} | model={summary.model}",
        f"  outcome: stopped={summary.stopped} "
        f"v_at_stop_line={fmt(summary.velocity_at_stop_line_mps, ' m/s')} "
        f"stop_error={fmt(summary.stop_position_error_m, ' m')} "
        f"final_v={fmt(summary.final_velocity_mps, ' m/s')}",
        f"  inference: requests={summary.inference_requests} "
        f"latency median/p95/max={fmt(lat.median_ms)}/{fmt(lat.p95_ms)}/{fmt(lat.max_ms)} ms "
        f"(deadline {summary.deadline_ms:.0f} ms)",
        f"  timeliness: misses={summary.deadline_miss_count} "
        f"(rate={fmt_rate(summary.deadline_miss_rate)}) "
        f"fallbacks={summary.fallback_count} late_discarded={summary.late_response_count} "
        f"skipped={summary.skipped_inference_count}",
        f"  quality: malformed={summary.malformed_response_count} "
        f"oracle_agreement={fmt_rate(summary.oracle_agreement_rate)} "
        f"unsafe_false_negatives={summary.unsafe_false_negative_count}",
        f"  coaching: warnings={summary.warning_count} actuations={summary.actuation_count} "
        f"safe_bound_violations={summary.safe_bound_violation_count} "
        f"({summary.safe_bound_violation_duration_s:.1f} s)",
    ]
    return "\n".join(lines)
