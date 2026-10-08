"""Warm-up 4: deadlines, lag, and slack - where logical meets wall-clock time.

Concepts
--------
- ``lag = wall-clock time - logical time of the event being processed``.
  A healthy real-time system keeps lag near zero; a handler that computes
  for a long time pushes lag up for everything scheduled after it.
- ``@xronos.reaction_with_deadline(deadline=...)`` declares that the handler
  should finish within ``deadline`` of the event's logical timestamp.
  Inside the handler, ``ctx.slack`` is the wall-clock margin left before the
  deadline and ``ctx.is_before_deadline`` says whether any margin remains.
- The slow reactor below busy-works ~80 ms against a 50 ms deadline: watch
  slack go negative in the second print. The fast timer's lag also grows
  whenever it fires right after a slow handler has hogged the runtime.

Why not ``fast=True`` here: fast mode does not wait between events, so
logical time races ahead of the wall clock and lag/slack/deadlines lose
their meaning. That is exactly why the lab forbids ``--fast`` with the live
LLM coach - wall-clock inference latency cannot be compared against a
logical deadline that no longer tracks wall-clock time.

Run:  python examples/04_deadline_lag.py
"""

import time
from collections.abc import Callable
from datetime import timedelta

import xronos


class FastMonitor(xronos.Reactor):
    """A light periodic task that reports its own lag."""

    _tick = xronos.PeriodicTimerDeclaration(period=timedelta(milliseconds=100))

    @xronos.reaction
    def report(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._tick)

        def handler() -> None:
            elapsed_ms = ctx.elapsed_time.total_seconds() * 1000.0
            lag_ms = ctx.lag.total_seconds() * 1000.0
            note = "  <-- delayed by the slow handler" if lag_ms > 20.0 else ""
            print(f"[monitor] t={elapsed_ms:6.1f} ms  lag={lag_ms:7.2f} ms{note}")

        return handler


class SlowWorker(xronos.Reactor):
    """Fires every 400 ms and violates its 50 ms deadline on purpose."""

    _tick = xronos.PeriodicTimerDeclaration(
        period=timedelta(milliseconds=400), offset=timedelta(milliseconds=150)
    )

    @xronos.reaction_with_deadline(deadline=timedelta(milliseconds=50))
    def crunch(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._tick)

        def handler() -> None:
            print(
                f"[worker]  start: slack={ctx.slack.total_seconds() * 1000.0:7.2f} ms "
                f"before_deadline={ctx.is_before_deadline}"
            )
            busy_until = time.perf_counter() + 0.080  # ~80 ms of "inference"
            while time.perf_counter() < busy_until:
                pass
            print(
                f"[worker]  end:   slack={ctx.slack.total_seconds() * 1000.0:7.2f} ms "
                f"before_deadline={ctx.is_before_deadline}  "
                f"lag={ctx.lag.total_seconds() * 1000.0:.2f} ms"
            )

        return handler


def main() -> None:
    env = xronos.Environment(timeout=timedelta(milliseconds=1000))
    env.create_reactor("monitor", FastMonitor)
    env.create_reactor("worker", SlowWorker)
    env.execute()


if __name__ == "__main__":
    main()
