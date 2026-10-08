"""Warm-up 5: the retroactive-fallback anti-pattern - and the race that replaces it.

This example runs the same tiny system twice, in real time (never with
``fast=True`` - lag is a wall-clock quantity):

Phase A - AFTER-THE-FACT deadline check (the anti-pattern)
    The coach calls a fake "model" *inside its reaction handler* (a 1.2 s
    sleep standing in for an HTTP call to an LLM server), then checks a
    400 ms deadline after the call returns. Watch two things:

    1. The "fallback" is announced ~0.8 s after the deadline it was supposed
       to enforce. This is the fundamental problem, and no runtime can fix
       it: the deadline check sits *after* the sleep in program order,
       reactions of one reactor are serialized on its state, and a running
       handler is never preempted. The fallback is applied retroactively,
       to a moment that has already passed. Retroactive safety is fiction.
    2. On this SDK's scheduler you will also see the bystander monitor's
       lag explode past 1000 ms: while the handler blocks, everything
       stalls. That part is a scheduler property, not a law - a parallel
       runtime can shield unrelated reactors from a blocking handler. What
       it cannot do is move the blocked coach's own fallback decision
       earlier than the sleep that precedes it.

Phase B - the DEADLINE RACE (the lab's design, see reactors/coach.py)
    The handler returns immediately. The response is delivered as a
    *scheduled event* at its modeled latency (a ProgrammableTimer here; the
    live system uses a PhysicalEvent fed by a worker thread), and it races a
    second ProgrammableTimer scheduled at the deadline. The deadline event
    wins, the fallback fires at logically exactly +400 ms while the
    monitor's lag stays near zero, and the late response is discarded when
    it finally arrives.

Same "model", same latency, same deadline - the only difference is *where
the waiting happens*: inside the handler that owes the answer (so the
deadline can only be checked after the fact) or in the event queue (so the
deadline is an event that can win). This is why the lab never waits inside a
handler that owes a timely answer. Waiting in a
handler whose output nobody is waiting on with a clock is merely a
throughput question - and one a runtime may well handle gracefully.

Run:  python examples/05_retroactive_fallback.py
"""

import time
from collections.abc import Callable
from datetime import timedelta

import xronos

#: The fake model's latency and the deadline it is raced against.
MODEL_LATENCY = timedelta(milliseconds=1200)
DEADLINE = timedelta(milliseconds=400)
#: The one inference opportunity fires at this logical time.
REQUEST_AT = timedelta(milliseconds=200)


class FastMonitor(xronos.Reactor):
    """A light 100 ms task standing in for 'the rest of the system'
    (driver ticks, car physics, ...). Its lag shows whether the runtime
    is keeping up with the wall clock."""

    _tick = xronos.PeriodicTimerDeclaration(period=timedelta(milliseconds=100))

    @xronos.reaction
    def report(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._tick)

        def handler() -> None:
            elapsed_ms = ctx.elapsed_time.total_seconds() * 1000.0
            lag_ms = ctx.lag.total_seconds() * 1000.0
            note = "  <-- the world is stalled" if lag_ms > 50.0 else ""
            print(f"  [monitor] t={elapsed_ms:6.1f} ms  lag={lag_ms:7.1f} ms{note}")

        return handler


class BlockingCoach(xronos.Reactor):
    """THE ANTI-PATTERN. Not because it blocks - because it owes an answer
    by a deadline and can only check that deadline after the wait ends."""

    _request = xronos.PeriodicTimerDeclaration(
        period=timedelta(seconds=60), offset=REQUEST_AT  # fires once per run
    )

    @xronos.reaction
    def infer(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._request)

        def handler() -> None:
            t_request_ms = ctx.elapsed_time.total_seconds() * 1000.0
            print(
                f"  [coach]   t={t_request_ms:6.1f} ms  calling model inline "
                f"(this handler now sleeps {MODEL_LATENCY.total_seconds():.1f} s)..."
            )
            t0 = time.perf_counter()
            time.sleep(MODEL_LATENCY.total_seconds())  # "ollama.chat(...)"
            elapsed = timedelta(seconds=time.perf_counter() - t0)
            # The after-the-fact deadline check - too late to matter:
            if elapsed > DEADLINE:
                lag_ms = ctx.lag.total_seconds() * 1000.0
                late_ms = (elapsed - DEADLINE).total_seconds() * 1000.0
                print(
                    f"  [coach]   t={t_request_ms:6.1f} ms  \"fallback\" engaged "
                    f"{late_ms:.0f} ms AFTER the deadline it was meant to enforce "
                    f"(handler lag now {lag_ms:.0f} ms) - retroactive, protects nothing"
                )

        return handler


class NonBlockingCoach(xronos.Reactor):
    """The lab's pattern: the response and the deadline are both *events*;
    whichever is processed first resolves the request (reactors/coach.py)."""

    _request = xronos.PeriodicTimerDeclaration(
        period=timedelta(seconds=60), offset=REQUEST_AT
    )
    _response = xronos.ProgrammableTimerDeclaration[str]()
    _deadline = xronos.ProgrammableTimerDeclaration[None]()

    def __init__(self) -> None:
        super().__init__()
        self._resolved = False

    @xronos.reaction
    def infer(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._request)
        response = ctx.add_effect(self._response)
        deadline = ctx.add_effect(self._deadline)

        def handler() -> None:
            t_ms = ctx.elapsed_time.total_seconds() * 1000.0
            print(
                f"  [coach]   t={t_ms:6.1f} ms  request submitted; handler returns "
                "immediately - the runtime keeps running"
            )
            # Modeled response (the live system: worker thread -> PhysicalEvent):
            response.schedule("SLOW DOWN", MODEL_LATENCY)
            # The deadline it races against:
            deadline.schedule(None, DEADLINE)

        return handler

    @xronos.reaction
    def on_deadline(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        ctx.add_trigger(self._deadline)

        def handler() -> None:
            if self._resolved:
                return  # response won the race; nothing to do
            self._resolved = True
            t_ms = ctx.elapsed_time.total_seconds() * 1000.0
            lag_ms = ctx.lag.total_seconds() * 1000.0
            print(
                f"  [coach]   t={t_ms:6.1f} ms  deadline: fallback issued ON TIME "
                f"(lag {lag_ms:.1f} ms) - the decision protects the moment it belongs to"
            )

        return handler

    @xronos.reaction
    def on_response(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        response = ctx.add_trigger(self._response)

        def handler() -> None:
            t_ms = ctx.elapsed_time.total_seconds() * 1000.0
            if self._resolved:
                print(
                    f"  [coach]   t={t_ms:6.1f} ms  late response "
                    f"{response.get()!r} discarded (fallback already stands)"
                )
            else:
                self._resolved = True
                print(
                    f"  [coach]   t={t_ms:6.1f} ms  response {response.get()!r} "
                    "beat the deadline"
                )

        return handler


def run_phase(title: str, coach_class: type[xronos.Reactor]) -> None:
    print(f"\n=== {title} ===")
    env = xronos.Environment(timeout=timedelta(milliseconds=1800))
    env.create_reactor("monitor", FastMonitor)
    env.create_reactor("coach", coach_class)
    env.execute()


def main() -> None:
    print(
        "One inference opportunity at t=200 ms; model latency 1200 ms; "
        "deadline 400 ms."
    )
    run_phase("Phase A: after-the-fact deadline check (ANTI-PATTERN)", BlockingCoach)
    run_phase(
        "Phase B: response and deadline as racing events (the lab's design)",
        NonBlockingCoach,
    )
    print(
        "\nSame model, same latency, same deadline. Compare the monitor's lag "
        "and\nWHEN each phase's fallback fired relative to t=600 ms "
        "(request + deadline)."
    )


if __name__ == "__main__":
    main()
