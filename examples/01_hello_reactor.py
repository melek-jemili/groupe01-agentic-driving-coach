"""Warm-up 1: a reactor with startup and shutdown reactions.

Concepts
--------
- A reactor is a class deriving from ``xronos.Reactor``.
- A *reaction* is a method decorated with ``@xronos.reaction``. It receives a
  ``xronos.ReactionContext`` (``ctx``), declares what triggers it with
  ``ctx.add_trigger(...)``, and returns the handler function that runs each
  time it is triggered.
- ``self.startup`` fires once when execution starts; ``self.shutdown`` fires
  once right before the program ends.

Run:  python examples/01_hello_reactor.py
Expect two lines: hello at startup, goodbye at shutdown.

Note the shutdown happens at logical t=0, *not* at the 500 ms timeout: this
program schedules no further events, and a Xronos program terminates as soon
as its event queue is empty. The timeout is an upper bound on logical time,
not a wait. (Warm-up 2 adds a timer, so there the timeout does decide.)
"""

from collections.abc import Callable
from datetime import timedelta

import xronos


class Greeter(xronos.Reactor):
    @xronos.reaction
    def on_startup(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self.startup)
        return lambda: print(f"[{self.name}] hello - execution has started")

    @xronos.reaction
    def on_shutdown(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self.shutdown)

        def handler() -> None:
            elapsed_ms = ctx.elapsed_time.total_seconds() * 1000.0
            print(f"[{self.name}] goodbye - shutting down at logical t={elapsed_ms:.0f} ms")

        return handler


def main() -> None:
    # timeout is an upper bound on logical time; this program ends earlier,
    # as soon as no events remain (see the module docstring).
    env = xronos.Environment(timeout=timedelta(milliseconds=500))
    env.create_reactor("greeter", Greeter)
    env.execute()


if __name__ == "__main__":
    main()
