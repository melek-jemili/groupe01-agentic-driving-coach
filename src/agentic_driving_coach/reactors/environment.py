"""Road environment reactor: tracks the remaining distance to the stop sign.

Preserved from the Environment reactor in StopSign.lf: on every velocity
update the remaining distance decreases by ``v * dt``; it clamps at 0 and
latches "passed", after which 0.0 is emitted (the original behavior).

Named RoadEnvironment to avoid confusion with ``xronos.Environment``, the
runtime executor.
"""

from __future__ import annotations

from collections.abc import Callable

import xronos


class RoadEnvironment(xronos.Reactor):
    velocity_in = xronos.InputPortDeclaration[float]()

    #: Remaining distance to the stop sign (m); 0.0 once passed.
    distance = xronos.OutputPortDeclaration[float]()

    def __init__(self, initial_distance: float, dt: float) -> None:
        super().__init__()
        self._distance = initial_distance
        self._dt = dt
        self._passed = False

    @property
    def passed(self) -> bool:
        return self._passed

    @xronos.reaction
    def advance(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        velocity = ctx.add_trigger(self.velocity_in)
        distance_out = ctx.add_effect(self.distance)

        def handler() -> None:
            if not self._passed:
                self._distance -= velocity.get() * self._dt
                if self._distance <= 0.0:
                    self._distance = 0.0
                    self._passed = True
                distance_out.set(self._distance)
            else:
                distance_out.set(0.0)

        return handler
