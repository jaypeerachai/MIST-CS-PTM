"""Record Jedi parsing diagnostics for files containing sources or calls."""

from __future__ import annotations

import argparse
from pathlib import Path

from mist.bindings.types import AnalysisStatus


def run_analysis_checks(
    args: argparse.Namespace,
    repo: Path,
    model_rows: list[dict[str, str]],
    loader_rows: list[dict[str, str]],
) -> AnalysisStatus:
    status = AnalysisStatus()
    if not args.enable_jedi:
        status.jedi_status = "disabled"
        return status

    files_needed = sorted({row["file_path"] for row in model_rows} | {row["file_path"] for row in loader_rows})
    files_needed = files_needed[:args.max_diagnostic_files]
    try:
        import jedi

        ok = 0
        failed = 0
        last_error = ""
        for rel_path in files_needed:
            try:
                source = (repo / rel_path).read_text(encoding="utf-8", errors="replace")
                jedi.Script(source, path=str(repo / rel_path))
                ok += 1
            except Exception as exc:
                failed += 1
                last_error = f"{type(exc).__name__}: {exc}"
        status.jedi_status = "ok" if failed == 0 else ("partial" if ok else "failed")
        status.jedi_files_ok = ok
        status.jedi_files_failed = failed
        status.jedi_error = last_error
    except Exception as exc:
        status.jedi_status = "failed"
        status.jedi_error = f"{type(exc).__name__}: {exc}"
    return status
