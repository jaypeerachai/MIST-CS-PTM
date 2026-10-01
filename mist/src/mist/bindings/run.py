"""Build the binding graph, trace eligible sources, and export evidence."""

from __future__ import annotations

from dataclasses import asdict
from mist.io import parse_bool
from mist.io import read_csv
from mist.io import write_csv
from mist.resources import DATA_DIR
from pathlib import Path
from typing import Sequence
import argparse
import json
import networkx as nx
from mist.bindings.graph import (
    SourceSinkBuilder,
)
from mist.bindings.diagnostics import (
    run_analysis_checks,
)
from mist.bindings.reports import (
    write_graph_edges,
    write_binding_outputs,
    write_binding_summary,
    write_sink_outputs,
)
from mist.bindings.repository import (
    parse_repo,
)
from mist.bindings.syntax import (
    build_identity_vocabulary,
    infer_repo_snapshot,
)
from mist.bindings.tracing import (
    make_pregraph_traces,
    binding_loader_candidate_is_eligible,
    trace_sources_to_sinks,
)
from mist.bindings.types import (
    AnalysisStatus,
)
from mist.bindings.selection import SINK_MODES


def main(argv: Sequence[str] | None = None) -> int:
    """Build the binding tracing graph, trace every allowed source, and write outputs."""
    args = parse_args(argv)
    repo = args.repo.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Source context determines which exact model-ID rows need tracing.
    model_rows = [
        row for row in read_csv(args.model_id_contexts, missing_ok=True)
        if parse_bool(row.get("binding_allowed", ""))
    ]
    # Candidate sinks still require an exact source-to-sink path.
    loader_rows = [
        row for row in read_csv(args.loader_candidates, missing_ok=True)
        if binding_loader_candidate_is_eligible(row)
    ]
    if not model_rows or not loader_rows:
        identity_vocab = build_identity_vocabulary(args.reuse_codebook, loader_rows)
        analysis_status = AnalysisStatus()
        traces, steps = make_pregraph_traces(
            repo_snapshot=args.repo_snapshot or infer_repo_snapshot(args.output_dir),
            model_rows=model_rows,
            analysis_status=analysis_status,
        )
        write_binding_outputs(
            args=args,
            output_dir=output_dir,
            repo=repo,
            model_rows=model_rows,
            loader_rows=loader_rows,
            traces=traces,
            steps=steps,
            graph=nx.DiGraph(),
            analysis_status=analysis_status,
            identity_vocab=identity_vocab,
            jedi_summary={
                "enabled": args.enable_jedi,
                "resolved_files_added": 0,
                "symbol_resolution_edges_added": 0,
                "internal_loader_sinks_added": 0,
                "error": "",
            },
            repo_facts=None,
            skipped_reason=(
                "no_binding_allowed_model_rows"
                if not model_rows
                else "no_eligible_import_origin_backed_loader_sinks"
            ),
        )
        write_sink_outputs(output_dir, [], [])
        return 0

    repo_facts = parse_repo(repo, syntax_cache=args.syntax_cache)
    identity_vocab = build_identity_vocabulary(args.reuse_codebook, loader_rows)
    # The builder connects source values to calls with verified import origins.
    builder = SourceSinkBuilder(
        repo_facts,
        model_rows,
        loader_rows,
        identity_vocab,
        enable_jedi=args.enable_jedi,
        reuse_codebook=args.reuse_codebook,
        framework_config_summaries=args.framework_config_summaries,
    )
    builder.build()
    analysis_status = run_analysis_checks(args, repo, model_rows, loader_rows)

    traces, steps, checks, binding_steps = trace_sources_to_sinks(
        repo_snapshot=args.repo_snapshot or infer_repo_snapshot(args.output_dir),
        builder=builder,
        analysis_status=analysis_status,
        sink_mode=args.sink_mode,
    )
    write_sink_outputs(output_dir, checks, binding_steps)
    checker = getattr(builder, "_mock_checker", None)
    (output_dir / "mock_scope_audit.json").write_text(json.dumps({
        "engine": "MIST",
        "checks": checker.audit if checker else [],
    }, indent=2) + "\n")

    write_csv(
        output_dir / "binding_source_sink_traces.csv",
        [asdict(row) for row in traces],
        quote_all=True,
        clean_values=True,
        ensure_parent=True,
    )
    write_csv(
        output_dir / "binding_trace_steps.csv",
        [asdict(row) for row in steps],
        quote_all=True,
        clean_values=True,
        ensure_parent=True,
    )
    write_graph_edges(output_dir / "binding_trace_graph_edges.csv", builder.graph)
    write_binding_summary(
        args=args,
        output_dir=output_dir,
        repo=repo,
        model_rows=model_rows,
        loader_rows=loader_rows,
        traces=traces,
        graph=builder.graph,
        analysis_status=analysis_status,
        identity_vocab=identity_vocab,
        jedi_summary={
            "enabled": args.enable_jedi,
            "resolved_files_added": len(builder.jedi_added_rel_paths),
            "symbol_resolution_edges_added": builder.jedi_resolution_edges_added,
            "internal_loader_sinks_added": builder.jedi_internal_sinks_added,
            "error": builder.jedi_error,
        },
        repo_facts=repo_facts,
        skipped_reason="",
    )
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trace exact model-ID sources to resolved loader sinks.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--model-id-contexts", type=Path, required=True)
    parser.add_argument("--loader-candidates", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reuse-codebook", type=Path, default=DATA_DIR / "reuse_rules.xlsx")
    parser.add_argument(
        "--framework-config-summaries",
        type=Path,
        default=DATA_DIR / "frameworks.json",
        help="Declarative framework selector, persistence, and state-read summaries.",
    )
    parser.add_argument("--repo-snapshot", default="")
    parser.add_argument("--syntax-cache", type=Path, help="Optional syntax cache directory.")
    parser.add_argument("--enable-jedi", action="store_true", help="Try Jedi parsing as helper evidence.")
    parser.add_argument("--sink-mode", choices=SINK_MODES, default='shortest')
    parser.add_argument("--max-diagnostic-files", type=int, default=40)
    return parser.parse_args(argv)
