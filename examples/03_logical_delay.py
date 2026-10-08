"""Warm-up 3: a connection with a logical delay.

Concepts
--------
- ``env.connect(a, b, delay=timedelta(...))`` delivers each event exactly
  ``delay`` later in *logical* time. This is how the lab models the 500 ms
  driver-to-car delay and the 200 ms coach actuation delay - no sleeps.
- The delay is exact by construction: an event sent at logical t arrives at
  logical t + delay, independent of how long any handler takes or how busy
  the machine is. Determinism lives in logical time.
- Try ``fast=True`` in main(): logical timestamps stay identical while the
  program finishes in milliseconds of wall-clock time.

Run:  python examples/03_logical_delay.py
Expect: each message sent at t is received at exactly t + 500.0 ms.
"""

from collections.abc import Callable
from datetime import timedelta

import xronos


class Commander(xronos.Reactor):
    command = xronos.OutputPortDeclaration[str]()
    _tick = xronos.PeriodicTimerDeclaration(period=timedelta(milliseconds=250))

    def __init__(self) -> None:
        super().__init__()
        self._count = 0

    @xronos.reaction
    def send(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._tick)
        command_out = ctx.add_effect(self.command)

        def handler() -> None:
            sent_ms = ctx.elapsed_time.total_seconds() * 1000.0
            command_out.set(f"command #{self._count} (sent at t={sent_ms:.1f} ms)")
            print(f"send    t={sent_ms:6.1f} ms  command #{self._count}")
            self._count += 1

        return handler


class Actuator(xronos.Reactor):
    command = xronos.InputPortDeclaration[str]()

    @xronos.reaction
    def receive(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        command_in = ctx.add_trigger(self.command)

        def handler() -> None:
            recv_ms = ctx.elapsed_time.total_seconds() * 1000.0
            print(f"receive t={recv_ms:6.1f} ms  {command_in.get()}")

        return handler


def main() -> None:
    env = xronos.Environment(timeout=timedelta(milliseconds=1300))
    commander = env.create_reactor("commander", Commander)
    actuator = env.create_reactor("actuator", Actuator)
    # The one line this example is about:
    env.connect(commander.command, actuator.command, delay=timedelta(milliseconds=500))
    env.execute()


if __name__ == "__main__":
    main()
