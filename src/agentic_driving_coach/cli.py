"""Command-line interface.

Commands
--------
    doctor              environment/setup check (never downloads models)
    run                 one simulation run (rule / ollama / replay coach)
    benchmark-models    repeated runs across models, comparison artifacts
    compare-behaviors   repeated runs across driver levels
    plot                regenerate the overview PNG from a run.csv

Exit codes: 0 success; 2 configuration error; 3 environment/setup failure
(e.g. Ollama unreachable or model not pulled); 1 unexpected runtime error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .config import (
    DRIVER_LEVELS,
    ConfigError,
    RunConfig,
    apply_overrides,
    default_configs_dir,
    default_data_dir,
    load_model_config,
    load_scenario_config,
)

if TYPE_CHECKING:
    from .metrics import RunSummary

logger = logging.getLogger("agentic_driving_coach")

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_CONFIG = 2
EXIT_SETUP = 3


def _sanitize(name: str) -> str:
    return name.replace(":", "-").replace("/", "-")


def _add_common_run_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--scenario", default="stop-sign", help="scenario name (stop-sign)")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="scenario TOML (default: configs/stop_sign.toml)",
    )
    parser.add_argument(
        "--models-config",
        type=Path,
        default=None,
        help="model TOML (default: configs/models.toml)",
    )
    parser.add_argument("--model", default=None, help="Ollama model name (default from config)")
    parser.add_argument(
        "--host",
        default=None,
        help="Ollama host URL (default: $OLLAMA_HOST or http://127.0.0.1:11434)",
    )
    parser.add_argument("--deadline-ms", type=float, default=None, help="inference deadline (ms)")
    parser.add_argument(
        "--warmup-timeout-s",
        type=float,
        default=None,
        help="bound for the pre-run model warm-up (default 300; raise on slow "
        "shared filesystems)",
    )
    parser.add_argument("--seed", type=int, default=None, help="model sampling seed")
    parser.add_argument(
        "--initial-velocity", type=float, default=None, help="override initial velocity (m/s)"
    )
    parser.add_argument(
        "--sign-distance", type=float, default=None, help="override stop-sign distance (m)"
    )
    parser.add_argument(
        "--duration-s", type=float, default=None, help="hard simulation timeout (logical s)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic-driving-coach",
        description="Agentic Driving Coach using reactor models via Xronos",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="warnings and errors only")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check the environment; print fix commands")
    doctor.add_argument(
        "--live",
        action="store_true",
        help="also require a reachable Ollama server with the configured models",
    )
    doctor.add_argument("--host", default=None, help="Ollama host URL to check")
    doctor.add_argument(
        "--models",
        nargs="*",
        default=["llama3.2:3b", "llama3.2:1b"],
        help="models expected on the server",
    )
    doctor.add_argument(
        "--warm",
        action="store_true",
        help="also pre-load each present model and report its load time "
        "(the validated fix for slow cold loads on shared filesystems)",
    )
    doctor.add_argument(
        "--warmup-timeout-s",
        type=float,
        default=300.0,
        help="bound for each --warm pre-load (default 300)",
    )

    run = sub.add_parser("run", help="run one simulation")
    _add_common_run_args(run)
    run.add_argument("--driver", default="beginner", help=f"{'/'.join(DRIVER_LEVELS)} or a path")
    run.add_argument("--coach", default="rule", choices=("rule", "ollama", "replay"))
    run.add_argument("--trace", type=Path, default=None, help="replay trace (.jsonl)")
    run.add_argument(
        "--fast",
        action="store_true",
        help="run logical time as fast as possible (rule/replay only)",
    )
    run.add_argument("--output", type=Path, required=True, help="output directory")
    run.add_argument("--run-id", default=None, help="fixed run id (default: random)")
    run.add_argument(
        "--telemetry",
        action="store_true",
        help="OPTIONAL: export Xronos telemetry (needs a local dashboard; "
        "never required for the lab)",
    )
    run.add_argument("--telemetry-endpoint", default="localhost:4317")

    bench = sub.add_parser("benchmark-models", help="compare models over repeated runs")
    _add_common_run_args(bench)
    bench.add_argument("--models", nargs="+", required=True, help="model names to compare")
    bench.add_argument("--driver", default="beginner")
    bench.add_argument("--repetitions", type=int, default=3)
    bench.add_argument(
        "--coach",
        default="ollama",
        choices=("ollama", "rule", "replay"),
        help="backend (default ollama; rule is useful for offline dry runs)",
    )
    bench.add_argument("--trace", type=Path, default=None, help="replay trace (.jsonl)")
    bench.add_argument("--fast", action="store_true")
    bench.add_argument("--output", type=Path, required=True)

    comp = sub.add_parser("compare-behaviors", help="compare driver levels over repeated runs")
    _add_common_run_args(comp)
    comp.add_argument("--drivers", nargs="+", required=True)
    comp.add_argument("--repetitions", type=int, default=3)
    comp.add_argument("--coach", default="ollama", choices=("ollama", "rule", "replay"))
    comp.add_argument("--trace", type=Path, default=None)
    comp.add_argument("--fast", action="store_true")
    comp.add_argument("--output", type=Path, required=True)

    plot = sub.add_parser("plot", help="regenerate the overview PNG from a run.csv")
    plot.add_argument("csv", type=Path)
    plot.add_argument("--output", type=Path, default=None, help="output PNG path")

    return parser


def _build_run_config(args: argparse.Namespace, coach: str, driver: str) -> RunConfig:
    scenario = load_scenario_config(args.config)
    model = load_model_config(args.models_config, model_key=args.model)
    cfg = RunConfig(scenario=scenario, model=model)

    driver_path = Path(driver)
    is_path = driver not in DRIVER_LEVELS and (
        driver_path.suffix == ".txt" or driver_path.exists()
    )
    cfg = apply_overrides(
        cfg,
        coach_backend=coach,
        driver_level=driver if not is_path else driver_path.stem,
        driver_trace_path=driver_path if is_path else None,
        replay_trace_path=getattr(args, "trace", None),
        fast=getattr(args, "fast", False),
        run_id=getattr(args, "run_id", None),
        **{
            "model.host": args.host,
            "model.deadline_ms": args.deadline_ms,
            "model.seed": args.seed,
            "model.warmup_timeout_s": args.warmup_timeout_s,
            "scenario.initial_velocity_mps": args.initial_velocity,
            "scenario.initial_distance_m": args.sign_distance,
            "scenario.max_duration_s": args.duration_s,
        },
    )
    return cfg


def _cmd_run(args: argparse.Namespace) -> int:
    from .metrics import format_terminal_summary
    from .scenario import run_scenario

    cfg = _build_run_config(args, coach=args.coach, driver=args.driver)
    cfg = replace(cfg, output_dir=args.output)
    summary, _ = run_scenario(
        cfg,
        argv=sys.argv,
        telemetry=args.telemetry,
        telemetry_endpoint=args.telemetry_endpoint,
    )

    from .plotting import plot_run

    png = plot_run(args.output / "run.csv")
    print(format_terminal_summary(summary))
    print(
        f"  artifacts: {args.output}/run.csv, summary.json, trace.jsonl, "
        f"manifest.json, {png.name}"
    )
    return EXIT_OK


def _preflight_ollama_models(args: argparse.Namespace, models: set[str]) -> None:
    """Fail fast, before any run, if any requested model is missing.

    Without this, a missing *second* model would only surface after all
    repetitions of the first had already been spent.
    """
    from .backends.ollama import OllamaBackend, OllamaUnavailableError

    probe_config = load_model_config(args.models_config)
    if args.host is not None:
        probe_config = replace(probe_config, host=args.host)
    probe = OllamaBackend(probe_config)
    try:
        available = set(probe.check_server())
    finally:
        probe.close()
    missing = sorted(models - available)
    if missing:
        pulls = "; ".join(f"ollama pull {m}" for m in missing)
        raise OllamaUnavailableError(
            f"model(s) not available on the Ollama server: {', '.join(missing)} "
            f"(available: {sorted(available) or 'none'}). Pull first: {pulls}. "
            "Note each pull is 1-2+ GB and can take several minutes."
        )


def _run_matrix(
    args: argparse.Namespace,
    variants: list[tuple[str, str]],  # (model, driver) pairs
    group_by: str,
) -> int:
    """Shared engine for benchmark-models and compare-behaviors."""
    from .metrics import format_terminal_summary
    from .plotting import plot_comparison
    from .scenario import run_scenario

    if args.coach == "ollama":
        _preflight_ollama_models(args, {model for model, _ in variants})

    output: Path = args.output
    summaries: list[RunSummary] = []
    for model_name, driver in variants:
        for rep in range(1, args.repetitions + 1):
            sub_args = argparse.Namespace(**vars(args))
            sub_args.model = model_name
            cfg = _build_run_config(sub_args, coach=args.coach, driver=driver)
            run_dir = output / f"{_sanitize(model_name)}-{_sanitize(driver)}-rep{rep}"
            cfg = replace(cfg, output_dir=run_dir, run_id="")
            logger.info("=== %s | driver=%s | repetition %d/%d ===",
                        model_name, driver, rep, args.repetitions)
            summary, _ = run_scenario(cfg, argv=sys.argv)
            print(format_terminal_summary(summary))
            summaries.append(summary)

    table_path = output / "comparison.csv"
    _write_comparison_csv(summaries, table_path)
    (output / "comparison.json").write_text(
        json.dumps([_summary_row(s) for s in summaries], indent=2) + "\n",
        encoding="utf-8",
    )
    png = plot_comparison(summaries, output / "comparison.png", group_by=group_by)
    print(f"\ncomparison artifacts: {table_path}, comparison.json, {png}")
    _print_comparison_table(summaries, group_by)
    return EXIT_OK


def _summary_row(s: RunSummary) -> dict[str, object]:
    lat = s.inference_latency
    return {
        "run_id": s.run_id,
        "model": s.model,
        "driver_level": s.driver_level,
        "coach_backend": s.coach_backend,
        "deadline_ms": s.deadline_ms,
        "requests": s.inference_requests,
        "latency_median_ms": lat.median_ms,
        "latency_p95_ms": lat.p95_ms,
        "latency_max_ms": lat.max_ms,
        "deadline_miss_rate": s.deadline_miss_rate,
        "malformed": s.malformed_response_count,
        "fallbacks": s.fallback_count,
        "late_discarded": s.late_response_count,
        "oracle_agreement_rate": s.oracle_agreement_rate,
        "unsafe_false_negatives": s.unsafe_false_negative_count,
        "warnings": s.warning_count,
        "actuations": s.actuation_count,
        "stopped": s.stopped,
        "v_at_stop_line_mps": s.velocity_at_stop_line_mps,
        "stop_position_error_m": s.stop_position_error_m,
        "safe_bound_violations": s.safe_bound_violation_count,
    }


def _write_comparison_csv(summaries: list[RunSummary], path: Path) -> None:
    import csv as _csv

    rows = [_summary_row(s) for s in summaries]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _print_comparison_table(summaries: list[RunSummary], group_by: str) -> None:
    def key(s: RunSummary) -> str:
        return s.model if group_by == "model" else s.driver_level

    groups: dict[str, list] = {}
    for s in summaries:
        groups.setdefault(key(s), []).append(s)

    def mean(values: list[float | None]) -> str:
        present = [v for v in values if v is not None]
        return f"{sum(present) / len(present):8.1f}" if present else "     n/a"

    print(f"\n{group_by:<22} {'med_lat':>8} {'p95_lat':>8} {'miss%':>6} "
          f"{'malformed':>9} {'unsafe_fn':>9} {'v@line':>7} {'stopped':>7}")
    for label, group in groups.items():
        miss = [s.deadline_miss_rate for s in group]
        present_miss = [m for m in miss if m is not None]
        if present_miss:
            miss_pct = f"{100 * sum(present_miss) / len(present_miss):5.1f}"
        else:
            miss_pct = "  n/a"
        stopped = sum(1 for s in group if s.stopped)
        print(
            f"{label:<22} "
            f"{mean([s.inference_latency.median_ms for s in group])} "
            f"{mean([s.inference_latency.p95_ms for s in group])} "
            f"{miss_pct:>6} "
            f"{sum(s.malformed_response_count for s in group):>9} "
            f"{sum(s.unsafe_false_negative_count for s in group):>9} "
            f"{mean([s.velocity_at_stop_line_mps for s in group]):>7} "
            f"{stopped}/{len(group):>4}"
        )


def _cmd_doctor(args: argparse.Namespace) -> int:
    problems: list[str] = []
    warnings: list[str] = []

    def ok(label: str, detail: str = "") -> None:
        print(f"  [ok]   {label}" + (f" - {detail}" if detail else ""))

    def bad(label: str, detail: str, fatal: bool = True) -> None:
        print(f"  [{'FAIL' if fatal else 'warn'}] {label} - {detail}")
        (problems if fatal else warnings).append(f"{label}: {detail}")

    print("agentic-driving-coach doctor\n")

    v = sys.version_info
    if (3, 10) <= (v.major, v.minor) < (3, 14):
        ok("python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        bad("python", f"{v.major}.{v.minor} unsupported; need 3.10-3.13 (lab-supported range)")

    try:
        from importlib.metadata import version

        import xronos  # noqa: F401

        xronos_version = version("xronos")
        if xronos_version == "0.13.1":
            ok("xronos", xronos_version)
        else:
            bad("xronos", f"found {xronos_version}, lab is pinned to 0.13.1", fatal=False)
    except ImportError:
        bad("xronos", "not installed; run: pip install -e .")

    try:
        import matplotlib

        ok("matplotlib", f"{matplotlib.__version__} (backend {matplotlib.get_backend()})")
    except ImportError:
        bad("matplotlib", "not installed; run: pip install -e .")

    data = default_data_dir()
    for level in DRIVER_LEVELS:
        trace = data / "driver" / f"{level}.txt"
        if trace.exists():
            ok(f"driver trace {level}", str(trace))
        else:
            bad(f"driver trace {level}", f"missing {trace}")
    replay_example = data / "replay" / "example_trace.jsonl"
    if replay_example.exists():
        ok("replay example", str(replay_example))
    else:
        bad("replay example", f"missing {replay_example}")
    for cfg_name in ("stop_sign.toml", "models.toml"):
        cfg_path = default_configs_dir() / cfg_name
        if cfg_path.exists():
            ok(f"config {cfg_name}", str(cfg_path))
        else:
            bad(f"config {cfg_name}", f"missing {cfg_path}")

    # Live path (optional unless --live): never pulls anything.
    host = args.host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434"
    try:
        import ollama

        try:
            listing = ollama.Client(host=host, timeout=3).list()
            available = [m.model for m in listing.models if m.model is not None]
            ok("ollama server", f"{host} ({len(available)} model(s))")
            for model in args.models:
                if model in available:
                    ok(f"model {model}", "pulled")
                else:
                    bad(
                        f"model {model}",
                        f"not on server; you may run: ollama pull {model}",
                        fatal=args.live,
                    )
            if args.warm:
                from .backends.ollama import OllamaBackend, OllamaUnavailableError
                from .config import ModelConfig

                for model in args.models:
                    if model not in available:
                        continue
                    backend = OllamaBackend(
                        ModelConfig(
                            model=model,
                            host=args.host,
                            warmup_timeout_s=args.warmup_timeout_s,
                        )
                    )
                    try:
                        seconds = backend.warm_up()
                        ok(f"warm-up {model}", f"resident in {seconds:.1f} s")
                        timings = backend.probe()

                        def fmt(value: float | None) -> str:
                            return "?" if value is None else f"{value:.0f}"

                        rate = timings.get("tokens_per_s")
                        ok(
                            f"probe {model}",
                            f"client {fmt(timings['client_ms'])} ms | server "
                            f"{fmt(timings['server_total_ms'])} ms (load "
                            f"{fmt(timings['load_ms'])}, prompt "
                            f"{fmt(timings['prompt_ms'])}, generate "
                            f"{fmt(timings['generate_ms'])} ms; "
                            f"{fmt(rate)} tok/s - if this is <~50 on a GPU "
                            "node, suspect CPU-fallback serving: check "
                            "'ollama ps')",
                        )
                    except OllamaUnavailableError as exc:
                        bad(f"warm-up/probe {model}", str(exc), fatal=args.live)
                    finally:
                        backend.close()
        except Exception as exc:
            bad(
                "ollama server",
                f"unreachable at {host} ({type(exc).__name__}); "
                "start it with: ollama serve  (only needed for --coach ollama)",
                fatal=args.live,
            )
    except ImportError:
        bad("ollama client", "package missing; run: pip install -e .", fatal=args.live)

    print()
    if problems:
        print(f"doctor: {len(problems)} problem(s) found")
        return EXIT_SETUP
    if warnings:
        print(f"doctor: OK for offline work; {len(warnings)} warning(s) for the live path")
    else:
        print("doctor: all checks passed")
    return EXIT_OK


def _cmd_plot(args: argparse.Namespace) -> int:
    from .plotting import plot_run

    if not args.csv.exists():
        raise ConfigError(f"csv file not found: {args.csv}")
    out = plot_run(args.csv, args.output)
    print(f"wrote {out}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "benchmark-models":
            variants = [(m, args.driver) for m in args.models]
            return _run_matrix(args, variants, group_by="model")
        if args.command == "compare-behaviors":
            model = args.model or load_model_config(args.models_config).model
            variants = [(model, d) for d in args.drivers]
            return _run_matrix(args, variants, group_by="driver_level")
        if args.command == "plot":
            return _cmd_plot(args)
        raise AssertionError(f"unhandled command {args.command}")
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return EXIT_CONFIG
    except Exception as exc:
        from .backends.ollama import OllamaUnavailableError

        if isinstance(exc, OllamaUnavailableError):
            logger.error("setup error: %s", exc)
            return EXIT_SETUP
        logger.exception("unexpected error")
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
