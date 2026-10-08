"""Coach backend interface.

A backend answers inference requests in one of two delivery modes:

- **synchronous with a modeled latency** (`is_async == False`): ``compute()``
  returns an InferenceResult whose ``latency_ms`` describes when the answer
  becomes available. The inference reactor delivers it by scheduling a
  ProgrammableTimer that far into the logical future, so rule and replay
  backends are fully deterministic and safe in ``fast=True`` runs.

- **asynchronous** (`is_async == True`): ``submit()`` hands the request to a
  worker; on completion the worker calls ``deliver(result)``, which the
  inference reactor wires to a thread-safe Xronos PhysicalEvent. This is the
  live Ollama path; it must never be used with ``fast=True``.

Backends never touch ports or reactor state: all system state transitions
happen inside Xronos reaction handlers in the inference reactor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from ..messages import InferenceRequest, InferenceResult


class InferenceBackend(ABC):
    """Interface implemented by the rule, replay, and ollama backends."""

    #: Short name recorded in logs/manifests ("rule", "replay", "ollama").
    name: str = "base"

    #: Model identifier recorded in logs (e.g. "llama3.2:3b", "rule-policy").
    model_label: str = "n/a"

    is_async: bool = False

    def compute(self, request: InferenceRequest) -> InferenceResult:
        """Synchronous-mode answer (rule/replay). Must be cheap and pure."""
        raise NotImplementedError

    def submit(
        self,
        request: InferenceRequest,
        deliver: Callable[[InferenceResult], None],
    ) -> None:
        """Asynchronous-mode answer. ``deliver`` is thread-safe."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Release worker threads/connections. Idempotent."""
