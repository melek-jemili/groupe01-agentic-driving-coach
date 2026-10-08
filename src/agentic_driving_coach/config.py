"""Configuration loading and validation.

Configuration precedence (highest wins):
    CLI flags  >  --config TOML file  >  packaged defaults (configs/*.toml)

All validation errors raise ConfigError with a message that names the field
and the offending value; the CLI turns these into exit code 2.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found]

from .policies import StopSignPolicyParams


class ConfigError(ValueError):
    """A configuration file or CLI value is invalid."""


def repo_root() -> Path:
    """Repository root when running from a checkout (src/agentic_driving_coach/ -> root)."""
    return Path(__file__).resolve().parent.parent.parent


def default_data_dir() -> Path:
    return repo_root() / "data"


def default_configs_dir() -> Path:
    return repo_root() / "configs"


DRIVER_LEVELS = ("beginner", "intermediate", "advanced")
COACH_BACKENDS = ("rule", "ollama", "replay")
SCENARIOS = ("stop-sign",)


@dataclass(frozen=True)
class ScenarioConfig:
    """Physical scenario parameters (defaults: the paper's stop-sign setup)."""

    name: str = "stop-sign"
    initial_distance_m: float = 100.0
    initial_velocity_mps: float = 10.0
    driver_period_ms: int = 100
    driver_to_car_delay_ms: int = 500
    coach_to_car_delay_ms: int = 200
    warning_throttle_s: float = 1.0
    max_duration_s: float = 30.0
    shutdown_grace_s: float = 1.0
    policy: StopSignPolicyParams = field(default_factory=StopSignPolicyParams)

    @property
    def dt_s(self) -> float:
        """Integration step: one driver period. Single source of truth."""
        return self.driver_period_ms / 1000.0

    def validate(self) -> None:
        if self.name not in SCENARIOS:
            raise ConfigError(f"unknown scenario {self.name!r}; supported: {SCENARIOS}")
        if self.initial_distance_m <= 0:
            raise ConfigError(f"initial_distance_m must be > 0, got {self.initial_distance_m}")
        if self.initial_velocity_mps < 0:
            raise ConfigError(f"initial_velocity_mps must be >= 0, got {self.initial_velocity_mps}")
        if self.driver_period_ms <= 0:
            raise ConfigError(f"driver_period_ms must be > 0, got {self.driver_period_ms}")
        for name in ("driver_to_car_delay_ms", "coach_to_car_delay_ms"):
            if getattr(self, name) < 0:
                raise ConfigError(f"{name} must be >= 0, got {getattr(self, name)}")
        if self.max_duration_s <= 0:
            raise ConfigError(f"max_duration_s must be > 0, got {self.max_duration_s}")


@dataclass(frozen=True)
class ModelConfig:
    """LLM configuration for the live Ollama backend.

    ``warmup_timeout_s`` bounds the explicit pre-load request issued before a
    live run starts. On shared filesystems (e.g. Sol scratch) a cold model
    load can take minutes - far beyond ``request_timeout_s`` - and Ollama
    aborts a load whose requesting client disconnects, so without a dedicated
    warm-up the model may never become resident on shared storage.
    ``keep_alive`` pins the model in server memory between requests and
    benchmark repetitions.
    """

    model: str = "llama3.2:3b"
    host: str | None = None  # None -> OLLAMA_HOST env var or client default
    deadline_ms: float = 300.0
    temperature: float = 0.0
    num_predict: int = 30
    seed: int = 42
    request_timeout_s: float = 30.0
    warmup_timeout_s: float = 300.0
    keep_alive: str = "30m"

    def resolved_host(self) -> str:
        return self.host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"

    def validate(self) -> None:
        if not self.model:
            raise ConfigError("model name must not be empty")
        if self.deadline_ms <= 0:
            raise ConfigError(f"deadline_ms must be > 0, got {self.deadline_ms}")
        if self.num_predict <= 0 or self.num_predict > 200:
            raise ConfigError(f"num_predict must be in (0, 200], got {self.num_predict}")
        if self.request_timeout_s <= 0:
            raise ConfigError(f"request_timeout_s must be > 0, got {self.request_timeout_s}")
        if self.warmup_timeout_s <= 0:
            raise ConfigError(f"warmup_timeout_s must be > 0, got {self.warmup_timeout_s}")
        if not self.keep_alive:
            raise ConfigError("keep_alive must not be empty (e.g. '30m')")


@dataclass(frozen=True)
class RunConfig:
    """Everything one simulation run needs."""

    scenario: ScenarioConfig = field(default_factory=ScenarioConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    coach_backend: str = "rule"
    driver_level: str = "beginner"
    driver_trace_path: Path | None = None  # overrides driver_level if set
    replay_trace_path: Path | None = None  # required for the replay backend
    fast: bool = False
    output_dir: Path = Path("results/run")
    run_id: str = ""

    def validate(self) -> None:
        self.scenario.validate()
        self.model.validate()
        if self.coach_backend not in COACH_BACKENDS:
            raise ConfigError(
                f"unknown coach backend {self.coach_backend!r}; supported: {COACH_BACKENDS}"
            )
        if self.driver_trace_path is None and self.driver_level not in DRIVER_LEVELS:
            raise ConfigError(
                f"unknown driver level {self.driver_level!r}; supported: {DRIVER_LEVELS} "
                "(or pass an explicit trace file)"
            )
        if self.coach_backend == "replay":
            if self.replay_trace_path is None:
                raise ConfigError("the replay backend requires --trace <file.jsonl>")
            if not self.replay_trace_path.exists():
                raise ConfigError(f"replay trace not found: {self.replay_trace_path}")
        if self.coach_backend == "ollama" and self.fast:
            raise ConfigError(
                "--fast cannot be combined with the live ollama backend: fast mode "
                "decouples logical time from wall-clock time, so wall-clock inference "
                "latencies and deadlines would be meaningless. Use --coach rule or "
                "--coach replay with --fast."
            )

    def driver_trace_file(self) -> Path:
        if self.driver_trace_path is not None:
            path = self.driver_trace_path
        else:
            path = default_data_dir() / "driver" / f"{self.driver_level}.txt"
        if not path.exists():
            raise ConfigError(f"driver behavior trace not found: {path}")
        return path


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _build_dataclass(cls: type, data: dict[str, Any], context: str) -> Any:
    """Construct a (frozen) dataclass from a TOML table, rejecting unknown keys."""
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown key(s) {sorted(unknown)} in [{context}]")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name == "policy" and isinstance(value, dict):
            value = _build_dataclass(StopSignPolicyParams, _tuplify(value), f"{context}.policy")
        kwargs[f.name] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"invalid [{context}] section: {exc}") from exc


def _tuplify(data: dict[str, Any]) -> dict[str, Any]:
    """TOML arrays arrive as lists; frozen dataclass fields want tuples."""
    return {k: tuple(v) if isinstance(v, list) else v for k, v in data.items()}


def load_scenario_config(path: Path | None = None) -> ScenarioConfig:
    """Load [scenario] (+ [scenario.policy]) from a TOML file."""
    if path is None:
        path = default_configs_dir() / "stop_sign.toml"
    doc = _read_toml(path)
    table = doc.get("scenario", {})
    cfg: ScenarioConfig = _build_dataclass(ScenarioConfig, table, "scenario")
    cfg.validate()
    return cfg


def load_model_config(path: Path | None = None, model_key: str | None = None) -> ModelConfig:
    """Load [model] from a TOML file.

    ``models.toml`` may also carry per-model overrides in [models."name"]
    tables; ``model_key`` selects one and merges it over [model].
    """
    if path is None:
        path = default_configs_dir() / "models.toml"
    doc = _read_toml(path)
    table = dict(doc.get("model", {}))
    if model_key is not None:
        overrides = doc.get("models", {}).get(model_key)
        if overrides is not None:
            table.update(overrides)
        table["model"] = model_key
    cfg: ModelConfig = _build_dataclass(ModelConfig, table, "model")
    cfg.validate()
    return cfg


def apply_overrides(cfg: RunConfig, **overrides: Any) -> RunConfig:
    """Return a copy of cfg with non-None override values applied.

    Nested keys use dotted names, e.g. ``model.deadline_ms`` or
    ``scenario.initial_velocity_mps``.
    """
    updates: dict[str, Any] = {}
    nested: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        if "." in key:
            section, name = key.split(".", 1)
            nested.setdefault(section, {})[name] = value
        else:
            updates[key] = value
    for section, values in nested.items():
        updates[section] = replace(getattr(cfg, section), **values)
    return replace(cfg, **updates)
