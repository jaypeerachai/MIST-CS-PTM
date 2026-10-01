#!/usr/bin/env python
"""Parse repository Python files and cache their syntax facts."""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import tokenize
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from mist.analysis.parse_cache import ParsedPythonFile, parse_python_files, source_hash, write_syntax_python_cache
from mist.io import count_values, display_output_path, write_csv

SOURCE_ROOT_DIRS = {"lib", "python", "src", "source"}


def main(argv: Sequence[str] | None = None) -> int:
    """Export file, import, literal, definition, assignment, call, and span facts."""
    args = parse_args(argv)
    repo = args.repo.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    files = []
    spans = []
    imports = []
    strings = []
    definitions = []
    assignments = []
    calls = []
    returns = []

    parsed_files = list(parse_python_files(repo, collect_nodes=True))
    write_syntax_python_cache(output_dir, parsed_files)

    for parsed in parsed_files:
        module = module_name_from_path(Path(parsed.rel_path))
        files.append(file_row(parsed, module))
        spans.extend(source_context_span_rows(parsed))
        if parsed.tree is None:
            continue
        imports.extend(import_rows(parsed))
        strings.extend(string_rows(parsed))
        definitions.extend(definition_rows(parsed, module))
        assignments.extend(assignment_rows(parsed, module))
        calls.extend(call_rows(parsed, module))
        returns.extend(return_rows(parsed, module))

    write_csv(output_dir / "files.csv", files)
    write_csv(output_dir / "source_context_spans.csv", spans)
    write_csv(output_dir / "imports.csv", imports)
    write_csv(output_dir / "strings.csv", strings)
    write_csv(output_dir / "definitions.csv", definitions)
    write_csv(output_dir / "assignments.csv", assignments)
    write_csv(output_dir / "calls.csv", calls)
    write_csv(output_dir / "returns.csv", returns)

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "repo": str(repo),
        "python_files": len(files),
        "parse_errors": sum(1 for row in files if row["parse_error"]),
        "row_counts": {
            "files": len(files),
            "source_context_spans": len(spans),
            "imports": len(imports),
            "strings": len(strings),
            "definitions": len(definitions),
            "assignments": len(assignments),
            "calls": len(calls),
            "returns": len(returns),
        },
        "parse_error_counts": count_values(files, "parse_error"),
        "outputs": {
            "files": display_output_path(output_dir / "files.csv"),
            "source_context_spans": display_output_path(output_dir / "source_context_spans.csv"),
            "imports": display_output_path(output_dir / "imports.csv"),
            "strings": display_output_path(output_dir / "strings.csv"),
            "definitions": display_output_path(output_dir / "definitions.csv"),
            "assignments": display_output_path(output_dir / "assignments.csv"),
            "calls": display_output_path(output_dir / "calls.csv"),
            "returns": display_output_path(output_dir / "returns.csv"),
            "ast_cache": display_output_path(output_dir / "parsed_python_files.pkl"),
            "summary": display_output_path(output_dir / "syntax_summary.json"),
        },
    }
    (output_dir / "syntax_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export reusable AST-derived repo facts.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def file_row(parsed: ParsedPythonFile, module: str) -> dict[str, Any]:
    return {
        "file_path": parsed.rel_path,
        "module": module,
        "sha256": source_hash(parsed.text),
        "line_count": len(parsed.lines),
        "parse_error": parsed.parse_error,
    }


def source_context_span_rows(parsed: ParsedPythonFile) -> list[dict[str, Any]]:
    rows = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(parsed.text).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            rows.append(span_row(parsed.rel_path, "comment", *token.start, *token.end))
    except (tokenize.TokenError, IndentationError):
        pass

    if parsed.tree is not None:
        for node in parsed.nodes:
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            doc_expr = docstring_expr(node)
            if doc_expr is None or not has_full_location(doc_expr):
                continue
            rows.append(
                span_row(
                    parsed.rel_path,
                    "docstring",
                    doc_expr.lineno,
                    doc_expr.col_offset,
                    doc_expr.end_lineno,
                    doc_expr.end_col_offset,
                )
            )
    return rows


def span_row(
    rel_path: str,
    source_context: str,
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
) -> dict[str, Any]:
    return {
        "file_path": rel_path,
        "source_context": source_context,
        "start_line": start_line,
        "start_col": start_col,
        "end_line": end_line,
        "end_col": end_col,
    }


def import_rows(parsed: ParsedPythonFile) -> list[dict[str, Any]]:
    rows = []
    for node in parsed.nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                rows.append(
                    {
                        "file_path": parsed.rel_path,
                        "line_number": node.lineno,
                        "import_style": "import",
                        "module": alias.name,
                        "root": alias.name.split(".", 1)[0],
                        "imported_name": alias.name,
                        "symbol": "",
                        "alias": alias.asname or "",
                        "line_text": line_at(parsed.lines, node.lineno),
                    }
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            for alias in node.names:
                rows.append(
                    {
                        "file_path": parsed.rel_path,
                        "line_number": node.lineno,
                        "import_style": "from",
                        "module": node.module,
                        "root": root,
                        "imported_name": f"{node.module}.{alias.name}",
                        "symbol": alias.name,
                        "alias": alias.asname or "",
                        "line_text": line_at(parsed.lines, node.lineno),
                    }
                )
    return rows


def string_rows(parsed: ParsedPythonFile) -> list[dict[str, Any]]:
    rows = []
    for node in parsed.nodes:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        rows.append(
            {
                "file_path": parsed.rel_path,
                "line_number": getattr(node, "lineno", 0),
                "column_start": getattr(node, "col_offset", 0),
                "column_end": getattr(node, "end_col_offset", 0),
                "value": node.value,
                "line_text": line_at(parsed.lines, getattr(node, "lineno", 0)),
            }
        )
    return rows


def definition_rows(parsed: ParsedPythonFile, module: str) -> list[dict[str, Any]]:
    rows = []
    for node in parsed.nodes:
        if isinstance(node, ast.ClassDef):
            rows.append(
                {
                    "file_path": parsed.rel_path,
                    "module": module,
                    "line_number": node.lineno,
                    "definition_kind": "class",
                    "name": node.name,
                    "args": "",
                    "line_text": line_at(parsed.lines, node.lineno),
                }
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rows.append(
                {
                    "file_path": parsed.rel_path,
                    "module": module,
                    "line_number": node.lineno,
                    "definition_kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                    "name": node.name,
                    "args": "|".join(function_arg_names(node)),
                    "line_text": line_at(parsed.lines, node.lineno),
                }
            )
    return rows


def assignment_rows(parsed: ParsedPythonFile, module: str) -> list[dict[str, Any]]:
    rows = []
    for node in parsed.nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            rows.append(
                {
                    "file_path": parsed.rel_path,
                    "module": module,
                    "line_number": node.lineno,
                    "target": expr_text(target),
                    "value": expr_text(node.value),
                    "line_text": line_at(parsed.lines, node.lineno),
                }
            )
    return rows


def call_rows(parsed: ParsedPythonFile, module: str) -> list[dict[str, Any]]:
    rows = []
    for node in parsed.nodes:
        if not isinstance(node, ast.Call):
            continue
        rows.append(
            {
                "file_path": parsed.rel_path,
                "module": module,
                "line_number": node.lineno,
                "call_chain": call_chain(node.func),
                "keyword_args": "|".join(keyword.arg for keyword in node.keywords if keyword.arg),
                "line_text": line_at(parsed.lines, node.lineno),
            }
        )
    return rows


def return_rows(parsed: ParsedPythonFile, module: str) -> list[dict[str, Any]]:
    rows = []
    for node in parsed.nodes:
        if not isinstance(node, ast.Return):
            continue
        rows.append(
            {
                "file_path": parsed.rel_path,
                "module": module,
                "line_number": node.lineno,
                "value": expr_text(node.value),
                "line_text": line_at(parsed.lines, node.lineno),
            }
        )
    return rows


def module_name_from_path(rel_path: Path) -> str:
    parts = list(rel_path.with_suffix("").parts)
    if len(parts) > 1 and parts[0] in SOURCE_ROOT_DIRS:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def docstring_expr(node: ast.AST) -> ast.Expr | None:
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if not isinstance(first, ast.Expr):
        return None
    value = first.value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return first
    return None


def has_full_location(node: ast.AST) -> bool:
    return all(hasattr(node, attr) for attr in ("lineno", "col_offset", "end_lineno", "end_col_offset"))


def function_arg_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    return [arg.arg for arg in args]


def call_chain(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = call_chain(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return call_chain(node.func)
    return ""


def expr_text(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def line_at(lines: list[str], line_number: int) -> str:
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1].strip()
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
