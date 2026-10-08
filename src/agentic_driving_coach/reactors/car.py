"""Car reactor: point-mass longitudinal dynamics.

Physics preserved from the Car reactor in StopSign.lf (agentic-driving-coach):
each incoming action maps to a net acceleration (messages.ACCELERATION_MAP),
integrated with a semi-implicit Euler step of ``dt`` seconds:

    v  <-  max(0, v + a * dt)
    x  <-  x + v * dt

A coach actuation command overrides the corresponding driver channel (brake
commands override the brake channel, acceleration commands the accelerator
channel), exactly as in the original.

Integration stays on the driver-tick grid: the car integrates one ``dt`` step
only at tags carrying a driver command. A coach actuation arriving *between*
ticks (a live model response is stamped with its wall-clock arrival, so its
actuation is generally off the 100 ms grid) is **latched** and applied as the
override at the next driver tick, instead of triggering an extra full-``dt``
integration step at the off-grid tag. Without the latch, every timely
model-sourced actuation would inject ~``dt`` of additional braking and
simulated time that on-grid fallback actuations do not get - a systematic
bias in exactly the timeliness-vs-outcome comparison this lab measures.
(Alternative considered: scale ``dt`` by the elapsed logical time since the
last step; rejected to preserve the paper's fixed-``dt`` point-mass model.
See docs/timing_and_deadlines.md.)
"""

from __future__ import annotations

from collections.abc import Callable

import xronos

from ..messages import ACCELERATION_MAP, DriverAction


def step_velocity(velocity: float, action: DriverAction | None, dt: float) -> float:
    """One integration step; the pure function the reactor (and tests) use."""
    acceleration = ACCELERATION_MAP[action] if action is not None else 0.0
    return max(0.0, velocity + acceleration * dt)


class Car(xronos.Reactor):
    accelerator_in = xronos.InputPortDeclaration[DriverAction]()
    brake_in = xronos.InputPortDeclaration[DriverAction]()
    actuate_in = xronos.InputPortDeclaration[DriverAction]()

    velocity = xronos.OutputPortDeclaration[float]()
    #: The action actually applied this step, after any coach override.
    applied_action = xronos.OutputPortDeclaration[DriverAction]()

    def __init__(self, initial_velocity: float, dt: float) -> None:
        super().__init__()
        self._velocity = initial_velocity
        self._distance_travelled = 0.0
        self._dt = dt
        #: Coach actuation latched for the next driver tick (module docstring).
        self._pending_override: DriverAction | None = None

    @property
    def distance_travelled(self) -> float:
        return self._distance_travelled

    @xronos.reaction
    def drive(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        accelerator = ctx.add_trigger(self.accelerator_in)
        brake = ctx.add_trigger(self.brake_in)
        actuate = ctx.add_trigger(self.actuate_in)
        velocity_out = ctx.add_effect(self.velocity)
        applied_out = ctx.add_effect(self.applied_action)

        def handler() -> None:
            if actuate.is_present():
                # Latch (last one wins); applied at the next integration step.
                self._pending_override = actuate.get()

            accel_cmd = accelerator.get() if accelerator.is_present() else None
            brake_cmd = brake.get() if brake.is_present() else None
            if accel_cmd is None and brake_cmd is None:
                return  # no driver tick at this tag: latch only, no physics step

            if self._pending_override is not None:
                override = self._pending_override
                self._pending_override = None
                if override.is_brake:
                    brake_cmd = override
                else:
                    accel_cmd = override

            # Brake input takes precedence over the accelerator, as in the
            # original if/elif chain.
            action = brake_cmd if brake_cmd is not None else accel_cmd

            self._velocity = step_velocity(self._velocity, action, self._dt)
            self._distance_travelled += self._velocity * self._dt

            velocity_out.set(self._velocity)
            if action is not None:
                applied_out.set(action)

        return handler
