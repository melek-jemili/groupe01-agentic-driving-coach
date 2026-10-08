"""Coach reactors: hierarchical Coach = InferenceReactor + Planner.

The InferenceReactor implements the deadline-aware, non-blocking inference
protocol:

1. A state snapshot (distance, velocity) arriving on the input ports becomes
   an InferenceRequest with a monotonically increasing ``request_id`` -
   unless a request is already outstanding, in which case the opportunity is
   *skipped* and counted (no queue can build up).
2. The request goes to the backend:
   - sync backends (rule, replay) return a result with a modeled latency;
     the result is delivered through a ProgrammableTimer scheduled that far
     into the logical future (deterministic, fast-mode safe);
   - the async backend (ollama) runs on a one-worker executor and delivers
     through a thread-safe PhysicalEvent (real time only).
3. A ProgrammableTimer is scheduled at the configured deadline. Whichever
   event arrives first at the reactor wins; ``request_id`` bookkeeping makes
   the race safe:
   - deadline first -> emit the deterministic fallback decision, record a
     deadline miss; a response arriving later is *discarded* (recorded, never
     emitted) - a late response can never overwrite an issued fallback;
   - response first -> emit the validated result; the deadline event finds
     the request already completed and does nothing.
4. Malformed or errored responses never become decisions: the deterministic
   fallback is emitted instead, with the raw response preserved for logging.

Every accepted request produces exactly one CoachDecision on ``decision``.
All port writes and state transitions happen inside reaction handlers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

import xronos

from ..backends.base import InferenceBackend
from ..messages import (
    CoachDecision,
    CoachToken,
    DecisionSource,
    DriverAction,
    InferenceRequest,
    InferenceResult,
    InferenceStats,
    PlannerMode,
    StateSnapshot,
)
from ..policies import StopSignPolicyParams, stop_sign_policy
from .planner import Planner

logger = logging.getLogger(__name__)


class InferenceReactor(xronos.Reactor):
    """Deterministic inference reactor for synchronous backends (rule, replay).

    Note there is deliberately **no** PhysicalEventDeclaration here: declaring
    one tells the Xronos runtime that events may arrive from outside at
    wall-clock times, so the runtime paces execution against the wall clock
    even under ``fast=True``. Keeping the physical event out of this class is
    what makes rule/replay runs finish in milliseconds; the live path adds it
    in LiveInferenceReactor below.
    """

    distance_in = xronos.InputPortDeclaration[float]()
    velocity_in = xronos.InputPortDeclaration[float]()

    decision = xronos.OutputPortDeclaration[CoachDecision]()
    #: Late model responses, emitted when discarded (observability only).
    late_response = xronos.OutputPortDeclaration[InferenceResult]()
    #: Skipped inference opportunities (value: total skipped so far).
    skipped = xronos.OutputPortDeclaration[int]()

    _scheduled_response = xronos.ProgrammableTimerDeclaration[InferenceResult]()
    _deadline_timer = xronos.ProgrammableTimerDeclaration[int]()

    def __init__(
        self,
        backend: InferenceBackend,
        deadline: timedelta,
        policy_params: StopSignPolicyParams,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._deadline = deadline
        self._policy_params = policy_params
        self.stats = InferenceStats()
        self._next_request_id = 0
        self._pending: dict[int, InferenceRequest] = {}
        self._completed: set[int] = set()
        self._worker_busy = False

    # ------------------------------------------------------------------
    # 1. Snapshot arrives: create a request or skip the opportunity.
    # ------------------------------------------------------------------
    @xronos.reaction
    def on_snapshot(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        distance = ctx.add_trigger(self.distance_in)
        velocity = ctx.add_trigger(self.velocity_in)
        decision_out = ctx.add_effect(self.decision)
        skipped_out = ctx.add_effect(self.skipped)
        scheduled_out = ctx.add_effect(self._scheduled_response)
        deadline_out = ctx.add_effect(self._deadline_timer)

        def handler() -> None:
            if not (distance.is_present() and velocity.is_present()):
                return  # both arrive at the same tag by construction
            snapshot = StateSnapshot(distance.get(), velocity.get())

            # Past the sign: no inference needed; answer NONE immediately so
            # the planner can leave the ACTUATE mode (as in the original).
            if snapshot.distance_m <= 0.0:
                decision_out.set(
                    CoachDecision(
                        request_id=-1,
                        token=CoachToken.NONE,
                        message="",
                        source=DecisionSource.RULE,
                        snapshot=snapshot,
                        latency_ms=0.0,
                    )
                )
                return

            if self._pending or self._worker_busy:
                self.stats.opportunities_skipped += 1
                skipped_out.set(self.stats.opportunities_skipped)
                return

            request = InferenceRequest(
                request_id=self._next_request_id,
                snapshot=snapshot,
                logical_elapsed_ms=ctx.elapsed_time.total_seconds() * 1000.0,
            )
            self._next_request_id += 1
            self._pending[request.request_id] = request
            self.stats.requests += 1

            # The (real or modeled) worker is busy from submission until its
            # response arrives - even beyond the deadline. A deadline miss
            # resolves the *decision* early, but no new request can start
            # until the in-flight one actually finishes (live semantics,
            # reproduced exactly by replay).
            self._worker_busy = True
            if self._backend.is_async:
                self._submit_async(request)
            else:
                result = self._backend.compute(request)
                if result.latency_ms is not None:
                    scheduled_out.schedule(
                        result, timedelta(milliseconds=result.latency_ms)
                    )
                # latency None means the response was never observed in the
                # recorded run: the worker stays busy for the rest of the
                # run and only the deadline below resolves this request -
                # faithfully reproducing the recorded starvation.
            deadline_out.schedule(request.request_id, self._deadline)

        return handler

    def _submit_async(self, request: InferenceRequest) -> None:
        raise RuntimeError(
            "asynchronous backends require LiveInferenceReactor "
            "(InferenceReactor has no physical event to deliver responses)"
        )

    # ------------------------------------------------------------------
    # 2a. Deterministic (rule/replay) response at its modeled latency.
    # ------------------------------------------------------------------
    @xronos.reaction
    def on_scheduled_response(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        response = ctx.add_trigger(self._scheduled_response)
        decision_out = ctx.add_effect(self.decision)
        late_out = ctx.add_effect(self.late_response)

        def handler() -> None:
            self._worker_busy = False
            self._resolve_response(response.get(), ctx, decision_out, late_out)

        return handler

    # ------------------------------------------------------------------
    # 3. Deadline: if the request is still open, fall back deterministically.
    # ------------------------------------------------------------------
    @xronos.reaction
    def on_deadline(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        deadline_event = ctx.add_trigger(self._deadline_timer)
        decision_out = ctx.add_effect(self.decision)

        def handler() -> None:
            request_id = deadline_event.get()
            if request_id in self._completed:
                return  # the response won the race; nothing to do
            request = self._pending.pop(request_id)
            self._completed.add(request_id)
            self.stats.deadline_misses += 1
            self.stats.fallbacks += 1

            token, message = stop_sign_policy(
                request.snapshot.distance_m,
                request.snapshot.velocity_mps,
                self._policy_params,
            )
            logger.info(
                "[t=%7.1f ms] request %d missed the %.0f ms deadline -> fallback %s",
                ctx.elapsed_time.total_seconds() * 1000.0,
                request_id,
                self._deadline.total_seconds() * 1000.0,
                token.value,
            )
            decision_out.set(
                CoachDecision(
                    request_id=request_id,
                    token=token,
                    message=message,
                    source=DecisionSource.FALLBACK,
                    snapshot=request.snapshot,
                    latency_ms=self._deadline.total_seconds() * 1000.0,
                    deadline_miss=True,
                    fallback_used=True,
                )
            )

        return handler

    # ------------------------------------------------------------------
    # Shared response resolution (both delivery paths).
    # ------------------------------------------------------------------
    def _resolve_response(
        self,
        result: InferenceResult,
        ctx: xronos.ReactionContext,
        decision_out: xronos.PortEffect[CoachDecision],
        late_out: xronos.PortEffect[InferenceResult],
    ) -> None:
        request_id = result.request_id
        if result.latency_ms is not None:
            self.stats.latencies_ms.append(result.latency_ms)

        if request_id in self._completed:
            # The fallback already went out: record and discard, never emit.
            self.stats.late_responses_discarded += 1
            late_out.set(result)
            logger.info(
                "[t=%7.1f ms] late response for request %d discarded "
                "(latency %.1f ms > deadline %.0f ms)",
                ctx.elapsed_time.total_seconds() * 1000.0,
                request_id,
                result.latency_ms if result.latency_ms is not None else float("inf"),
                self._deadline.total_seconds() * 1000.0,
            )
            return

        request = self._pending.pop(request_id)
        self._completed.add(request_id)

        if result.error is not None or result.malformed:
            if result.malformed:
                self.stats.malformed += 1
            if result.error is not None:
                self.stats.errors += 1
            self.stats.fallbacks += 1
            token, message = stop_sign_policy(
                request.snapshot.distance_m,
                request.snapshot.velocity_mps,
                self._policy_params,
            )
            decision_out.set(
                CoachDecision(
                    request_id=request_id,
                    token=token,
                    message=message,
                    source=DecisionSource.FALLBACK,
                    snapshot=request.snapshot,
                    latency_ms=result.latency_ms if result.latency_ms is not None else 0.0,
                    fallback_used=True,
                    malformed_response=result.malformed,
                    raw_response=result.raw_response,
                    error=result.error,
                )
            )
            return

        source = (
            DecisionSource.RULE if self._backend.name == "rule" else DecisionSource.MODEL
        )
        decision_out.set(
            CoachDecision(
                request_id=request_id,
                token=result.token,
                message=result.message,
                source=source,
                snapshot=request.snapshot,
                latency_ms=result.latency_ms if result.latency_ms is not None else 0.0,
                raw_response=result.raw_response,
            )
        )


class LiveInferenceReactor(InferenceReactor):
    """Inference reactor for asynchronous backends (live Ollama).

    Adds the thread-safe PhysicalEvent through which the one-worker executor
    delivers model responses. Declaring the physical event makes the runtime
    pace logical time against the wall clock - which is exactly right for
    live inference, and exactly why this class must not be used with
    ``fast=True`` (the CLI rejects that combination).
    """

    _model_response = xronos.PhysicalEventDeclaration[InferenceResult]()

    def _submit_async(self, request: InferenceRequest) -> None:
        self._backend.submit(request, self._model_response.trigger)

    # ------------------------------------------------------------------
    # 2b. Live model response, delivered by the worker thread.
    # ------------------------------------------------------------------
    @xronos.reaction
    def on_model_response(self, ctx: xronos.ReactionContext) -> Callable[[], None]:
        response = ctx.add_trigger(self._model_response)
        decision_out = ctx.add_effect(self.decision)
        late_out = ctx.add_effect(self.late_response)

        def handler() -> None:
            self._worker_busy = False
            self._resolve_response(response.get(), ctx, decision_out, late_out)

        return handler


class Coach(xronos.Reactor):
    """Hierarchical coach: forwards state to inference, decisions to the planner.

    Mirrors the LF ``Coach`` reactor: contains an inference child and a
    planner child, and exposes observability ports for the recorder. The
    inference child is a LiveInferenceReactor for asynchronous backends and
    the deterministic InferenceReactor otherwise (see those docstrings).
    """

    distance_in = xronos.InputPortDeclaration[float]()
    velocity_in = xronos.InputPortDeclaration[float]()

    actuate = xronos.OutputPortDeclaration[DriverAction]()
    instruction = xronos.OutputPortDeclaration[str]()

    # Observability (recorder only)
    decision = xronos.OutputPortDeclaration[CoachDecision]()
    late_response = xronos.OutputPortDeclaration[InferenceResult]()
    skipped = xronos.OutputPortDeclaration[int]()
    planner_mode = xronos.OutputPortDeclaration[PlannerMode]()

    def __init__(
        self,
        backend: InferenceBackend,
        deadline: timedelta,
        policy_params: StopSignPolicyParams,
        warning_throttle: timedelta,
    ) -> None:
        super().__init__()
        inference_class = LiveInferenceReactor if backend.is_async else InferenceReactor
        self.inference = self.create_reactor(
            "inference", inference_class, backend, deadline, policy_params
        )
        self.planner = self.create_reactor(
            "planner", Planner, warning_throttle
        )

        self.connect(self.distance_in, self.inference.distance_in)
        self.connect(self.velocity_in, self.inference.velocity_in)

        self.connect(self.inference.decision, self.planner.decision_in)

        self.connect(self.planner.actuate, self.actuate)
        self.connect(self.planner.instruction, self.instruction)

        self.connect(self.inference.decision, self.decision)
        self.connect(self.inference.late_response, self.late_response)
        self.connect(self.inference.skipped, self.skipped)
        self.connect(self.planner.mode_out, self.planner_mode)
