"""Scenario assembly: build the reactor graph, execute it, write artifacts.

This is the single place where the stop-sign topology is wired (the CLI
`run`, `benchmark-models`, and `compare-behaviors` commands all call
``run_scenario``):

    Driver --(500 ms)--> Car --> RoadEnvironment --> Coach
      ^                   ^                            |
      |                   +---------(200 ms)-----------+  actuate
      +--------------------- instruction ---------------+

plus a Recorder tapping every observable port. Delays are Xronos connection
delays (logical time), never sleeps. The run always terminates: a hard
``Environment(timeout=max_duration_s)`` plus an early shutdown once the
driver trace is exhausted (see reactors/recorder.py).
"""

from __future__ import annotations

import logging
import sys
from datetime import timedelta
from pathlib import Path

import xronos

from .backends.base import InferenceBackend
from .backends.replay import ReplayBackend
from .backends.rule import RuleBackend
from .config import ConfigError, RunConfig
from .messages import InferenceStats
from .metrics import RunSummary, compute_summary
from .reactors.car import Car
from .reactors.coach import Coach
from .reactors.driver import Driver, load_behavior_trace
from .reactors.environment import RoadEnvironment
from .reactors.recorder import Recorder
from .recording import RunRecorder

logger = logging.getLogger(__name__)


def build_backend(config: RunConfig) -> InferenceBackend:
    if config.coach_backend == "rule":
        return RuleBackend(config.scenario.policy)
    if config.coach_backend == "replay":
        assert config.replay_trace_path is not None  # validated upstream
        return ReplayBackend(config.replay_trace_path)
    if config.coach_backend == "ollama":
        from .backends.ollama import OllamaBackend  # deferred: needs the client lib

        return OllamaBackend(config.model)
    raise ConfigError(f"unknown coach backend {config.coach_backend!r}")


def run_scenario(
    config: RunConfig,
    argv: list[str] | None = None,
    telemetry: bool = False,
    telemetry_endpoint: str = "localhost:4317",
    backend: InferenceBackend | None = None,
) -> tuple[RunSummary, RunRecorder]:
    """Execute one run and write run.csv / trace.jsonl / manifest.json /
    summary.json into ``config.output_dir``. Returns the summary and the
    in-memory recorder used by the comparison commands.

    ``backend`` injects a pre-built backend;
    by default it is built from the configuration."""
    config.validate()

    trace_file = config.driver_trace_file()
    actions = load_behavior_trace(
        trace_file.read_text(encoding="utf-8").splitlines(), str(trace_file)
    )

    if backend is None:
        backend = build_backend(config)
        if config.coach_backend == "ollama":
            from .backends.ollama import OllamaBackend, OllamaUnavailableError

            assert isinstance(backend, OllamaBackend)
            available = backend.check_server()  # raises OllamaUnavailableError if down
            if config.model.model not in available:
                backend.close()
                raise OllamaUnavailableError(
                    f"model {config.model.model!r} is not available on the Ollama server "
                    f"(available: {available or 'none'}). Pull it first: "
                    f"ollama pull {config.model.model}"
                )
            # Pre-load before the reactor graph runs: a cold load on a shared
            # filesystem can take minutes and must never be measured as
            # inference latency or fought by the per-request timeout.
            logger.info(
                "warming up %s (a cold load can take minutes on shared filesystems)...",
                config.model.model,
            )
            try:
                load_s = backend.warm_up()
            except Exception:
                backend.close()
                raise
            logger.info("model %s resident (warm-up took %.1f s)", config.model.model, load_s)

    recorder_state = RunRecorder(config, backend.model_label)
    scenario = config.scenario

    try:
        env = xronos.Environment(
            fast=config.fast, timeout=timedelta(seconds=scenario.max_duration_s)
        )
        if telemetry:  # optional; never part of the graded workflow
            env.enable_telemetry(
                application_name=f"agentic-driving-coach-{recorder_state.run_id}",
                endpoint=telemetry_endpoint,
            )

        driver = env.create_reactor(
            "driver", Driver, actions, timedelta(milliseconds=scenario.driver_period_ms)
        )
        car = env.create_reactor(
            "car", Car, scenario.initial_velocity_mps, scenario.dt_s
        )
        road = env.create_reactor(
            "environment", RoadEnvironment, scenario.initial_distance_m, scenario.dt_s
        )
        coach = env.create_reactor(
            "coach",
            Coach,
            backend,
            timedelta(milliseconds=config.model.deadline_ms),
            scenario.policy,
            timedelta(seconds=scenario.warning_throttle_s),
        )
        recorder = env.create_reactor(
            "recorder",
            Recorder,
            recorder_state,
            timedelta(seconds=scenario.shutdown_grace_s),
        )

        driver_delay = timedelta(milliseconds=scenario.driver_to_car_delay_ms)
        actuate_delay = timedelta(milliseconds=scenario.coach_to_car_delay_ms)

        # The closed control loop.
        env.connect(driver.accelerator, car.accelerator_in, delay=driver_delay)
        env.connect(driver.brake, car.brake_in, delay=driver_delay)
        env.connect(car.velocity, road.velocity_in)
        env.connect(car.velocity, coach.velocity_in)
        env.connect(road.distance, coach.distance_in)
        env.connect(coach.actuate, car.actuate_in, delay=actuate_delay)
        env.connect(coach.instruction, driver.instruction)

        # Observability taps (driver commands delayed to align with the tag
        # at which the car applies them).
        env.connect(driver.accelerator, recorder.driver_accelerator_in, delay=driver_delay)
        env.connect(driver.brake, recorder.driver_brake_in, delay=driver_delay)
        env.connect(driver.finished, recorder.driver_finished_in)
        env.connect(car.velocity, recorder.velocity_in)
        env.connect(car.applied_action, recorder.applied_action_in)
        env.connect(road.distance, recorder.distance_in)
        env.connect(coach.decision, recorder.decision_in)
        env.connect(coach.late_response, recorder.late_response_in)
        env.connect(coach.skipped, recorder.skipped_in)
        env.connect(coach.planner_mode, recorder.planner_mode_in)
        env.connect(coach.actuate, recorder.actuate_in)
        env.connect(coach.instruction, recorder.instruction_in)

        env.execute()

        stats: InferenceStats = coach.inference.stats
        if isinstance(backend, ReplayBackend):
            stats.replay_mismatches = backend.mismatches
    finally:
        backend.close()

    summary = compute_summary(recorder_state, stats, config)

    output = Path(config.output_dir)
    recorder_state.write_csv(output / "run.csv")
    recorder_state.write_trace(output / "trace.jsonl")
    recorder_state.write_manifest(output / "manifest.json", argv or sys.argv)
    summary.to_json(output / "summary.json")
    logger.info("artifacts written to %s", output)
    return summary, recorder_state
