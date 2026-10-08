"""Recorder reactor: the headless observer of the whole system.

One reaction with many triggers fires once per logical tag at which anything
observable happened, and appends one EventRow to the RunRecorder. The
reaction is declared with a deadline so every row carries a real
``ctx.lag`` / ``ctx.slack`` measurement from the Xronos 0.13 timing API.
In ``--fast`` runs, wall-clock quantities are not meaningful.

The recorder also ends the run: when the driver trace is exhausted it
schedules a shutdown a configurable grace period later, giving in-flight
delayed connections and inference decisions time to settle. A hard
``Environment(timeout=...)`` bound exists as well, so every run terminates.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

import xronos

from ..messages import CoachDecision, DriverAction, InferenceResult, PlannerMode
from ..recording import RunRecorder

logger = logging.getLogger(__name__)

#: Processing deadline for one recording step (source of ctx.slack).
RECORDER_DEADLINE = timedelta(milliseconds=50)


class Recorder(xronos.Reactor):
    velocity_in = xronos.InputPortDeclaration[float]()
    distance_in = xronos.InputPortDeclaration[float]()
    driver_accelerator_in = xronos.InputPortDeclaration[DriverAction]()
    driver_brake_in = xronos.InputPortDeclaration[DriverAction]()
    applied_action_in = xronos.InputPortDeclaration[DriverAction]()
    decision_in = xronos.InputPortDeclaration[CoachDecision]()
    late_response_in = xronos.InputPortDeclaration[InferenceResult]()
    skipped_in = xronos.InputPortDeclaration[int]()
    planner_mode_in = xronos.InputPortDeclaration[PlannerMode]()
    actuate_in = xronos.InputPortDeclaration[DriverAction]()
    instruction_in = xronos.InputPortDeclaration[str]()
    driver_finished_in = xronos.InputPortDeclaration[bool]()

    _shutdown_timer = xronos.ProgrammableTimerDeclaration[None]()

    def __init__(self, recorder: RunRecorder, shutdown_grace: timedelta) -> None:
        super().__init__()
        self._recorder = recorder
        self._grace = shutdown_grace
        self._shutdown_scheduled = False

    @xronos.reaction_with_deadline(deadline=RECORDER_DEADLINE)
    def collect(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        velocity = ctx.add_trigger(self.velocity_in)
        distance = ctx.add_trigger(self.distance_in)
        driver_accelerator = ctx.add_trigger(self.driver_accelerator_in)
        driver_brake = ctx.add_trigger(self.driver_brake_in)
        applied_action = ctx.add_trigger(self.applied_action_in)
        decision = ctx.add_trigger(self.decision_in)
        late_response = ctx.add_trigger(self.late_response_in)
        skipped = ctx.add_trigger(self.skipped_in)
        planner_mode = ctx.add_trigger(self.planner_mode_in)
        actuate = ctx.add_trigger(self.actuate_in)
        instruction = ctx.add_trigger(self.instruction_in)
        finished = ctx.add_trigger(self.driver_finished_in)
        shutdown_timer = ctx.add_effect(self._shutdown_timer)

        def handler() -> None:
            row = self._recorder.new_row(
                logical_time_ms=ctx.elapsed_time.total_seconds() * 1000.0,
                lag_ms=ctx.lag.total_seconds() * 1000.0,
                slack_ms=ctx.slack.total_seconds() * 1000.0,
            )
            self._recorder.observe_state(
                row,
                distance.get() if distance.is_present() else None,
                velocity.get() if velocity.is_present() else None,
            )
            if driver_accelerator.is_present():
                self._recorder.observe_driver_action(row, driver_accelerator.get().name)
            if driver_brake.is_present():
                self._recorder.observe_driver_action(row, driver_brake.get().name)
            if applied_action.is_present():
                self._recorder.observe_applied_action(row, applied_action.get().name)
            if planner_mode.is_present():
                self._recorder.observe_mode(row, planner_mode.get().value)
            if decision.is_present():
                self._recorder.observe_decision(row, decision.get())
            if late_response.is_present():
                self._recorder.observe_late_response(row, late_response.get())
            if skipped.is_present():
                self._recorder.observe_skip(row)
            if actuate.is_present():
                self._recorder.observe_actuation(row, actuate.get().name)
                logger.info(
                    "[t=%7.1f ms] coach actuates: %s",
                    row.logical_time_ms,
                    actuate.get().name,
                )
            if instruction.is_present():
                self._recorder.observe_instruction(row, instruction.get())
                logger.info("[t=%7.1f ms] %s", row.logical_time_ms, instruction.get())

            if finished.is_present() and not self._shutdown_scheduled:
                self._shutdown_scheduled = True
                shutdown_timer.schedule(None, self._grace)
                logger.info(
                    "[t=%7.1f ms] driver trace exhausted; shutting down in %.1f s",
                    row.logical_time_ms,
                    self._grace.total_seconds(),
                )

        return handler

    @xronos.reaction
    def end_run(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._shutdown_timer)
        shutdown = ctx.add_effect(self.shutdown)

        def handler() -> None:
            shutdown.trigger_shutdown()

        return handler
