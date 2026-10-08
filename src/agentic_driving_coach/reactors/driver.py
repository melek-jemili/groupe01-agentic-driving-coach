"""Driver reactor: replays a recorded human behavior trace.

Adapted from the Driver reactor in StopSign.lf (agentic-driving-coach): a
periodic timer emits one action per tick (default every 100 ms of logical
time). Actions 1-4 go out on ``accelerator``, 5-6 on ``brake``. Coaching
instructions arriving on ``instruction`` are printed and forwarded to the
recorder by the scenario wiring.

The trace itself is injected as a list (file I/O stays in ``scenario.py``),
which keeps this reactor trivially testable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import timedelta

import xronos

from ..messages import DriverAction

logger = logging.getLogger(__name__)


def load_behavior_trace(lines: Sequence[str], source: str = "<trace>") -> list[DriverAction]:
    """Parse behavior-file lines (one integer 1-6 per line) into actions."""
    actions: list[DriverAction] = []
    for lineno, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            actions.append(DriverAction(int(text)))
        except ValueError as exc:
            raise ValueError(
                f"{source}:{lineno}: invalid driver action {text!r} (expected 1-6)"
            ) from exc
    if not actions:
        raise ValueError(f"{source}: behavior trace is empty")
    return actions


class Driver(xronos.Reactor):
    accelerator = xronos.OutputPortDeclaration[DriverAction]()
    brake = xronos.OutputPortDeclaration[DriverAction]()
    #: Pure event emitted once, at the tick after the last trace action.
    finished = xronos.OutputPortDeclaration[bool]()

    instruction = xronos.InputPortDeclaration[str]()

    _tick = xronos.PeriodicTimerDeclaration()

    def __init__(self, actions: Sequence[DriverAction], period: timedelta) -> None:
        super().__init__()
        self._tick.period = period
        self._actions = list(actions)
        self._index = 0
        self._announced_done = False

    @xronos.reaction
    def emit_action(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._tick)
        accelerator = ctx.add_effect(self.accelerator)
        brake = ctx.add_effect(self.brake)
        finished = ctx.add_effect(self.finished)

        def handler() -> None:
            if self._index >= len(self._actions):
                if not self._announced_done:
                    self._announced_done = True
                    finished.set(True)
                return
            action = self._actions[self._index]
            self._index += 1
            if action.is_brake:
                brake.set(action)
            else:
                accelerator.set(action)

        return handler

    @xronos.reaction
    def hear_instruction(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        instruction = ctx.add_trigger(self.instruction)

        def handler() -> None:
            logger.info(
                "[t=%7.1f ms] driver hears: %s",
                ctx.elapsed_time.total_seconds() * 1000.0,
                instruction.get(),
            )

        return handler
