"""Trace PTM ID occurrences to eligible sinks and check mock context."""

from __future__ import annotations

from collections import deque
from mist.bindings.locations import summary_locations
from mist.bindings.locations import unresolved_summary
from mist.bindings.selection import check_sinks, selected_check
from mist.io import parse_bool
from mist.io import parse_int
import json
import networkx as nx
import re
from mist.bindings.constants import (
    GENERIC_HTTP_ORIGINS,
    HTTP_MODEL_ENDPOINT_TERMS,
    HTTP_WEAK_ENDPOINT_TERMS,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mist.bindings.graph import SourceSinkBuilder
from mist.bindings.syntax import (
    location_url_from_loader,
    node_file_part,
    split_identifier,
)
from mist.bindings.types import (
    IdentityVocabulary,
    AnalysisStatus,
    SinkInfo,
    TraceResult,
    TraceStep,
)


def make_pregraph_traces(
    *,
    repo_snapshot: str,
    model_rows: list[dict[str, str]],
    analysis_status: AnalysisStatus,
) -> tuple[list[TraceResult], list[TraceStep]]:
    traces: list[TraceResult] = []
    steps: list[TraceStep] = []
    for index, row in enumerate(model_rows, start=1):
        trace_id = f"trace_{index:05d}"
        source_node = f"source:{row.get('model_id_occurrence_id') or index}"
        status = "unresolved_missing_loader_candidate"
        reason = "source is eligible for tracing, but no eligible import-origin-backed loader sink was found"
        traces.append(
            make_trace_result(
                trace_id=trace_id,
                repo_snapshot=repo_snapshot,
                graph=None,
                source_row=row,
                sink=None,
                status=status,
                confidence="unresolved",
                kind=status,
                path=[source_node],
                analysis_status=analysis_status,
                reason=reason,
            )
        )
        steps.append(
            TraceStep(
                trace_id=trace_id,
                step_index=1,
                step_type="model_id_source",
                file_path=row.get("file_path", ""),
                line_number=row.get("line_number", ""),
                node_id=source_node,
                evidence=f"exact model ID source {row.get('matched_text', '')!r}",
            )
        )
    return traces, steps


def binding_mock_context(builder, source_row, sink):
    from mist import bindings
    from mist.mocks.decision import binding_context
    return binding_context(bindings, builder, source_row, sink)


def trace_sources_to_sinks(
    *,
    repo_snapshot: str,
    builder: SourceSinkBuilder,
    analysis_status: AnalysisStatus,
    sink_mode: str = 'shortest',
) -> tuple[list[TraceResult], list[TraceStep], list[TraceResult], list[TraceStep]]:
    traces: list[TraceResult] = []
    steps: list[TraceStep] = []
    checks: list[TraceResult] = []
    binding_steps: list[TraceStep] = []
    primary_sinks = [
        sink
        for sink in builder.sinks.values()
        if is_primary_loader_candidate(sink.loader_row.get("loader_candidate_id", ""))
    ]
    primary_sink_nodes = [sink.sink_node_id for sink in primary_sinks]
    trace_index = 0

    for source in builder.sources.values():
        trace_index += 1
        trace_id = f"trace_{trace_index:05d}"
        row = source.row
        path = shortest_path_to_any_sink(builder.graph, source.node_id, primary_sink_nodes)
        if path:
            checked = check_sinks(builder.graph, source.node_id, path, primary_sinks,
                                  lambda sink: binding_mock_context(builder, row, sink), sink_mode)
            for index, (sink, sink_path, context) in enumerate(checked, start=1):
                check_id = f"check_{trace_index:05d}_{index:05d}"
                # The gated enumeration uses a per-sink shortest path for export.
                if sink_mode == 'confirmed-all' and checked[0][2].state == 'clear':
                    sink_path = nx.shortest_path(builder.graph, source.node_id, sink.sink_node_id)
                result = checked_trace(check_id, repo_snapshot, builder, row, sink,
                                       sink_path, context, analysis_status)
                checks.append(result)
                if result.trace_status == 'confirmed_real_reuse':
                    binding_steps.extend(steps_for_path(check_id, builder, sink_path))
            sink, path, context = selected_check(checked)
            traces.append(checked_trace(
                trace_id, repo_snapshot, builder, row, sink, path, context, analysis_status)
            )
            steps.extend(steps_for_path(trace_id, builder, path))
            continue

        nearest_loader_id = row.get("nearest_loader_candidate_id", "")
        sink = builder.sinks.get(nearest_loader_id) if nearest_loader_id else None
        if sink:
            status = "unresolved_nearest_loader_no_value_trace"
            reason = "a nearby loader exists, but no source-to-sink value path was found"
        elif primary_sinks:
            status = "unresolved_no_source_sink_path"
            reason = "resolved loader sinks exist in the repo, but none are reached by this exact source"
        else:
            status = "unresolved_missing_loader_candidate"
            reason = "source is eligible for tracing, but no eligible import-origin-backed loader sink was found"
        traces.append(
            make_trace_result(
                trace_id=trace_id,
                repo_snapshot=repo_snapshot,
                graph=builder.graph,
                source_row=row,
                sink=sink,
                status=status,
                confidence="unresolved",
                kind=status,
                path=[source.node_id],
                analysis_status=analysis_status,
                reason=reason,
            )
        )
        steps.append(
            TraceStep(
                trace_id,
                1,
                "model_id_source",
                row.get("file_path", ""),
                row.get("line_number", ""),
                source.node_id,
                f"exact model ID source {source.literal_value!r}",
            )
        )
    return traces, steps, checks, binding_steps


def checked_trace(trace_id, repo_snapshot, builder, row, sink, path, context, analysis_status):
    if context.state == 'mocked':
        status, confidence, kind = 'fp_mock_or_monkeypatched_loader', 'excluded', 'mocked_loader_path'
        reason = 'verified mock in this calling context: ' + json.dumps(context.evidence, sort_keys=True)
    elif context.state == 'unresolved':
        status, confidence, kind = 'unresolved_mock_context', 'unresolved', 'unresolved_mock_context'
        reason = 'mock context not established: ' + json.dumps(context.evidence, sort_keys=True)
    else:
        status = 'confirmed_real_reuse'
        confidence = confidence_for_path(builder.graph, path)
        kind = kind_for_path(builder.graph, path)
        reason = 'exact model-ID source reaches resolved loader model sink'
    return make_trace_result(trace_id=trace_id, repo_snapshot=repo_snapshot,
        graph=builder.graph, source_row=row, sink=sink, status=status,
        confidence=confidence, kind=kind, path=path, analysis_status=analysis_status, reason=reason)


def is_primary_loader_candidate(loader_candidate_id: str) -> bool:
    return loader_candidate_id.startswith("loader_candidate_")


def binding_loader_candidate_is_eligible(row: dict[str, str]) -> bool:
    """Keep only import-origin-backed sinks that binding tracing can safely trace to."""
    if not is_primary_loader_candidate(row.get("loader_candidate_id", "")):
        return False
    if row.get("call_sink_eligible", "") and not parse_bool(row.get("call_sink_eligible", "")):
        return False
    receiver_origin = row.get("receiver_origin", "")
    if not receiver_origin:
        return False
    matched_origins = split_pipe_values(row.get("matched_rule_import_origin", ""))
    if matched_origins and receiver_origin not in matched_origins:
        return False
    linked_origins = split_pipe_values(row.get("linked_import_origin", ""))
    if linked_origins and receiver_origin not in linked_origins:
        return False

    confidence = row.get("confidence", "").lower()
    if confidence == "high":
        return True
    if confidence != "medium":
        return False

    origins = split_pipe_values(row.get("linked_import_origin", ""))
    if origins and origins <= GENERIC_HTTP_ORIGINS:
        return False
    if not (parse_bool(row.get("has_model_arg")) or parse_bool(row.get("has_model_payload"))):
        return False

    suffixes = split_pipe_values(row.get("matched_chain_suffix", ""))
    return any("." in suffix for suffix in suffixes)


def split_pipe_values(value: str) -> set[str]:
    return {part.strip() for part in value.split("|") if part.strip()}


def url_text_has_model_endpoint(value: str, identity_vocab: IdentityVocabulary) -> bool:
    """Recognize HTTP URLs that point at model-serving endpoint shapes."""
    text = str(value or "").lower()
    if not text:
        return False
    compact = re.sub(r"[^a-z0-9]+", "", text)
    terms = split_identifier(text)
    strong_terms = HTTP_MODEL_ENDPOINT_TERMS | (
        set(identity_vocab.endpoint_terms) - {"open", "post", "request", "send", "stream", "urlopen"}
    )
    if "chat" in terms and "completions" in terms:
        return True
    if "generatecontent" in compact:
        return True
    if terms & HTTP_MODEL_ENDPOINT_TERMS:
        return True
    if len(terms & strong_terms) >= 2:
        return True
    if (terms & HTTP_WEAK_ENDPOINT_TERMS) and (terms & strong_terms):
        return True
    return False


def make_trace_result(
    *,
    trace_id: str,
    repo_snapshot: str,
    graph: nx.DiGraph | None = None,
    source_row: dict[str, str],
    sink: SinkInfo | None,
    status: str,
    confidence: str,
    kind: str,
    path: list[str],
    analysis_status: AnalysisStatus,
    reason: str,
) -> TraceResult:
    loader_row = sink.loader_row if sink else {}
    edge_types = path_edge_types(graph, path)
    locations = summary_locations(graph, path)
    interprocedural = any(
        "param" in edge or "return" in edge or "call_" in edge
        or edge == "outer_scope_value_to_free_variable"
        for edge in edge_types
    ) or len({(loc["file_path"], loc["procedure"]) for loc in locations if loc["procedure"]}) > 1
    interfile = path_uses_interfile(path, graph)
    locality = "unresolved"
    if status == "confirmed_real_reuse":
        if interfile or interprocedural:
            locality = "non_local"
        elif not unresolved_summary(graph, path):
            locality = "local"
    return TraceResult(
        trace_id=trace_id,
        model_id_occurrence_id=source_row.get("model_id_occurrence_id", ""),
        loader_candidate_id=loader_row.get("loader_candidate_id", ""),
        repo_snapshot=repo_snapshot,
        file_path=source_row.get("file_path", ""),
        model_line_number=parse_int(source_row.get("line_number")),
        loader_file_path=loader_row.get("file_path", ""),
        loader_line_number=loader_row.get("line_number", ""),
        model_location_url=location_url_from_loader(loader_row, source_row),
        loader_location_url=loader_row.get("location_url", ""),
        canonical_model_id=source_row.get("canonical_model_id", ""),
        matched_text=source_row.get("matched_text", ""),
        model_ast_context=source_row.get("ast_context", ""),
        direct_carrier_name=source_row.get("direct_carrier_name", ""),
        loader_call=loader_row.get("visible_call_chain", ""),
        loader_origin=loader_row.get("linked_import_origin", ""),
        trace_status=status,
        trace_confidence=confidence,
        trace_kind=kind,
        path_length=str(max(0, len(path) - 1)) if path else "",
        used_source_sink_graph=status in {"confirmed_real_reuse", "fp_mock_or_monkeypatched_loader", "unresolved_mock_context"},
        used_interprocedural_edges=interprocedural,
        used_interfile_edges=interfile,
        used_ui_flow_edges=any(
            edge.startswith("ui_")
            or edge in {"framework_selector_choice_set", "framework_form_value_persisted"}
            for edge in edge_types
        ),
        jedi_status=analysis_status.jedi_status,
        reason=reason,
        binding_locality=locality,
    )


def path_edge_types(graph: nx.DiGraph | None, path: list[str]) -> list[str]:
    if graph is None:
        return []
    return [
        str(graph.edges[path[index], path[index + 1]].get("edge_type", ""))
        for index in range(len(path) - 1)
        if graph.has_edge(path[index], path[index + 1])
    ]


def steps_for_path(trace_id: str, builder: SourceSinkBuilder, path: list[str]) -> list[TraceStep]:
    steps: list[TraceStep] = []
    for index, node in enumerate(path, start=1):
        meta = builder.graph.nodes.get(node, {})
        if index == 1:
            edge_type = "source"
            evidence = f"source node {meta.get('label', '')!r}"
        else:
            prev = path[index - 2]
            edge = builder.graph.edges.get((prev, node), {})
            edge_type = edge.get("edge_type", "value_flow")
            evidence = edge.get("evidence", "")
            for location in summary_locations(builder.graph, [prev, node]):
                steps.append(TraceStep(
                    trace_id=trace_id,
                    step_index=len(steps) + 1,
                    step_type=location["step_type"],
                    file_path=location["file_path"],
                    line_number=str(location["line_number"]),
                    node_id=(f"summary_location|{location['file_path']}|"
                             f"{location['line_number']}|{location['scope']}|{location['step_type']}"),
                    evidence=location["evidence"],
                    procedure=location["procedure"],
                    end_line_number=str(location["end_line_number"]),
                    scope=location["scope"],
                    record_kind="summary_location",
                ))
        steps.append(
            TraceStep(
                trace_id=trace_id,
                step_index=len(steps) + 1,
                step_type=edge_type,
                file_path=str(meta.get("file_path", "")),
                line_number=str(meta.get("line_number", "")),
                node_id=node,
                evidence=evidence,
            )
        )
    return steps


def shortest_path_to_any_sink(graph: nx.DiGraph, source: str, sinks: list[str]) -> list[str]:
    if source not in graph:
        return []
    sink_set = set(sinks)
    visited = {source}
    queue: deque[list[str]] = deque([[source]])
    while queue:
        path = queue.popleft()
        node = path[-1]
        if node in sink_set:
            return path
        for child in graph.successors(node):
            if child in visited:
                continue
            visited.add(child)
            queue.append(path + [child])
    return []


def confidence_for_path(graph: nx.DiGraph, path: list[str]) -> str:
    if len(path) <= 2:
        return "high"
    edge_types = [graph.edges[path[i], path[i + 1]].get("edge_type", "") for i in range(len(path) - 1)]
    if any(edge.startswith("ui_") for edge in edge_types):
        return "medium"
    if any("project_import" in edge for edge in edge_types):
        return "high"
    if any("call_" in edge or "return" in edge for edge in edge_types):
        return "high"
    return "high"


def kind_for_path(graph: nx.DiGraph, path: list[str]) -> str:
    edge_types = [graph.edges[path[i], path[i + 1]].get("edge_type", "") for i in range(len(path) - 1)]
    if any(edge.startswith("framework_") for edge in edge_types):
        return "framework_config_value_flow"
    if any(edge.startswith("ui_") for edge in edge_types):
        return "registry_ui_value_flow"
    if any("payload" in edge or "kwargs" in edge for edge in edge_types):
        return "payload_or_kwargs_value_flow"
    if any(edge.startswith("adapter_config_registry") for edge in edge_types):
        return "adapter_config_registry_value_flow"
    if any("call_" in edge or "return" in edge for edge in edge_types):
        return "interprocedural_value_flow"
    if any("field" in node for node in path):
        return "class_field_value_flow"
    return "direct_value_flow"


def path_uses_interfile(path: list[str], graph: nx.DiGraph | None = None) -> bool:
    files = {node_file_part(node) for node in path if node_file_part(node)}
    files.update(location["file_path"] for location in summary_locations(graph, path))
    return len(files) > 1
