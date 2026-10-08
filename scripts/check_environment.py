#!/usr/bin/env python3
"""Capture environment information for the submission and run doctor checks.

Usage:
    python scripts/check_environment.py [--output results/environment.txt] [--live]

Writes a plain-text report (hostname, OS, Python, xronos, ollama client/server,
available models, nvidia-smi when present) and then runs `agentic_driving_coach doctor`.
Exit code mirrors doctor's. Never downloads anything.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _cmd_output(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        return proc.stdout.strip() or proc.stderr.strip() or "(no output)"
    except FileNotFoundError:
        return "(not installed)"
    except subprocess.TimeoutExpired:
        return "(timed out)"


def _package_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "(not installed)"


def gather() -> str:
    lines = [
        f"captured: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"hostname: {socket.gethostname()}",
        f"platform: {platform.platform()}",
        f"python: {sys.version.replace(chr(10), ' ')}",
        f"python executable: {sys.executable}",
        f"xronos: {_package_version('xronos')}",
        f"matplotlib: {_package_version('matplotlib')}",
        f"ollama (python client): {_package_version('ollama')}",
        f"OLLAMA_HOST: {os.environ.get('OLLAMA_HOST', '(unset; default http://127.0.0.1:11434)')}",
        f"MPLBACKEND: {os.environ.get('MPLBACKEND', '(unset)')}",
    ]
    if shutil.which("ollama"):
        lines.append(f"ollama CLI version: {_cmd_output(['ollama', '--version'])}")
        lines.append("ollama models:\n" + _cmd_output(["ollama", "list"]))
    else:
        lines.append("ollama CLI: (not on PATH)")
    if shutil.which("nvidia-smi"):
        lines.append("nvidia-smi:\n" + _cmd_output(["nvidia-smi"]))
    else:
        lines.append("nvidia-smi: (not available - CPU-only or drivers missing)")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("results/environment.txt"))
    parser.add_argument("--live", action="store_true", help="require live Ollama in doctor")
    args = parser.parse_args()

    report = gather()
    print(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"wrote {args.output}\n")

    from agentic_driving_coach.cli import main as cli_main

    doctor_args = ["doctor"] + (["--live"] if args.live else [])
    return cli_main(doctor_args)


if __name__ == "__main__":
    sys.exit(main())
