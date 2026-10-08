"""OllamaCoach backend: a small local LLM behind a one-worker executor.

Design:

- ``submit()`` hands the blocking HTTP call to a **single-worker**
  ThreadPoolExecutor so the Xronos runtime is never blocked, and at most one
  model request is in flight at a time. The inference reactor refuses to
  submit while the worker is busy, so no request queue can build up.
- The worker measures wall-clock latency, validates the strict
  ``TOKEN|Message`` contract, and calls ``deliver(result)`` - which the
  inference reactor wires to a thread-safe Xronos ``PhysicalEvent``.
- Deadlines are *not* handled here: the inference reactor races this
  response against a ``ProgrammableTimer``. A response that loses the race is
  discarded by request-id bookkeeping; it can never overwrite the fallback.

Determinism note: the LLM itself is not deterministic (and its latency never
is). We request temperature 0, a fixed seed, and <= 30 tokens to make outputs
as repeatable as practical, and we record every raw response and latency to
``trace.jsonl`` so a run can be reproduced exactly with the replay backend.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from ..config import ModelConfig
from ..messages import CoachToken, InferenceRequest, InferenceResult
from ..parsing import parse_coach_output
from ..prompts import build_messages
from .base import InferenceBackend

logger = logging.getLogger(__name__)


class OllamaUnavailableError(RuntimeError):
    """The ollama client library or server is not available."""


class OllamaBackend(InferenceBackend):
    name = "ollama"
    is_async = True

    def __init__(self, model_config: ModelConfig) -> None:
        try:
            import ollama
        except ImportError as exc:  # pragma: no cover - import guard
            raise OllamaUnavailableError(
                "the 'ollama' Python package is not installed; "
                "run: pip install -e . (or pip install ollama)"
            ) from exc

        self._config = model_config
        self.model_label = model_config.model
        self._client = ollama.Client(
            host=model_config.resolved_host(), timeout=model_config.request_timeout_s
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ollama")

    def check_server(self) -> list[str]:
        """Return available model names; raise OllamaUnavailableError if down."""
        try:
            listing = self._client.list()
        except Exception as exc:
            raise OllamaUnavailableError(
                f"cannot reach Ollama at {self._config.resolved_host()}: {exc}"
            ) from exc
        return [m.model for m in listing.models if m.model is not None]

    def warm_up(self) -> float:
        """Pre-load the model into server memory; return elapsed seconds.

        Cold-loading a model from a shared filesystem can far exceed
        ``request_timeout_s``, and Ollama aborts a load when the requesting
        client disconnects. This dedicated call therefore uses the longer
        ``warmup_timeout_s``, generates a single token, and pins the model
        with ``keep_alive``. It runs before the reactor graph executes, so
        load time is never measured as inference latency.
        """
        import ollama

        warm_client = ollama.Client(
            host=self._config.resolved_host(), timeout=self._config.warmup_timeout_s
        )
        t0 = time.perf_counter()
        try:
            warm_client.chat(
                model=self._config.model,
                messages=[{"role": "user", "content": "ready?"}],
                options={"temperature": 0.0, "num_predict": 1},
                keep_alive=self._config.keep_alive,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            raise OllamaUnavailableError(
                f"warm-up of {self._config.model!r} failed after {elapsed:.0f} s "
                f"(warmup_timeout_s={self._config.warmup_timeout_s:.0f}): {exc}. "
                "Cold loads from shared filesystems can take minutes; raise "
                "--warmup-timeout-s (or warmup_timeout_s in configs/models.toml) "
                "and retry."
            ) from exc
        return time.perf_counter() - t0

    def probe(self) -> dict[str, float | None]:
        """One representative coach request, with the server's own timing
        breakdown (milliseconds). Used by ``doctor --warm`` to show where
        request time goes - decisive when latency looks anomalous:

        - large ``prompt_ms``/``generate_ms`` with a low ``tokens_per_s``
          points at CPU-fallback serving (check ``ollama ps``);
        - ``client_ms`` far above ``server_total_ms`` points at transport /
          client-side overhead;
        - large ``load_ms`` means the model was not resident (warm-up issue).
        """
        from ..messages import StateSnapshot
        from ..prompts import build_messages

        snapshot = StateSnapshot(distance_m=55.0, velocity_mps=12.0)
        t0 = time.perf_counter()
        try:
            response = self._client.chat(
                model=self._config.model,
                messages=build_messages(snapshot),
                options={
                    "temperature": self._config.temperature,
                    "num_predict": self._config.num_predict,
                    "seed": self._config.seed,
                },
                keep_alive=self._config.keep_alive,
            )
        except Exception as exc:
            raise OllamaUnavailableError(
                f"probe request to {self._config.model!r} failed: {exc}"
            ) from exc
        client_ms = (time.perf_counter() - t0) * 1000.0

        def ns_to_ms(value: int | None) -> float | None:
            return value / 1e6 if value is not None else None

        eval_ms = ns_to_ms(response.eval_duration)
        eval_count = response.eval_count
        tokens_per_s: float | None = None
        if eval_ms and eval_count:
            tokens_per_s = eval_count / (eval_ms / 1000.0)
        return {
            "client_ms": client_ms,
            "server_total_ms": ns_to_ms(response.total_duration),
            "load_ms": ns_to_ms(response.load_duration),
            "prompt_ms": ns_to_ms(response.prompt_eval_duration),
            "prompt_tokens": float(response.prompt_eval_count or 0),
            "generate_ms": eval_ms,
            "generate_tokens": float(eval_count or 0),
            "tokens_per_s": tokens_per_s,
        }

    def submit(
        self,
        request: InferenceRequest,
        deliver: Callable[[InferenceResult], None],
    ) -> None:
        self._executor.submit(self._infer, request, deliver)

    def _infer(
        self, request: InferenceRequest, deliver: Callable[[InferenceResult], None]
    ) -> None:
        """Runs on the worker thread. Never raises: errors become results."""
        t0 = time.perf_counter()
        raw = ""
        error: str | None = None
        try:
            response = self._client.chat(
                model=self._config.model,
                messages=build_messages(request.snapshot),
                options={
                    "temperature": self._config.temperature,
                    "num_predict": self._config.num_predict,
                    "seed": self._config.seed,
                },
                keep_alive=self._config.keep_alive,
            )
            raw = (response.message.content or "").strip()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("inference request %d failed: %s", request.request_id, error)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        parsed = parse_coach_output(raw) if error is None else None
        if parsed is not None and not parsed.malformed and parsed.token is not None:
            result = InferenceResult(
                request_id=request.request_id,
                raw_response=raw,
                token=parsed.token,
                message=parsed.message,
                latency_ms=latency_ms,
            )
        else:
            # Malformed or errored: the inference reactor substitutes the
            # deterministic fallback; the raw response is preserved for logs.
            result = InferenceResult(
                request_id=request.request_id,
                raw_response=raw,
                token=CoachToken.NONE,
                message="",
                latency_ms=latency_ms,
                malformed=parsed.malformed if parsed is not None else False,
                error=error,
            )
        deliver(result)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
