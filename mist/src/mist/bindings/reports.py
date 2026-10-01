"""Write trace, graph, and summary evidence."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, fields
import csv
from datetime import datetime
from mist.io import count_values
from mist.io import display_output_path
from mist.io import write_csv
from pathlib import Path
import argparse
import json
import networkx as nx
from mist.bindings.constants import (
    FRAMEWORK_CONFIG_EDGE_TYPES,
)
from mist.bindings.types import (
    IdentityVocabulary,
    AnalysisStatus,
    RepoFacts,
    TraceResult,
    TraceStep,
)


def write_sink_outputs(output_dir, checks, binding_steps):
    confirmed = [row for row in checks if row.trace_status == 'confirmed_real_reuse']
    for name, rows, record in (
        ('binding_sink_checks.csv', checks, TraceResult),
        ('binding_confirmed_bindings.csv', confirmed, TraceResult),
        ('binding_confirmed_steps.csv', binding_steps, TraceStep),
    ):
        with (output_dir / name).open('w', encoding='utf-8', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=[field.name for field in fields(record)])
            writer.writeheader()
            writer.writerows(asdict(row) for row in rows)


def write_binding_outputs(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    repo: Path,
    model_rows: list[dict[str, str]],
    loader_rows: list[dict[str, str]],
    traces: list[TraceResult],
    steps: list[TraceStep],
    graph: nx.DiGraph,
    analysis_status: AnalysisStatus,
    identity_vocab: IdentityVocabulary,
    jedi_summary: dict[str, object],
    repo_facts: RepoFacts | None,
    skipped_reason: str,
) -> None:
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
    write_graph_edges(output_dir / "binding_trace_graph_edges.csv", graph)
    write_binding_summary(
        args=args,
        output_dir=output_dir,
        repo=repo,
        model_rows=model_rows,
        loader_rows=loader_rows,
        traces=traces,
        graph=graph,
        analysis_status=analysis_status,
        identity_vocab=identity_vocab,
        jedi_summary=jedi_summary,
        repo_facts=repo_facts,
        skipped_reason=skipped_reason,
    )


def write_binding_summary(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    repo: Path,
    model_rows: list[dict[str, str]],
    loader_rows: list[dict[str, str]],
    traces: list[TraceResult],
    graph: nx.DiGraph,
    analysis_status: AnalysisStatus,
    identity_vocab: IdentityVocabulary,
    jedi_summary: dict[str, object],
    repo_facts: RepoFacts | None,
    skipped_reason: str,
) -> None:
    repo_files = repo_facts.files if repo_facts else {}

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "model_id_contexts": display_output_path(args.model_id_contexts),
        "loader_candidates": display_output_path(args.loader_candidates),
        "syntax_cache": str(args.syntax_cache) if args.syntax_cache else "",
        "binding_allowed_model_rows": len(model_rows),
        "eligible_loader_candidates": len(loader_rows),
        "eligible_loader_confidence_counts": count_values(loader_rows, "confidence"),
        "skipped_reason": skipped_reason,
        "jedi_graph_evidence": jedi_summary,
        "framework_config_graph_evidence": framework_config_summary(args, graph),
        "repo_python_files": len(repo_files),
        "repo_parse_error_files": {
            rel_path: facts.parse_error
            for rel_path, facts in repo_files.items()
            if facts.parse_error
        },
        "source_sink_graph_nodes": graph.number_of_nodes(),
        "source_sink_graph_edges": graph.number_of_edges(),
        "trace_rows": len(traces),
        "trace_status_counts": count_values(traces, "trace_status"),
        "trace_confidence_counts": count_values(traces, "trace_confidence"),
        "trace_kind_counts": count_values(traces, "trace_kind"),
        "analysis_status": asdict(analysis_status),
        "identity_vocabulary": identity_vocab.summary(),
        "outputs": {
            "traces": display_output_path(output_dir / "binding_source_sink_traces.csv"),
            "trace_steps": display_output_path(output_dir / "binding_trace_steps.csv"),
            "trace_graph_edges": display_output_path(output_dir / "binding_trace_graph_edges.csv"),
            "summary": display_output_path(output_dir / "binding_summary.json"),
        },
    }
    (output_dir / "binding_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def framework_config_summary(args: argparse.Namespace, graph: nx.DiGraph) -> dict[str, object]:
    edge_counts: dict[str, int] = defaultdict(int)
    for _, _, data in graph.edges(data=True):
        edge_type = str(data.get("edge_type", ""))
        if edge_type in FRAMEWORK_CONFIG_EDGE_TYPES:
            edge_counts[edge_type] += 1
    summary_path = args.framework_config_summaries
    return {
        "enabled": summary_path is not None,
        "summary_path": str(summary_path) if summary_path else "",
        "summary_exists": bool(summary_path and summary_path.is_file()),
        "config_channel_count": sum(
            1 for node in graph.nodes if str(node).startswith("framework_state|")
        ),
        "overlay_edge_count": sum(edge_counts.values()),
        "edge_type_counts": dict(sorted(edge_counts.items())),
    }


def write_graph_edges(path: Path, graph: nx.DiGraph) -> None:
    rows = []
    for source, target, data in sorted(graph.edges(data=True), key=lambda edge: (edge[0], edge[1], str(edge[2]))):
        rows.append({
            "source": source,
            "target": target,
            "edge_type": data.get("edge_type", ""),
            "evidence": data.get("evidence", ""),
            "summary_location_status": data.get("summary_location_status", ""),
            "summary_locations": json.dumps(data.get("summary_locations", []), sort_keys=True),
        })
    write_csv(path, rows, quote_all=True, clean_values=True, ensure_parent=True)
