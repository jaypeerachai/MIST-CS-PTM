"""Share parsed Python files and reject stale source caches."""

from __future__ import annotations

import ast
import hashlib
import pickle
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mist.io import iter_python_files

AST_CACHE_FILENAME = "parsed_python_files.pkl"


@dataclass
class ParsedPythonFile:
    path: Path
    rel_path: str
    text: str
    lines: list[str]
    tree: ast.AST | None
    parse_error: str = ""
    nodes: tuple[ast.AST, ...] = ()
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)
    parent_links: dict[ast.AST, tuple[ast.AST, str, int | None]] = field(default_factory=dict)

    @property
    def parse_failed(self) -> bool:
        return self.tree is None and bool(self.parse_error)


def source_hash(text: str) -> str:
    """Hash source text so syntax index caches cannot silently go stale."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def parse_python_file(
    path: Path,
    *,
    rel_path: str = "",
    collect_nodes: bool = False,
    collect_parents: bool = False,
    collect_parent_links: bool = False,
) -> ParsedPythonFile:
    """Read and parse one Python file, with optional reusable AST indexes."""
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    rel_path = rel_path or path.as_posix()
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return ParsedPythonFile(
            path=path,
            rel_path=rel_path,
            text=text,
            lines=lines,
            tree=None,
            parse_error=str(exc),
        )

    needs_nodes = collect_nodes or collect_parents or collect_parent_links
    nodes = tuple(ast.walk(tree)) if needs_nodes else ()
    parents: dict[ast.AST, ast.AST] = {}
    parent_links: dict[ast.AST, tuple[ast.AST, str, int | None]] = {}
    if collect_parents or collect_parent_links:
        for parent in nodes:
            for field, value in ast.iter_fields(parent):
                if isinstance(value, list):
                    for index, child in enumerate(value):
                        if not isinstance(child, ast.AST):
                            continue
                        if collect_parents:
                            parents[child] = parent
                        if collect_parent_links:
                            parent_links[child] = (parent, field, index)
                elif isinstance(value, ast.AST):
                    if collect_parents:
                        parents[value] = parent
                    if collect_parent_links:
                        parent_links[value] = (parent, field, None)

    return ParsedPythonFile(
        path=path,
        rel_path=rel_path,
        text=text,
        lines=lines,
        tree=tree,
        nodes=nodes,
        parents=parents,
        parent_links=parent_links,
    )


def parse_python_files(
    repo: Path,
    *,
    syntax_cache: Path | None = None,
    check_file_name: bool = False,
    sort_paths: bool = False,
    collect_nodes: bool = False,
    collect_parents: bool = False,
    collect_parent_links: bool = False,
) -> Iterable[ParsedPythonFile]:
    """Yield parsed Python files for a repo using the shared file iterator."""
    if syntax_cache is not None:
        yield from load_syntax_python_files(
            repo,
            syntax_cache,
            check_file_name=check_file_name,
            sort_paths=sort_paths,
            collect_nodes=collect_nodes,
            collect_parents=collect_parents,
            collect_parent_links=collect_parent_links,
        )
        return

    for path in iter_python_files(repo, check_file_name=check_file_name, sort_paths=sort_paths):
        rel_path = path.relative_to(repo).as_posix()
        yield parse_python_file(
            path,
            rel_path=rel_path,
            collect_nodes=collect_nodes,
            collect_parents=collect_parents,
            collect_parent_links=collect_parent_links,
        )


def write_syntax_python_cache(cache_dir: Path, parsed_files: Iterable[ParsedPythonFile]) -> Path:
    """Cache parsed source and AST trees for call and binding analysis."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for parsed in parsed_files:
        records.append(
            {
                "rel_path": parsed.rel_path,
                "text": parsed.text,
                "sha256": source_hash(parsed.text),
                "tree": parsed.tree,
                "parse_error": parsed.parse_error,
            }
        )
    cache_path = cache_dir / AST_CACHE_FILENAME
    previous_limit = sys.getrecursionlimit()
    try:
        # Deep ASTs need extra recursion while pickling, not during later analysis.
        sys.setrecursionlimit(max(previous_limit, 100_000))
        with cache_path.open("wb") as handle:
            pickle.dump({"version": 1, "files": records}, handle, protocol=pickle.HIGHEST_PROTOCOL)
    finally:
        sys.setrecursionlimit(previous_limit)
    return cache_path


def load_syntax_python_files(
    repo: Path,
    cache_dir: Path,
    *,
    check_file_name: bool = False,
    sort_paths: bool = False,
    collect_nodes: bool = False,
    collect_parents: bool = False,
    collect_parent_links: bool = False,
) -> Iterable[ParsedPythonFile]:
    """Yield parsed files from a syntax index cache in the same order as live scanning."""
    cache_records = load_syntax_python_cache(cache_dir)
    expected_paths = [
        path.relative_to(repo).as_posix()
        for path in iter_python_files(repo, check_file_name=check_file_name, sort_paths=sort_paths)
    ]
    for rel_path in expected_paths:
        record = cache_records.get(rel_path)
        path = repo / rel_path
        if record is None:
            yield parse_python_file(
                path,
                rel_path=rel_path,
                collect_nodes=collect_nodes,
                collect_parents=collect_parents,
                collect_parent_links=collect_parent_links,
            )
            continue
        text = str(record.get("text", ""))
        current_text = path.read_text(encoding="utf-8", errors="replace")
        if source_hash(current_text) != record.get("sha256", ""):
            raise ValueError(f"syntax parsing AST cache is stale for {rel_path}")
        yield parsed_file_from_cache_record(
            path=path,
            rel_path=rel_path,
            text=text,
            tree=record.get("tree"),
            parse_error=str(record.get("parse_error", "")),
            collect_nodes=collect_nodes,
            collect_parents=collect_parents,
            collect_parent_links=collect_parent_links,
        )


def load_syntax_python_file_map(
    repo: Path,
    cache_dir: Path,
    rel_paths: Iterable[str],
    *,
    collect_nodes: bool = False,
    collect_parents: bool = False,
    collect_parent_links: bool = False,
) -> dict[str, ParsedPythonFile]:
    """Load selected cached files instead of materializing the whole repo."""
    cache_records = load_syntax_python_cache(cache_dir)
    parsed: dict[str, ParsedPythonFile] = {}
    for rel_path in rel_paths:
        if not rel_path or rel_path in parsed:
            continue
        path = repo / rel_path
        record = cache_records.get(rel_path)
        if record is None:
            parsed[rel_path] = parse_python_file(
                path,
                rel_path=rel_path,
                collect_nodes=collect_nodes,
                collect_parents=collect_parents,
                collect_parent_links=collect_parent_links,
            )
            continue
        text = str(record.get("text", ""))
        current_text = path.read_text(encoding="utf-8", errors="replace")
        if source_hash(current_text) != record.get("sha256", ""):
            raise ValueError(f"syntax parsing AST cache is stale for {rel_path}")
        parsed[rel_path] = parsed_file_from_cache_record(
            path=path,
            rel_path=rel_path,
            text=text,
            tree=record.get("tree"),
            parse_error=str(record.get("parse_error", "")),
            collect_nodes=collect_nodes,
            collect_parents=collect_parents,
            collect_parent_links=collect_parent_links,
        )
    return parsed


def load_syntax_python_cache(cache_dir: Path) -> dict[str, dict[str, Any]]:
    cache_path = cache_dir / AST_CACHE_FILENAME
    if not cache_path.exists():
        raise FileNotFoundError(f"Missing syntax parsing AST cache: {cache_path}")
    with cache_path.open("rb") as handle:
        payload = pickle.load(handle)
    if payload.get("version") != 1:
        raise ValueError(f"Unsupported syntax parsing AST cache version in {cache_path}")
    return {
        str(record.get("rel_path", "")): record
        for record in payload.get("files", [])
        if record.get("rel_path")
    }


def parsed_file_from_cache_record(
    *,
    path: Path,
    rel_path: str,
    text: str,
    tree: ast.AST | None,
    parse_error: str,
    collect_nodes: bool,
    collect_parents: bool,
    collect_parent_links: bool,
) -> ParsedPythonFile:
    lines = text.splitlines()
    needs_nodes = tree is not None and (collect_nodes or collect_parents or collect_parent_links)
    nodes = tuple(ast.walk(tree)) if needs_nodes else ()
    parents: dict[ast.AST, ast.AST] = {}
    parent_links: dict[ast.AST, tuple[ast.AST, str, int | None]] = {}
    if tree is not None and (collect_parents or collect_parent_links):
        for parent in nodes:
            for field, value in ast.iter_fields(parent):
                if isinstance(value, list):
                    for index, child in enumerate(value):
                        if not isinstance(child, ast.AST):
                            continue
                        if collect_parents:
                            parents[child] = parent
                        if collect_parent_links:
                            parent_links[child] = (parent, field, index)
                elif isinstance(value, ast.AST):
                    if collect_parents:
                        parents[value] = parent
                    if collect_parent_links:
                        parent_links[value] = (parent, field, None)

    return ParsedPythonFile(
        path=path,
        rel_path=rel_path,
        text=text,
        lines=lines,
        tree=tree,
        parse_error=parse_error,
        nodes=nodes,
        parents=parents,
        parent_links=parent_links,
    )
