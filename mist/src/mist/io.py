"""Shared CSV, path, and value helpers."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "envs",
    "site-packages",
    "dist-packages",
}


def iter_python_files(
    repo: Path,
    *,
    skip_dirs: set[str] | None = None,
    check_file_name: bool = False,
    sort_paths: bool = False,
) -> Iterable[Path]:
    """Yield Python files while skipping common generated/dependency folders."""
    skip_dirs = skip_dirs or DEFAULT_SKIP_DIRS
    paths = repo.rglob("*.py")
    if sort_paths:
        paths = sorted(paths)
    for path in paths:
        if not path.is_file():
            continue
        rel_parts = path.relative_to(repo).parts
        parts_to_check = rel_parts if check_file_name else rel_parts[:-1]
        if any(part in skip_dirs for part in parts_to_check):
            continue
        yield path


def read_csv(path: Path, *, missing_ok: bool = False) -> list[dict[str, str]]:
    """Read a CSV into dictionaries, optionally treating a missing file as empty."""
    if missing_ok and not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    quote_all: bool = False,
    clean_values: bool = False,
    ensure_parent: bool = False,
) -> None:
    """Write dictionaries to CSV with the quoting style each script needs."""
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    output_rows = [
        {key: clean_csv_value(value) for key, value in row.items()}
        for row in rows
    ] if clean_values else rows
    quoting = csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL
    with path.open("w", newline="", encoding="utf-8", errors="backslashreplace") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0].keys()),
            quoting=quoting,
            escapechar="\\",
            doublequote=True,
        )
        writer.writeheader()
        writer.writerows(output_rows)


def clean_csv_value(value: Any) -> str:
    """Keep CSV cells single-line and free of null bytes."""
    text = "" if value is None else str(value)
    return text.replace("\x00", "").replace("\r", "\\r")


def parse_bool(value: Any) -> bool:
    """Parse common truthy strings used in CSV outputs."""
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def parse_int(value: Any) -> int:
    """Parse an integer field and fall back to 0 for blank/bad values."""
    try:
        return int(str(value).strip())
    except Exception:
        return 0


def count_values(rows: Iterable[Any], field: str) -> dict[str, int]:
    """Count one field on dict rows or dataclass-like objects."""
    counter: Counter[str] = Counter()
    for row in rows:
        if isinstance(row, dict):
            value = row.get(field, "")
        else:
            value = getattr(row, field)
        counter[str(value)] += 1
    return dict(sorted(counter.items()))


def display_output_path(path: Path, *, resolve: bool = False) -> str:
    """Show repo-relative output paths when possible."""
    try:
        if resolve:
            rel_path = path.resolve().relative_to(Path.cwd().resolve())
        else:
            rel_path = path.relative_to(Path.cwd())
        return f"/{rel_path.as_posix()}"
    except ValueError:
        return str(path)


def strip_quotes(value: str) -> str:
    """Remove one matching pair of quote characters."""
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text
