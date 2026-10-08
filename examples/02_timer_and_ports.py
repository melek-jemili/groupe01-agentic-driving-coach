"""Warm-up 2: a periodic timer driving typed ports between two reactors.

Concepts
--------
- ``PeriodicTimerDeclaration`` emits an event every ``period`` (logical time).
- Ports are *typed* class attributes: ``OutputPortDeclaration[float]`` /
  ``InputPortDeclaration[float]``. ``env.connect(...)`` wires them.
- Inside a handler, ``trigger.get()`` reads the event value and
  ``effect.set(value)`` writes an output port.
- ``ctx.current_time`` is the logical clock as a wall-clock-anchored datetime;
  ``ctx.elapsed_time`` is the logical duration since startup. Both are frozen
  during one reaction: logical time does not advance while a handler runs.

Run:  python examples/02_timer_and_ports.py
Expect 5 samples at logical t = 0, 100, 200, 300, 400 ms, each received in
the same logical instant it was sent (no connection delay yet).
"""

from collections.abc import Callable
from datetime import timedelta

import xronos


class Sensor(xronos.Reactor):
    sample = xronos.OutputPortDeclaration[float]()
    _tick = xronos.PeriodicTimerDeclaration(period=timedelta(milliseconds=100))

    def __init__(self) -> None:
        super().__init__()
        self._reading = 20.0

    @xronos.reaction
    def measure(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._tick)
        sample_out = ctx.add_effect(self.sample)

        def handler() -> None:
            self._reading += 0.5
            sample_out.set(self._reading)

        return handler


class Display(xronos.Reactor):
    sample = xronos.InputPortDeclaration[float]()

    @xronos.reaction
    def show(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        sample_in = ctx.add_trigger(self.sample)

        def handler() -> None:
            elapsed_ms = ctx.elapsed_time.total_seconds() * 1000.0
            print(
                f"logical t={elapsed_ms:6.1f} ms  value={sample_in.get():.1f}  "
                f"(current_time={ctx.current_time.time()})"
            )

        return handler


def main() -> None:
    env = xronos.Environment(timeout=timedelta(milliseconds=450))
    sensor = env.create_reactor("sensor", Sensor)
    display = env.create_reactor("display", Display)
    env.connect(sensor.sample, display.sample)
    env.execute()


if __name__ == "__main__":
    main()
