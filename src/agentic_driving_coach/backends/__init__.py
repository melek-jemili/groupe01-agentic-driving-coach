"""Coach inference backends: rule (deterministic), replay, and live Ollama."""

from .base import InferenceBackend
from .replay import ReplayBackend, ReplayTraceError, load_trace
from .rule import RuleBackend

__all__ = [
    "InferenceBackend",
    "ReplayBackend",
    "ReplayTraceError",
    "RuleBackend",
    "load_trace",
]
