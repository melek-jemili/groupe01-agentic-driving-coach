#!/usr/bin/env python3
"""Build the submission archive for Canvas.

Usage:
    python scripts/make_submission.py --groupid your_groupid \
        [--results results/...]...
        [--zip submission/group<groupid>_agentic-driving-coach.zip]

Included:
- submission/answers.md, stored as answers.md
- all regular project files under src/, configs/, data/, examples/, scripts/,
  and slurm/, except scripts/make_submission.py
- containers/Apptainer.def and pyproject.toml
- results/environment.txt if present
- from each named results directory: summary.json, manifest.json, run.csv,
  trace.jsonl, comparison.{csv,json}, and all PNG figures

Never included: model weights, archives, virtual environments, caches, the
submission builder itself, or any single file over 20 MB.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAX_FILE_BYTES = 20 * 1024 * 1024
RESULT_PATTERNS = (
    "summary.json",
    "manifest.json",
    "run.csv",
    "trace.jsonl",
    "comparison.csv",
    "comparison.json",
    "*.png",
)
PROJECT_DIRECTORIES = ("src", "configs", "data", "examples", "scripts", "slurm")
PROJECT_FILES = ("containers/Apptainer.def", "pyproject.toml")
EXCLUDED_DIRECTORY_NAMES = {
    ".cache",
    ".git",
    ".github",
    ".idea",
    ".ipynb_checkpoints",
    ".mypy_cache",
    ".nox",
    ".ollama",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "node_modules",
    "ollama",
}
EXCLUDED_SUFFIXES = {
    ".7z",
    ".bz2",
    ".ckpt",
    ".gguf",
    ".gz",
    ".log",
    ".onnx",
    ".pt",
    ".pth",
    ".pyc",
    ".rar",
    ".safetensors",
    ".sif",
    ".swp",
    ".swo",
    ".tar",
    ".tgz",
    ".tmp",
    ".xz",
    ".zip",
    ".zst",
}
SUBMISSION_BUILDER = REPO / "scripts" / "make_submission.py"


def _is_inside_repo(path: Path) -> bool:
    try:
        path.relative_to(REPO)
    except ValueError:
        return False
    return True


def _inspect_file(path: Path) -> tuple[Path | None, str | None]:
    """Return a safe regular file and no reason, or a concise skip reason."""
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return None, "cannot be resolved"
    if not _is_inside_repo(resolved):
        return None, "resolves outside the repository"
    if not resolved.is_file():
        return None, "not a regular file"
    try:
        size = resolved.stat().st_size
    except OSError:
        return None, "cannot be read"
    if size > MAX_FILE_BYTES:
        return None, f"over {MAX_FILE_BYTES // (1024 * 1024)} MB"
    suffixes = {path.suffix.lower(), resolved.suffix.lower()}
    excluded = sorted(suffixes & EXCLUDED_SUFFIXES)
    if excluded:
        return None, f"excluded type ({excluded[0]})"
    return resolved, None


def _project_candidates() -> list[Path]:
    candidates: list[Path] = []
    for name in PROJECT_DIRECTORIES:
        root = REPO / name
        try:
            resolved_root = root.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            continue
        if not _is_inside_repo(resolved_root) or not resolved_root.is_dir():
            continue
        for path in root.rglob("*"):
            relative = path.relative_to(REPO)
            if any(part in EXCLUDED_DIRECTORY_NAMES for part in relative.parts[:-1]):
                continue
            if path.is_dir():
                continue
            candidates.append(path)
    for name in PROJECT_FILES:
        path = REPO / name
        if path.exists() or path.is_symlink():
            candidates.append(path)
    return sorted(candidates, key=lambda path: path.relative_to(REPO).as_posix())


def _result_matches(directory: Path) -> list[Path]:
    matches: dict[str, Path] = {}
    for pattern in RESULT_PATTERNS:
        for path in directory.rglob(pattern):
            if path.is_dir():
                continue
            matches[path.relative_to(REPO).as_posix()] = path
    return [matches[name] for name in sorted(matches)]


def _resolve_result_directory(path: Path) -> tuple[Path | None, str | None]:
    candidate = path if path.is_absolute() else REPO / path
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError):
        return None, f"result directory does not exist: {path}"
    if not _is_inside_repo(resolved):
        return None, f"result directory is outside the repository: {path}"
    if not resolved.is_dir():
        return None, f"result path is not a directory: {path}"
    return resolved, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groupid", required=True, help="your group (names the zip)")
    parser.add_argument(
        "--results",
        action="append",
        type=Path,
        default=[],
        help="results directory to include (repeatable); default: all of results/",
    )
    parser.add_argument("--zip", type=Path, default=None, help="output zip path")
    args = parser.parse_args()

    out_zip = (
        args.zip
        or REPO / "submission" / f"group{args.groupid}_agentic-driving-coach.zip"
    )
    if not out_zip.is_absolute():
        out_zip = REPO / out_zip

    answers = REPO / "submission" / "answers.md"
    if not answers.exists():
        print(
            "ERROR: submission/answers.md not found.\n"
            "Copy submission/answers_template.md to submission/answers.md and fill it in.",
            file=sys.stderr,
        )
        return 2

    explicit_results = bool(args.results)
    if explicit_results:
        result_dirs: list[Path] = []
        for requested in args.results:
            directory, error = _resolve_result_directory(requested)
            if error:
                print(f"ERROR: {error}", file=sys.stderr)
                return 2
            assert directory is not None
            result_dirs.append(directory)
    else:
        results_root = REPO / "results"
        result_dirs = []
        resolved_results_root, error = _resolve_result_directory(results_root)
        if error is None:
            assert resolved_results_root is not None
            for path in resolved_results_root.iterdir():
                if not path.is_dir():
                    continue
                directory, error = _resolve_result_directory(path)
                if error is None:
                    assert directory is not None
                    result_dirs.append(directory)
            result_dirs.sort(key=lambda path: path.relative_to(REPO).as_posix())

    selected: dict[str, tuple[Path, str]] = {}
    skipped: dict[str, str] = {}

    def add_file(path: Path, arcname: str, category: str) -> bool:
        resolved, reason = _inspect_file(path)
        if reason:
            skipped.setdefault(arcname, reason)
            return False
        assert resolved is not None
        selected.setdefault(arcname, (resolved, category))
        return True

    if not add_file(answers, "answers.md", "answers"):
        print(
            f"ERROR: submission/answers.md cannot be included: {skipped['answers.md']}",
            file=sys.stderr,
        )
        return 2

    for path in _project_candidates():
        arcname = path.relative_to(REPO).as_posix()
        if path == SUBMISSION_BUILDER:
            skipped.setdefault(arcname, "submission builder excluded")
            continue
        add_file(path, arcname, "project")

    source_prefix = "src/agentic_driving_coach/"
    if not any(
        category == "project" and arcname.startswith(source_prefix)
        for arcname, (_, category) in selected.items()
    ):
        print(
            "ERROR: no source files found under src/agentic_driving_coach.",
            file=sys.stderr,
        )
        return 2

    env_txt = REPO / "results" / "environment.txt"
    if env_txt.exists() or env_txt.is_symlink():
        add_file(env_txt, "results/environment.txt", "result")

    for directory in result_dirs:
        matches = _result_matches(directory)
        valid_matches = 0
        for path in matches:
            resolved, reason = _inspect_file(path)
            if resolved is not None:
                valid_matches += 1
            arcname = path.relative_to(REPO).as_posix()
            if reason:
                skipped.setdefault(arcname, reason)
            else:
                assert resolved is not None
                selected.setdefault(arcname, (resolved, "result"))
        if explicit_results and valid_matches == 0:
            relative = directory.relative_to(REPO).as_posix()
            print(
                f"ERROR: no required result files found in {relative}.",
                file=sys.stderr,
            )
            return 2

    try:
        out_resolved = out_zip.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: cannot resolve output ZIP path: {exc}", file=sys.stderr)
        return 2
    for arcname, (source, _) in selected.items():
        same_path = out_resolved == source
        same_file = False
        if out_zip.exists():
            try:
                same_file = out_zip.samefile(source)
            except OSError:
                pass
        if same_path or same_file:
            print(
                f"ERROR: output ZIP would overwrite an input file: {arcname}",
                file=sys.stderr,
            )
            return 2

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname in sorted(selected):
            source, _ = selected[arcname]
            zf.write(source, arcname)

    answer_count = sum(category == "answers" for _, category in selected.values())
    project_count = sum(category == "project" for _, category in selected.values())
    result_count = sum(category == "result" for _, category in selected.values())

    print(f"Output ZIP: {out_zip}")
    print(f"Total files included: {len(selected)}")
    print("Answers")
    print(f"  {answer_count} file: answers.md")
    print("Project files")
    print(f"  {project_count} files")
    print("Result files")
    print(f"  {result_count} files")
    print("Skipped files")
    if skipped:
        for arcname, reason in sorted(skipped.items()):
            print(f"  {arcname}: {reason}")
    else:
        print("  None")
    return 0


if __name__ == "__main__":
    sys.exit(main())
