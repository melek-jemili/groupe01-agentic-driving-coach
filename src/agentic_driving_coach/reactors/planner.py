"""Planner reactor: the safety/moderation state machine of the coach.

Preserves the modal Planner of StopSign.lf, with Lingua Franca's first-class
modes replaced by an explicit PlannerMode enum (Xronos has no modal-reactor
construct; the translation strategy follows the preliminary port):

- MONITORING: pass a throttled WARNING to the driver, or arm actuation.
- WARNING: keep warning (throttled, at most ~1/s of logical time); de-escalate
  on NONE; arm actuation on ACTUATE.
- ACTUATE (armed): on the *next* decision that still says ACTUATE with the
  sign ahead, emit EmergencyBraking to the car and return to MONITORING.
  (Actuation therefore takes one extra coach cycle - reference behavior.)

The reaction is declared with a deadline: a planner that processes a decision
late is a timeliness bug worth surfacing, and ``ctx.is_before_deadline`` /
``ctx.lag`` show students the Xronos deadline API inside the real system
(examples/04_deadline_lag.py introduces it in isolation).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

import xronos

from ..messages import CoachDecision, CoachToken, DriverAction, PlannerMode

logger = logging.getLogger(__name__)

#: Processing deadline for reacting to a coach decision.
PLANNER_DEADLINE = timedelta(milliseconds=100)


class Planner(xronos.Reactor):
    decision_in = xronos.InputPortDeclaration[CoachDecision]()

    actuate = xronos.OutputPortDeclaration[DriverAction]()
    instruction = xronos.OutputPortDeclaration[str]()
    #: Mode after processing each decision (observability).
    mode_out = xronos.OutputPortDeclaration[PlannerMode]()

    def __init__(self, warning_throttle: timedelta) -> None:
        super().__init__()
        self._mode = PlannerMode.MONITORING
        self._throttle = warning_throttle
        self._last_warning_at = timedelta(seconds=-1_000_000)
        self._last_spoken = ""

    @property
    def mode(self) -> PlannerMode:
        return self._mode

    @xronos.reaction_with_deadline(deadline=PLANNER_DEADLINE)
    def decide(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        decision_trigger = ctx.add_trigger(self.decision_in)
        actuate_out = ctx.add_effect(self.actuate)
        instruction_out = ctx.add_effect(self.instruction)
        mode_out = ctx.add_effect(self.mode_out)

        def speak(token: CoachToken, message: str) -> None:
            if message and message != self._last_spoken:
                instruction_out.set(f"[VERBAL] {token.value} | {message}")
                self._last_spoken = message

        def handler() -> None:
            if not ctx.is_before_deadline:
                logger.warning(
                    "planner processed a decision %.1f ms late (deadline %s)",
                    ctx.lag.total_seconds() * 1000.0,
                    PLANNER_DEADLINE,
                )

            decision = decision_trigger.get()
            token = decision.token
            distance = decision.snapshot.distance_m
            now = ctx.elapsed_time

            if self._mode in (PlannerMode.MONITORING, PlannerMode.WARNING):
                if token is CoachToken.ACTUATE and distance > 0.0:
                    speak(token, decision.message)
                    self._mode = PlannerMode.ACTUATE
                elif token is CoachToken.WARNING:
                    if now - self._last_warning_at >= self._throttle:
                        speak(token, decision.message)
                        self._last_warning_at = now
                    self._mode = PlannerMode.WARNING
                elif token is CoachToken.NONE and self._mode is PlannerMode.WARNING:
                    self._mode = PlannerMode.MONITORING

            elif self._mode is PlannerMode.ACTUATE:
                if token is not CoachToken.ACTUATE or distance <= 0.0:
                    self._mode = PlannerMode.MONITORING
                else:
                    actuate_out.set(DriverAction.EMERGENCY_BRAKING)
                    instruction_out.set(
                        f"[VERBAL] {token.value} | Sent EmergencyBraking"
                    )
                    self._mode = PlannerMode.MONITORING

            mode_out.set(self._mode)

        return handler
