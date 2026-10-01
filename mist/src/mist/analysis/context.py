#!/usr/bin/env python
"""Classify the source context of each PTM ID occurrence before tracing."""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from mist.analysis.parse_cache import ParsedPythonFile, load_syntax_python_file_map, parse_python_file
from mist.io import count_values, display_output_path, parse_bool, read_csv, strip_quotes, write_csv
from mist.rules.context_terms import CONTEXT_CLI_COMMAND_TARGET_TERMS, CONTEXT_DESCRIPTIVE_TERMS, CONTEXT_FUNCTION_DEFAULT_MODEL_TERMS, CONTEXT_METADATA_TERMS, CONTEXT_MODEL_CARRIER_TERMS, CONTEXT_MODEL_ID_VALIDATION_CALLS, CONTEXT_MODEL_ID_VALIDATION_METHODS, CONTEXT_SHORT_AMBIGUOUS_OPENAI_MODEL_IDS


MODEL_CARRIER_TERMS = CONTEXT_MODEL_CARRIER_TERMS
METADATA_TERMS = CONTEXT_METADATA_TERMS
DESCRIPTIVE_TERMS = CONTEXT_DESCRIPTIVE_TERMS
MODEL_ID_VALIDATION_CALLS = CONTEXT_MODEL_ID_VALIDATION_CALLS
MODEL_ID_VALIDATION_METHODS = CONTEXT_MODEL_ID_VALIDATION_METHODS
FUNCTION_DEFAULT_MODEL_TERMS = CONTEXT_FUNCTION_DEFAULT_MODEL_TERMS
CLI_COMMAND_TARGET_TERMS = CONTEXT_CLI_COMMAND_TARGET_TERMS
SHORT_AMBIGUOUS_OPENAI_MODEL_IDS = CONTEXT_SHORT_AMBIGUOUS_OPENAI_MODEL_IDS


@dataclass(frozen=True)
class LoaderContext:
    count: int = 0
    nearest_line: str = ""
    nearest_call: str = ""
    nearest_distance: str = ""
    nearest_candidate_id: str = ""


@dataclass(frozen=True)
class ModelIdContext:
    model_id_occurrence_id: str
    canonical_model_id: str
    matched_text: str
    variant: str
    file_path: str
    line_number: int
    column_start: int
    column_end: int
    line_text: str
    source_context: str
    path_signal: str
    occurrence_fp_signal: str
    occurrence_excluded_from_main_queue: bool
    ast_context: str
    direct_carrier_kind: str
    direct_carrier_name: str
    carrier_container_name: str
    function_context: str
    class_context: str
    surrounding_context_terms: str
    same_file_loader_candidates: int
    nearest_loader_candidate_id: str
    nearest_loader_line: str
    nearest_loader_call: str
    nearest_loader_distance: str
    preliminary_label: str
    trace_priority: str
    binding_allowed: bool
    reason: str


@dataclass(frozen=True)
class AstContext:
    ast_context: str = "unknown_string_context"
    direct_carrier_kind: str = ""
    direct_carrier_name: str = ""
    carrier_container_name: str = ""
    function_context: str = ""
    class_context: str = ""
    surrounding_context_terms: str = ""
    reason: str = ""


def main(argv: Sequence[str] | None = None) -> int:
    """Add local AST/context features to identifier extraction model-ID occurrences."""
    args = parse_args(argv)
    repo = args.repo.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    occurrence_rows = read_csv(args.model_occurrences)
    loader_rows = read_csv(args.loader_candidates, missing_ok=True) if args.loader_candidates else []
    eligible_loader_rows = [row for row in loader_rows if call_loader_is_eligible(row)]
    loader_contexts = load_loader_contexts(eligible_loader_rows)
    eligible_loader_count = len(eligible_loader_rows)
    contexts: list[ModelIdContext] = []
    parse_cache: dict[
        str,
        tuple[ast.AST | None, dict[ast.AST, tuple[ast.AST, str, int | None]], tuple[ast.AST, ...]],
    ] = {}
    syntax_files = (
        load_syntax_file_map(repo, args.syntax_cache.resolve(), occurrence_rows)
        if args.syntax_cache
        else {}
    )
    parse_errors = 0

    for index, row in enumerate(occurrence_rows, start=1):
        rel_path = row["file_path"]
        source_context = row.get("source_context", "code")
        ast_context = AstContext(ast_context=source_context, reason=f"source_context={source_context}")
        if source_context == "code":
            # Parse each file once; many model IDs can live in the same file.
            tree, parents, nodes = parse_cache.get(rel_path, (None, {}, ()))
            if rel_path not in parse_cache:
                tree, parents, nodes, failed = parse_file(repo / rel_path, syntax_files.get(rel_path))
                parse_cache[rel_path] = (tree, parents, nodes)
                parse_errors += int(failed)
            if tree is not None:
                ast_context = classify_ast_context(row, tree, parents, nodes)
        # Same-file loader distance is only context here, not proof of reuse.
        loader_context = nearest_loader_context(row, loader_contexts.get(rel_path, []))
        label, priority, binding_allowed, label_reason = classify_label(
            row,
            ast_context,
            loader_context,
            eligible_loader_count,
        )
        contexts.append(
            ModelIdContext(
                model_id_occurrence_id=f"model_id_occurrence_{index:05d}",
                canonical_model_id=row["canonical_model_id"],
                matched_text=row["matched_text"],
                variant=row["variant"],
                file_path=rel_path,
                line_number=int(row["line_number"]),
                column_start=int(row["column_start"]),
                column_end=int(row["column_end"]),
                line_text=row["line_text"],
                source_context=source_context,
                path_signal=row.get("path_signal", ""),
                occurrence_fp_signal=row.get("fp_signal", ""),
                occurrence_excluded_from_main_queue=parse_bool(row.get("excluded_from_main_queue", "")),
                ast_context=ast_context.ast_context,
                direct_carrier_kind=ast_context.direct_carrier_kind,
                direct_carrier_name=ast_context.direct_carrier_name,
                carrier_container_name=ast_context.carrier_container_name,
                function_context=ast_context.function_context,
                class_context=ast_context.class_context,
                surrounding_context_terms=ast_context.surrounding_context_terms,
                same_file_loader_candidates=loader_context.count,
                nearest_loader_candidate_id=loader_context.nearest_candidate_id,
                nearest_loader_line=loader_context.nearest_line,
                nearest_loader_call=loader_context.nearest_call,
                nearest_loader_distance=loader_context.nearest_distance,
                preliminary_label=label,
                trace_priority=priority,
                binding_allowed=binding_allowed,
                reason="; ".join(part for part in [ast_context.reason, label_reason] if part),
            )
        )

    write_csv(output_dir / "model_id_contexts.csv", [asdict(row) for row in contexts])
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "model_occurrences": str(args.model_occurrences),
        "loader_candidates": str(args.loader_candidates) if args.loader_candidates else "",
        "eligible_loader_candidates": eligible_loader_count,
        "syntax_cache": str(args.syntax_cache) if args.syntax_cache else "",
        "model_id_occurrences_loaded": len(occurrence_rows),
        "python_files_parsed": len(parse_cache),
        "python_parse_errors": parse_errors,
        "label_counts": count_values(contexts, "preliminary_label"),
        "trace_priority_counts": count_values(contexts, "trace_priority"),
        "binding_allowed_counts": count_values(contexts, "binding_allowed"),
        "ast_context_counts": count_values(contexts, "ast_context"),
        "direct_carrier_kind_counts": count_values(contexts, "direct_carrier_kind"),
        "outputs": {
            "model_id_contexts": display_output_path(output_dir / "model_id_contexts.csv", resolve=True),
            "summary": display_output_path(output_dir / "context_summary.json", resolve=True),
        },
    }
    (output_dir / "context_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract source context analysis model-ID local context features.")
    parser.add_argument("--repo", type=Path, required=True, help="Repository snapshot root.")
    parser.add_argument(
        "--model-occurrences",
        type=Path,
        required=True,
        help="occurrence discovery model_id_occurrences.csv.",
    )
    parser.add_argument(
        "--loader-candidates",
        type=Path,
        default=None,
        help="Optional call analysis loader_candidates.csv for same-file loader context.",
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="Output directory.")
    parser.add_argument("--syntax-cache", type=Path, help="Optional syntax cache directory.")
    return parser.parse_args(argv)


def load_syntax_file_map(
    repo: Path,
    cache_dir: Path,
    occurrence_rows: list[dict[str, str]],
) -> dict[str, ParsedPythonFile]:
    """Load only cached files that can need source context analysis AST context."""
    rel_paths = {
        row.get("file_path", "")
        for row in occurrence_rows
        if row.get("source_context", "code") == "code"
    }
    return load_syntax_python_file_map(
        repo,
        cache_dir,
        rel_paths,
        collect_nodes=True,
        collect_parent_links=True,
    )


def parse_file(
    path: Path,
    cached: ParsedPythonFile | None = None,
) -> tuple[ast.AST | None, dict[ast.AST, tuple[ast.AST, str, int | None]], tuple[ast.AST, ...], bool]:
    parsed = cached or parse_python_file(path, collect_nodes=True, collect_parent_links=True)
    return parsed.tree, parsed.parent_links, parsed.nodes, parsed.parse_failed


def classify_ast_context(
    row: dict[str, str],
    tree: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
    nodes: tuple[ast.AST, ...],
) -> AstContext:
    node = find_string_node(row, nodes)
    function_context, class_context = enclosing_context(row, nodes)
    if node is None:
        return AstContext(
            ast_context="string_literal_not_resolved",
            function_context=function_context,
            class_context=class_context,
            surrounding_context_terms=context_terms(row, "", "", function_context, class_context),
            reason="no AST string node overlaps exact occurrence discovery span",
        )

    dict_info = nearest_dict_value(node, parents)
    if dict_info is not None:
        key_name, container = dict_info
        return make_context(
            row,
            "dict_value",
            "dict_key",
            key_name,
            container_name=container,
            function_context=function_context,
            class_context=class_context,
            reason=f"string literal is a value for dict key {key_name or '<unknown>'}",
        )

    keyword = nearest_keyword(node, parents)
    if keyword is not None:
        name = keyword.arg or "**kwargs"
        return make_context(
            row,
            "call_keyword_arg",
            "call_keyword",
            name,
            container_name=nearest_call_name(node, parents),
            function_context=function_context,
            class_context=class_context,
            reason=f"string literal is assigned to call keyword {name}",
        )

    default_name = nearest_function_default(node, parents)
    if default_name:
        return make_context(
            row,
            "function_default",
            "function_parameter",
            default_name,
            function_context=function_context,
            class_context=class_context,
            reason=f"string literal is a default value for parameter {default_name}",
        )

    collection_container = nearest_collection_assignment(node, parents)
    if collection_container:
        return make_context(
            row,
            "collection_item",
            "collection_assignment_target",
            collection_container,
            function_context=function_context,
            class_context=class_context,
            reason=f"string literal is an item in collection assigned to {collection_container}",
        )

    compare_name = nearest_compare_context(node, parents)
    if compare_name:
        return make_context(
            row,
            "comparison_value",
            "comparison_operand",
            compare_name,
            function_context=function_context,
            class_context=class_context,
            reason=f"string literal is used in a comparison against {compare_name}",
        )

    assignment_name = nearest_assignment_target(node, parents)
    if assignment_name:
        kind = "class_attribute" if "." in assignment_name else "assignment_value"
        carrier_kind = "attribute_assignment_target" if "." in assignment_name else "assignment_target"
        return make_context(
            row,
            kind,
            carrier_kind,
            assignment_name,
            function_context=function_context,
            class_context=class_context,
            reason=f"string literal is assigned to {assignment_name}",
        )

    call_name, positional_param, helper_feeds_model = nearest_positional_call_info(node, parents)
    if call_name:
        carrier_kind = "call_positional_arg_model_param" if helper_feeds_model else "call_positional_arg"
        reason = f"string literal is a positional argument to {call_name}"
        if positional_param:
            reason += f" parameter {positional_param}"
        if helper_feeds_model:
            reason += " that feeds a model keyword in the local helper"
        return make_context(
            row,
            "call_positional_arg",
            carrier_kind,
            call_name,
            container_name=positional_param,
            function_context=function_context,
            class_context=class_context,
            reason=reason,
        )

    if nearest_node(node, parents, ast.Return):
        return make_context(
            row,
            "return_value",
            "return_value",
            "return",
            function_context=function_context,
            class_context=class_context,
            reason="string literal is returned by a function",
        )

    return make_context(
        row,
        "standalone_string_literal",
        "string_literal",
        "",
        function_context=function_context,
        class_context=class_context,
        reason="string literal resolved but no direct carrier was recognized",
    )


def make_context(
    row: dict[str, str],
    ast_context: str,
    direct_carrier_kind: str,
    direct_carrier_name: str,
    *,
    container_name: str = "",
    function_context: str = "",
    class_context: str = "",
    reason: str = "",
) -> AstContext:
    return AstContext(
        ast_context=ast_context,
        direct_carrier_kind=direct_carrier_kind,
        direct_carrier_name=direct_carrier_name,
        carrier_container_name=container_name,
        function_context=function_context,
        class_context=class_context,
        surrounding_context_terms=context_terms(
            row,
            direct_carrier_name,
            container_name,
            function_context,
            class_context,
        ),
        reason=reason,
    )


def find_string_node(row: dict[str, str], nodes: tuple[ast.AST, ...]) -> ast.Constant | None:
    line_number = int(row["line_number"])
    start_col = int(row["column_start"]) - 1
    end_col = int(row["column_end"])
    matched_value = strip_quotes(row["matched_text"])
    candidates = []
    for node in nodes:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not has_location(node):
            continue
        if not span_contains(node, line_number, start_col, end_col):
            continue
        score = 0
        if str(node.value) == matched_value:
            score += 3
        elif matched_value in str(node.value):
            score += 1
        score += max(0, 200 - abs(node.lineno - line_number))
        candidates.append((score, node))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0], reverse=True)[0][1]


def has_location(node: ast.AST) -> bool:
    return all(hasattr(node, attr) for attr in ("lineno", "col_offset", "end_lineno", "end_col_offset"))


def span_contains(node: ast.AST, line_number: int, start_col: int, end_col: int) -> bool:
    if line_number < node.lineno or line_number > node.end_lineno:
        return False
    effective_start = node.col_offset if line_number == node.lineno else 0
    effective_end = node.end_col_offset if line_number == node.end_lineno else 10**9
    return start_col >= effective_start and end_col <= effective_end


def nearest_keyword(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> ast.keyword | None:
    current = node
    while current in parents:
        parent, field, _ = parents[current]
        if isinstance(parent, ast.keyword) and field == "value":
            return parent
        current = parent
    return None


def nearest_dict_value(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> tuple[str, str] | None:
    current = node
    while current in parents:
        parent, field, index = parents[current]
        if isinstance(parent, ast.Dict) and field == "values" and index is not None:
            key_name = expr_label(parent.keys[index]) if index < len(parent.keys) else ""
            return key_name, nearest_assignment_target(parent, parents)
        current = parent
    return None


def nearest_function_default(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> str:
    current = node
    while current in parents:
        parent, field, index = parents[current]
        if isinstance(parent, ast.arguments) and field in {"defaults", "kw_defaults"} and index is not None:
            if field == "defaults":
                args = parent.args[-len(parent.defaults):] if parent.defaults else []
                if index < len(args):
                    return args[index].arg
            else:
                kwonlyargs = parent.kwonlyargs
                if index < len(kwonlyargs):
                    return kwonlyargs[index].arg
        current = parent
    return ""


def nearest_collection_assignment(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> str:
    current = node
    while current in parents:
        parent, _, _ = parents[current]
        if isinstance(parent, (ast.List, ast.Tuple, ast.Set)):
            target = nearest_assignment_target(parent, parents)
            if target:
                return target
        current = parent
    return ""


def nearest_compare_context(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> str:
    current = node
    while current in parents:
        parent, field, index = parents[current]
        if isinstance(parent, ast.Compare):
            if field == "comparators":
                return expr_label(parent.left) or "compare"
            if field == "left" and parent.comparators:
                return expr_label(parent.comparators[index or 0]) or "compare"
            return "compare"
        current = parent
    return ""


def nearest_assignment_target(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> str:
    current = node
    while current in parents:
        parent, field, _ = parents[current]
        if isinstance(parent, ast.Assign) and field == "value":
            return "|".join(expr_label(target) for target in parent.targets if expr_label(target))
        if isinstance(parent, ast.AnnAssign) and field == "value":
            return expr_label(parent.target)
        current = parent
    return ""


def nearest_positional_call_info(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> tuple[str, str, bool]:
    current = node
    while current in parents:
        parent, field, index = parents[current]
        if isinstance(parent, ast.Call) and field == "args" and index is not None:
            param_name, feeds_model = local_positional_param_context(parent, index, parents)
            return expr_label(parent.func), param_name, feeds_model
        current = parent
    return "", "", False


def local_positional_param_context(
    call: ast.Call,
    arg_index: int,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> tuple[str, bool]:
    callee = local_callee_for_call(call, parents)
    if callee is None:
        return "", False
    params = [arg.arg for arg in callee.args.posonlyargs + callee.args.args]
    offset = 1 if params and params[0] in {"self", "cls"} else 0
    param_index = arg_index + offset
    if param_index >= len(params):
        return "", False
    param = params[param_index]
    return param, param_feeds_model_keyword(callee, param)


def local_callee_for_call(
    call: ast.Call,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    if isinstance(call.func, ast.Name):
        module = enclosing_module(call, parents)
        for node in getattr(module, "body", []):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == call.func.id:
                return node
    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in {"self", "cls"}
    ):
        class_node = enclosing_class_node(call, parents)
        if class_node is None:
            return None
        for node in class_node.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == call.func.attr:
                return node
    return None


def enclosing_module(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> ast.Module:
    current = node
    while current in parents:
        current = parents[current][0]
    return current if isinstance(current, ast.Module) else ast.Module(body=[], type_ignores=[])


def enclosing_class_node(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> ast.ClassDef | None:
    current = node
    while current in parents:
        current = parents[current][0]
        if isinstance(current, ast.ClassDef):
            return current
    return None


def param_feeds_model_keyword(node: ast.AST, param: str) -> bool:
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call):
            continue
        for keyword in candidate.keywords:
            if not keyword.arg or not is_model_arg_name(keyword.arg):
                continue
            if expr_uses_name(keyword.value, param):
                return True
    return False


def expr_uses_name(node: ast.AST | None, name: str) -> bool:
    if node is None:
        return False
    return any(isinstance(candidate, ast.Name) and candidate.id == name for candidate in ast.walk(node))


def nearest_call_name(
    node: ast.AST,
    parents: dict[ast.AST, tuple[ast.AST, str, int | None]],
) -> str:
    call = nearest_node(node, parents, ast.Call)
    return expr_label(call.func) if call else ""


def nearest_node(node: ast.AST, parents: dict[ast.AST, tuple[ast.AST, str, int | None]], node_type: type) -> ast.AST | None:
    current = node
    while current in parents:
        parent, _, _ = parents[current]
        if isinstance(parent, node_type):
            return parent
        current = parent
    return None


def enclosing_context(row: dict[str, str], nodes: tuple[ast.AST, ...]) -> tuple[str, str]:
    line_number = int(row["line_number"])
    functions = []
    classes = []
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and has_location(node):
            if node.lineno <= line_number <= node.end_lineno:
                functions.append((node.lineno, node.name))
        elif isinstance(node, ast.ClassDef) and has_location(node):
            if node.lineno <= line_number <= node.end_lineno:
                classes.append((node.lineno, node.name))
    function_context = sorted(functions, reverse=True)[0][1] if functions else ""
    class_context = sorted(classes, reverse=True)[0][1] if classes else ""
    return function_context, class_context


def classify_label(
    row: dict[str, str],
    ast_context: AstContext,
    loader_context: LoaderContext,
    eligible_loader_count: int,
) -> tuple[str, str, bool, str]:
    source_context = row.get("source_context", "")
    path_signal = row.get("path_signal", "")
    occurrence_fp_signal = row.get("fp_signal", "")
    terms = set(split_terms(ast_context.surrounding_context_terms))
    carrier_terms = set(split_terms(ast_context.direct_carrier_name))
    container_terms = set(split_terms(ast_context.carrier_container_name))
    all_terms = terms | carrier_terms | container_terms
    model_like = bool((carrier_terms | container_terms) & MODEL_CARRIER_TERMS)

    if occurrence_fp_signal == "docstring_comment" or source_context in {"comment", "docstring"}:
        return "fp_docstring_comment", "excluded", False, "occurrence discovery source context is comment/docstring"
    if path_signal == "example_or_demo_code":
        return "fp_path_example_or_demo", "excluded", False, "occurrence discovery path signal marks example/demo code"
    if path_signal == "third_party_or_vendored_code":
        return "fp_path_third_party_or_vendored", "excluded", False, "occurrence discovery path signal marks third-party/vendored code"
    if is_model_id_validation_context(ast_context):
        return (
            "fp_model_id_validation_signal",
            "excluded",
            False,
            "direct carrier indicates model-ID validation/checking rather than model loading",
        )
    if is_helper_model_positional_context(ast_context):
        return (
            "trace_candidate_medium",
            "medium",
            True,
            "local helper positional argument maps to a parameter that feeds a model keyword",
        )
    if is_test_assertion_context(row, ast_context):
        return (
            "fp_test_fixture_assertion_signal",
            "excluded",
            False,
            "test fixture/assertion line checks the model-ID value",
        )
    if is_agent_loading_from_cli_context(row, ast_context):
        return (
            "fp_agent_loading_from_cli_signal",
            "excluded",
            False,
            "model ID appears in a CLI command list with a model flag",
        )
    if is_short_ambiguous_wrong_string_match(row, ast_context, loader_context, model_like):
        return (
            "fp_wrong_string_matching_signal",
            "excluded",
            False,
            "short ambiguous model ID appears in non-model code context without loader evidence",
        )
    if eligible_loader_count == 0:
        return (
            "excluded_no_loader_sink",
            "excluded",
            False,
            "call analysis found no eligible loader sinks in this repo snapshot",
        )
    if all_terms & METADATA_TERMS:
        return (
            "metadata_context_signal",
            "low",
            True,
            f"metadata-like carrier/context terms: {sorted(all_terms & METADATA_TERMS)}",
        )
    if all_terms & DESCRIPTIVE_TERMS:
        return (
            "descriptive_context_signal",
            "low",
            True,
            f"descriptive carrier/context terms: {sorted(all_terms & DESCRIPTIVE_TERMS)}",
        )

    if is_plausible_function_default_model_context(ast_context, loader_context, carrier_terms):
        return (
            "trace_candidate_medium",
            "medium",
            True,
            "function default is a plausible model carrier and same-file loader evidence exists",
        )
    if model_like and loader_context.count:
        return (
            "trace_candidate_high",
            "high",
            True,
            "model-like direct carrier and call analysis loader candidate exists in same file",
        )
    if ast_context.ast_context in {"call_keyword_arg", "dict_value"} and model_like:
        return "trace_candidate_high", "high", True, "model-like direct carrier in call keyword or dict value"
    if ast_context.ast_context == "collection_item" and model_like:
        return (
            "trace_candidate_low",
            "low",
            True,
            "model-like collection/registry item; defer loader connectivity to binding tracing",
        )
    if model_like:
        return "trace_candidate_medium", "medium", True, "model-like direct carrier without same-file loader evidence"
    if ast_context.ast_context in {"call_keyword_arg", "dict_value", "assignment_value", "class_attribute"}:
        return (
            "trace_candidate_medium",
            "medium",
            True,
            f"usable AST context {ast_context.ast_context} but carrier is not clearly model-like",
        )
    if ast_context.ast_context == "return_value":
        return (
            "trace_candidate_low",
            "low",
            True,
            "returned string value; defer source-to-sink proof to binding tracing",
        )
    if ast_context.ast_context == "call_positional_arg":
        return (
            "trace_candidate_low",
            "low",
            True,
            "positional string argument; defer source-to-sink proof to binding tracing",
        )
    return "needs_manual_context", "manual", False, "no decisive local context rule matched"


def is_model_id_validation_context(ast_context: AstContext) -> bool:
    carrier = ast_context.direct_carrier_name.strip()
    if ast_context.ast_context == "comparison_value":
        return True
    if carrier in MODEL_ID_VALIDATION_CALLS:
        return True
    method = carrier.rsplit(".", 1)[-1]
    return method in MODEL_ID_VALIDATION_METHODS


def is_helper_model_positional_context(ast_context: AstContext) -> bool:
    return (
        ast_context.ast_context == "call_positional_arg"
        and ast_context.direct_carrier_kind == "call_positional_arg_model_param"
    )


def is_test_assertion_context(row: dict[str, str], ast_context: AstContext) -> bool:
    carrier = ast_context.direct_carrier_name.lower()
    path_terms = set(split_terms(row.get("file_path", "")))
    line_terms = set(split_terms(row.get("line_text", "")))
    if "assert" in carrier:
        return True
    if "assert" in line_terms:
        return True
    return bool({"test", "tests"} & path_terms and ast_context.ast_context == "call_positional_arg")


def is_model_arg_name(name: str | None) -> bool:
    if not name:
        return False
    normalized = name.strip().lower()
    return normalized in MODEL_CARRIER_TERMS


def is_agent_loading_from_cli_context(row: dict[str, str], ast_context: AstContext) -> bool:
    if ast_context.ast_context != "collection_item":
        return False
    carrier_terms = set(split_terms(ast_context.direct_carrier_name))
    if not carrier_terms & CLI_COMMAND_TARGET_TERMS:
        return False
    line = row.get("line_text", "").lower()
    terms = set(split_terms(ast_context.surrounding_context_terms))
    return "--model" in line or bool("model" in terms and {"codex", "cli", "subprocess"} & terms)


def is_short_ambiguous_wrong_string_match(
    row: dict[str, str],
    ast_context: AstContext,
    loader_context: LoaderContext,
    model_like: bool,
) -> bool:
    if loader_context.count or model_like:
        return False
    if ast_context.ast_context not in {"call_positional_arg", "function_default"}:
        return False
    matched_value = strip_quotes(row.get("matched_text", "")).lower()
    model_basename = row.get("canonical_model_id", "").rsplit("/", 1)[-1].lower()
    return matched_value == model_basename and model_basename in SHORT_AMBIGUOUS_OPENAI_MODEL_IDS


def is_plausible_function_default_model_context(
    ast_context: AstContext,
    loader_context: LoaderContext,
    carrier_terms: set[str],
) -> bool:
    if ast_context.ast_context != "function_default" or not loader_context.count:
        return False
    return bool(carrier_terms & FUNCTION_DEFAULT_MODEL_TERMS)


def context_terms(
    row: dict[str, str],
    direct_carrier_name: str,
    container_name: str,
    function_context: str,
    class_context: str,
) -> str:
    pieces = [
        row.get("file_path", ""),
        row.get("line_text", ""),
        direct_carrier_name,
        container_name,
        function_context,
        class_context,
    ]
    terms = []
    seen = set()
    for piece in pieces:
        for term in split_terms(piece):
            if term not in seen:
                seen.add(term)
                terms.append(term)
    return "|".join(terms)


def split_terms(value: str) -> list[str]:
    output = []
    for term in re.split(r"[^A-Za-z0-9]+", value.replace("_", " ")):
        term = term.strip().lower()
        if term:
            output.append(term)
    return output


def expr_label(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = expr_label(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Subscript):
        return expr_label(node.value)
    if isinstance(node, ast.Call):
        return expr_label(node.func)
    if isinstance(node, (ast.Tuple, ast.List)):
        return "|".join(expr_label(elt) for elt in node.elts if expr_label(elt))
    return ""


def load_loader_contexts(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    by_file: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_file.setdefault(row["file_path"], []).append(row)
    return by_file


def call_loader_is_eligible(row: dict[str, str]) -> bool:
    """Mirror binding tracing sink eligibility enough for source context analysis routing."""
    sink_flag = row.get("call_sink_eligible", "")
    if sink_flag and not parse_bool(sink_flag):
        return False
    receiver_origin = row.get("receiver_origin", "")
    if not receiver_origin:
        return False
    matched_origins = set(split_pipe(row.get("matched_rule_import_origin", "")))
    if matched_origins and receiver_origin not in matched_origins:
        return False
    linked_origins = set(split_pipe(row.get("linked_import_origin", "")))
    if linked_origins and receiver_origin not in linked_origins:
        return False
    return True


def split_pipe(value: str) -> list[str]:
    return [part for part in (value or "").split("|") if part]


def nearest_loader_context(row: dict[str, str], loader_rows: list[dict[str, str]]) -> LoaderContext:
    if not loader_rows:
        return LoaderContext()
    line_number = int(row["line_number"])
    nearest = min(loader_rows, key=lambda item: abs(int(item["line_number"]) - line_number))
    distance = abs(int(nearest["line_number"]) - line_number)
    return LoaderContext(
        count=len(loader_rows),
        nearest_line=nearest["line_number"],
        nearest_call=nearest["visible_call_chain"],
        nearest_distance=str(distance),
        nearest_candidate_id=nearest["loader_candidate_id"],
    )


if __name__ == "__main__":
    raise SystemExit(main())
