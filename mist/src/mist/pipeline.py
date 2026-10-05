"""Analyze one fixed repository snapshot and write its reuse evidence."""

import contextlib
import csv
import hashlib
import importlib.metadata
import io
import json
import platform
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import jedi

from mist.analysis import calls, context, identifiers, syntax_index
from mist.bindings import run as tracing
from mist.io import read_csv
from mist.resources import DATA_DIR
from mist.bindings.selection import SINK_MODES


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_rows(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def analyze(repo, commit, repository, output, full=False, model_ids=None, sink_mode='shortest'):
    """The caller supplies a clean checkout at the requested commit."""
    repo, output = Path(repo).resolve(), Path(output).resolve()
    if sink_mode not in SINK_MODES:
        raise ValueError(f'Unknown sink mode: {sink_mode}')
    model_ids = Path(model_ids).resolve() if model_ids is not None else DATA_DIR / "ptm_ids.csv"
    if not model_ids.is_file():
        raise ValueError(f"PTM ID vocabulary not found: {model_ids}")
    vocabulary = identifiers.load_model_ids(model_ids)
    if not vocabulary:
        raise ValueError("The PTM ID vocabulary must contain at least one ID")
    vocabulary_hash = file_hash(model_ids)
    output.mkdir(parents=True, exist_ok=False)
    snapshot = repository.replace("/", "__") + "_" + commit[:12]
    previous_cache = jedi.settings.cache_directory
    with tempfile.TemporaryDirectory(prefix="mist-analysis-") as temporary:
        work = Path(temporary)
        jedi.settings.cache_directory = str(work / "jedi")
        facts, seeds, loaders, contexts, traces = [work / name for name in
                                                 ["syntax", "identifiers", "calls", "context", "bindings"]]
        try:
            # Intermediate CSVs stay in the temporary directory.
            with contextlib.redirect_stdout(io.StringIO()):
                syntax_index.main(["--repo", str(repo), "--output-dir", str(facts)])
                identifiers.main(["--repo", str(repo), "--model-ids", str(model_ids),
                                  "--reuse-codebook", str(DATA_DIR / "reuse_rules.xlsx"),
                                  "--syntax-cache", str(facts), "--output-dir", str(seeds)])
                calls.main(["--repo", str(repo), "--reuse-codebook", str(DATA_DIR / "reuse_rules.xlsx"),
                            "--import-origins", str(seeds / "import_origin_occurrences.csv"),
                            "--syntax-cache", str(facts), "--repo-full-name", repository,
                            "--commit", commit, "--enable-jedi", "--output-dir", str(loaders)])
                context.main(["--repo", str(repo), "--model-occurrences", str(seeds / "model_id_occurrences.csv"),
                              "--loader-candidates", str(loaders / "loader_candidates.csv"),
                              "--syntax-cache", str(facts), "--output-dir", str(contexts)])
                tracing.main(["--repo", str(repo), "--model-id-contexts", str(contexts / "model_id_contexts.csv"),
                              "--loader-candidates", str(loaders / "loader_candidates.csv"),
                              "--reuse-codebook", str(DATA_DIR / "reuse_rules.xlsx"),
                              "--framework-config-summaries", str(DATA_DIR / "frameworks.json"),
                              "--syntax-cache", str(facts), "--repo-snapshot", snapshot,
                              "--sink-mode", sink_mode,
                              "--enable-jedi", "--output-dir", str(traces)])
            trace_rows = read_csv(traces / "binding_source_sink_traces.csv")
            context_rows = read_csv(contexts / "model_id_contexts.csv")
            by_occurrence = {row["model_id_occurrence_id"]: row for row in trace_rows}
            decisions = []
            for row in context_rows:
                trace = by_occurrence.get(row["model_id_occurrence_id"])
                status = trace["trace_status"] if trace else row["preliminary_label"]
                decisions.append({"occurrence_id": row["model_id_occurrence_id"],
                                  "ptm_id": row["canonical_model_id"], "path": row["file_path"],
                                  "line": row["line_number"], "column": row["column_start"],
                                  "confirmed_reuse": status == "confirmed_real_reuse", "status": status})
            write_rows(output / "decisions.csv", decisions,
                       ["occurrence_id", "ptm_id", "path", "line", "column", "confirmed_reuse", "status"])
            shutil.copyfile(traces / "binding_source_sink_traces.csv", output / "traces.csv")
            shutil.copyfile(traces / "binding_trace_steps.csv", output / "trace_steps.csv")
            for source, target in (
                ('binding_confirmed_bindings.csv', 'bindings.csv'),
                ('binding_confirmed_steps.csv', 'binding_steps.csv'),
                ('binding_sink_checks.csv', 'sink_checks.csv'),
            ):
                shutil.copyfile(traces / source, output / target)
            audit = traces / "mock_scope_audit.json"
            checks = json.loads(audit.read_text())["checks"] if audit.exists() else []
            (output / "mock_checks.json").write_text(json.dumps({"checks": checks}, indent=2) + "\n")
            if full:
                shutil.copyfile(seeds / "model_id_occurrences.csv", output / "model_id_occurrences.csv")
                shutil.copyfile(seeds / "import_origin_occurrences.csv", output / "import_origin_occurrences.csv")
                shutil.copyfile(traces / "binding_trace_graph_edges.csv", output / "binding_graph.csv")
                shutil.copyfile(loaders / "call_graph_edges.csv", output / "call_graph_edges.csv")
                shutil.copyfile(loaders / "loader_candidates.csv", output / "candidate_calls.csv")
                shutil.copyfile(contexts / "model_id_contexts.csv", output / "source_contexts.csv")
            binding_summary = json.loads((traces / "binding_summary.json").read_text())
            summary = {"engine": "MIST", "repository": repository, "commit": commit,
                       "confirmed_reuse": any(row["confirmed_reuse"] for row in decisions),
                       "occurrences": len(decisions),
                       "confirmed_occurrences": sum(row["confirmed_reuse"] for row in decisions),
                       "status_counts": dict(Counter(row["status"] for row in decisions)),
                       "python": platform.python_version(), "python_executable": sys.executable,
                       "packages": {name: importlib.metadata.version(name) for name in
                                    ["jedi", "parso", "networkx", "openpyxl", "httpx", "requests"]},
                       "inputs_sha256": {path.name: file_hash(path) for path in sorted(DATA_DIR.iterdir()) if path.is_file()},
                       "lookup": json.loads((loaders / "call_summary.json").read_text())["jedi_origin_edges"],
                       "tracing": binding_summary["jedi_graph_evidence"],
                       "jedi_diagnostics": binding_summary["analysis_status"]}
            summary["parse_errors"] = {row["file_path"]: row["parse_error"]
                                       for row in read_csv(facts / "files.csv") if row["parse_error"]}
            summary["parse_errors"].update(binding_summary["repo_parse_error_files"])
            summary['sink_mode'] = sink_mode
            summary['checked_bindings'] = len(read_csv(output / 'sink_checks.csv'))
            summary['confirmed_bindings'] = len(read_csv(output / 'bindings.csv'))
            if file_hash(model_ids) != vocabulary_hash:
                raise ValueError("The PTM ID vocabulary changed during analysis")
            summary["inputs_sha256"]["ptm_ids.csv"] = vocabulary_hash
            summary["model_id_vocabulary"] = {"path": str(model_ids), "count": len(vocabulary),
                                              "sha256": vocabulary_hash}
            (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        finally:
            jedi.settings.cache_directory = previous_cache
    return summary
