"""Static, headless plot generation (PNG only; never opens a window).

The Agg backend is selected before matplotlib loads unless the user already
set MPLBACKEND explicitly. No function here calls plt.show().
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib

if not os.environ.get("DISPLAY") and matplotlib.get_backend().lower() != "agg":
    matplotlib.use("Agg")

import matplotlib.pyplot as plt

from .metrics import RunSummary


@dataclass
class _RunSeries:
    time_ms: list[float]
    distance: list[float]
    velocity: list[float]
    safe_lower: list[float | None]
    safe_upper: list[float | None]
    warning_points: list[tuple[float, float, float]]  # (t, distance, velocity)
    actuate_points: list[tuple[float, float, float]]
    miss_points: list[tuple[float, float, float]]
    title: str


def _to_float(value: str) -> float | None:
    return float(value) if value not in ("", None) else None


def load_run_csv(path: Path) -> _RunSeries:
    time_ms: list[float] = []
    distance: list[float] = []
    velocity: list[float] = []
    safe_lower: list[float | None] = []
    safe_upper: list[float | None] = []
    warning_points: list[tuple[float, float, float]] = []
    actuate_points: list[tuple[float, float, float]] = []
    miss_points: list[tuple[float, float, float]] = []
    title = path.stem

    with path.open("r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            t = _to_float(row.get("logical_time_ms", ""))
            d = _to_float(row.get("distance_m", ""))
            v = _to_float(row.get("velocity_mps", ""))
            title = (
                f"{row.get('scenario', '?')} | driver={row.get('driver_level', '?')} | "
                f"coach={row.get('coach_backend', '?')} | model={row.get('model', '?')}"
            )
            if t is None:
                continue
            # Physics samples: tags that carried an actual velocity event.
            if row.get("travelled_m") not in ("", None) and d is not None and v is not None:
                time_ms.append(t)
                distance.append(d)
                velocity.append(v)
                safe_lower.append(_to_float(row.get("safe_lower_mps", "")))
                safe_upper.append(_to_float(row.get("safe_upper_mps", "")))
            if d is None or v is None:
                continue
            if row.get("coach_token") == "WARNING":
                warning_points.append((t, d, v))
            if row.get("actuation"):
                actuate_points.append((t, d, v))
            if row.get("deadline_miss") in ("True", "true", "1"):
                miss_points.append((t, d, v))

    return _RunSeries(
        time_ms,
        distance,
        velocity,
        safe_lower,
        safe_upper,
        warning_points,
        actuate_points,
        miss_points,
        title,
    )


def _shade_bands(ax, xs: list[float], lower: list[float | None], upper: list[float | None]) -> None:
    lo = [-1.0 if v is None else v for v in lower]
    hi = [-1.0 if v is None else v for v in upper]
    where = [u >= 0 for u in hi]
    if any(where):
        ax.fill_between(
            xs, lo, hi, where=where, alpha=0.15, color="tab:green",
            label="safe velocity band", step="mid",
        )


def plot_run(csv_path: Path, out_path: Path | None = None) -> Path:
    """The required run overview: velocity vs. distance and vs. logical time,
    with safety bands and warning/actuation/deadline-miss markers."""
    series = load_run_csv(csv_path)
    if not series.time_ms:
        raise ValueError(f"{csv_path}: no physics samples found (is this a run.csv?)")
    out_path = out_path or csv_path.with_name("run_overview.png")

    fig, (ax_d, ax_t) = plt.subplots(1, 2, figsize=(12, 4.5), sharey=True)

    ax_d.plot(series.distance, series.velocity, color="tab:blue", lw=1.8, label="velocity")
    _shade_bands(ax_d, series.distance, series.safe_lower, series.safe_upper)
    ax_d.scatter(
        [p[1] for p in series.warning_points], [p[2] for p in series.warning_points],
        marker="^", color="tab:orange", zorder=3, label="WARNING",
    )
    ax_d.scatter(
        [p[1] for p in series.actuate_points], [p[2] for p in series.actuate_points],
        marker="v", color="tab:red", zorder=3, label="ACTUATE (emergency brake)",
    )
    ax_d.scatter(
        [p[1] for p in series.miss_points], [p[2] for p in series.miss_points],
        marker="x", color="black", zorder=3, label="deadline miss",
    )
    ax_d.axvline(0.0, color="tab:red", ls="--", lw=1, label="stop line")
    ax_d.set_xlabel("distance to stop sign (m)")
    ax_d.set_ylabel("velocity (m/s)")
    ax_d.invert_xaxis()
    ax_d.legend(fontsize=8, loc="best")

    times_s = [t / 1000.0 for t in series.time_ms]
    ax_t.plot(times_s, series.velocity, color="tab:blue", lw=1.8)
    _shade_bands(ax_t, times_s, series.safe_lower, series.safe_upper)
    ax_t.scatter(
        [p[0] / 1000.0 for p in series.warning_points],
        [p[2] for p in series.warning_points],
        marker="^", color="tab:orange", zorder=3,
    )
    ax_t.scatter(
        [p[0] / 1000.0 for p in series.actuate_points],
        [p[2] for p in series.actuate_points],
        marker="v", color="tab:red", zorder=3,
    )
    ax_t.scatter(
        [p[0] / 1000.0 for p in series.miss_points],
        [p[2] for p in series.miss_points],
        marker="x", color="black", zorder=3,
    )
    ax_t.set_xlabel("logical time (s)")

    fig.suptitle(series.title, fontsize=10)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path


def _summary_label(summary: RunSummary) -> str:
    return f"{summary.model}\n({summary.driver_level})"


def plot_comparison(
    summaries: list[RunSummary], out_path: Path, group_by: str = "model"
) -> Path:
    """Comparison across models or driver levels: latency distribution proxy
    (median & p95), deadline-miss rate, and stopping outcome."""
    if not summaries:
        raise ValueError("no summaries to plot")

    def key(s: RunSummary) -> str:
        return s.model if group_by == "model" else s.driver_level

    groups: dict[str, list[RunSummary]] = {}
    for s in summaries:
        groups.setdefault(key(s), []).append(s)
    labels = list(groups)

    fig, (ax_lat, ax_miss, ax_stop) = plt.subplots(1, 3, figsize=(13, 4))

    medians = [
        [x.inference_latency.median_ms or 0.0 for x in groups[label]] for label in labels
    ]
    p95s = [[x.inference_latency.p95_ms or 0.0 for x in groups[label]] for label in labels]
    positions = range(len(labels))
    ax_lat.bar(
        [p - 0.18 for p in positions],
        [sum(m) / len(m) for m in medians],
        width=0.36, label="median", color="tab:blue",
    )
    ax_lat.bar(
        [p + 0.18 for p in positions],
        [sum(p95) / len(p95) for p95 in p95s],
        width=0.36, label="p95", color="tab:orange",
    )
    deadline = summaries[0].deadline_ms
    ax_lat.axhline(deadline, color="tab:red", ls="--", lw=1, label=f"deadline {deadline:.0f} ms")
    ax_lat.set_xticks(list(positions), labels)
    ax_lat.set_ylabel("inference latency (ms)")
    ax_lat.legend(fontsize=8)

    miss_rates = [
        [x.deadline_miss_rate or 0.0 for x in groups[label]] for label in labels
    ]
    ax_miss.bar(
        list(positions),
        [100 * sum(m) / len(m) for m in miss_rates],
        color="tab:red", alpha=0.8,
    )
    ax_miss.set_xticks(list(positions), labels)
    ax_miss.set_ylabel("deadline-miss rate (%)")

    stop_v = [
        [
            x.velocity_at_stop_line_mps
            for x in groups[label]
            if x.velocity_at_stop_line_mps is not None
        ]
        for label in labels
    ]
    ax_stop.bar(
        list(positions),
        [sum(v) / len(v) if v else 0.0 for v in stop_v],
        color="tab:green", alpha=0.8,
    )
    ax_stop.set_xticks(list(positions), labels)
    ax_stop.set_ylabel("velocity at/nearest stop line (m/s)")

    for ax in (ax_lat, ax_miss, ax_stop):
        ax.tick_params(axis="x", labelsize=8)
    fig.suptitle(f"comparison by {group_by} (mean over repetitions)", fontsize=10)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path
