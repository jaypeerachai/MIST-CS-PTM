"""Adapt the released depth-six caller search to annotated sinks and local repositories.

The untouched authors' files are in upstream/yasmin. Their entry script needs
an unreleased module and project-specific inputs. This adapter supplies source
indexing and caller recovery for the automatic search, not the manual analysis.
"""
from __future__ import annotations
import ast
import re
import subprocess
import time
from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class CodeLocation:
    file_path: str
    line_number: int
    url: str


@dataclass(frozen=True)
class AnnotationCase:
    case_id: str
    workbook: str
    owner_repo: str
    commit: str
    file_path: str
    line_number: int
    model_id: str
    final_fp: bool
    final_fp_type: str
    loader_locations: tuple[CodeLocation, ...]
    import_locations: tuple[CodeLocation, ...]
    manual_num_file_touched: int | None
    manual_notes: str
    file_url: str

    @property
    def known_gold_files(self) -> tuple[str, ...]:
        files = {self.file_path}
        files.update(location.file_path for location in self.loader_locations)
        files.update(location.file_path for location in self.import_locations)
        return tuple(sorted(path for path in files if path))

    @property
    def known_gold_complete(self) -> bool:
        return bool(self.manual_num_file_touched) and self.manual_num_file_touched == len(self.known_gold_files)

    @property
    def touch_scope(self) -> str:
        if self.manual_num_file_touched == 1:
            return "one_file"
        if self.manual_num_file_touched and self.manual_num_file_touched > 1:
            return "multi_file"
        return "missing"


@dataclass(frozen=True)
class CallableSpan:
    qualname: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class TraceRecord:
    workbook: str
    case_id: str
    owner_repo: str
    commit: str
    model_id: str
    seed_index: int
    seed_file: str
    seed_line: int
    seed_callable: str
    record_type: str
    depth: int
    target_callable: str
    file_path: str
    line_number: int
    enclosing_callable: str
    is_source_file: bool
    is_known_gold_file: bool


@dataclass(frozen=True)
class CaseResult:
    workbook: str
    case_id: str
    owner_repo: str
    commit: str
    model_id: str
    gold_label: str
    source_file: str
    source_line: int
    manual_num_file_touched: str
    touch_scope: str
    known_gold_complete: bool
    known_gold_files: str
    seed_files: str
    seed_lines: str
    seed_callables: str
    seed_count: int
    valid_seed_count: int
    trace_status: str
    trace_timed_out: bool
    trace_elapsed_seconds: float
    source_file_retrieved: bool
    source_first_depth: str
    known_gold_files_retrieved: int
    known_gold_file_recall: float
    complete_known_path_retrieved: bool
    candidate_file_count: int
    reference_file_count: int
    definition_file_count: int
    candidate_function_count: int
    candidate_files: str
    candidate_functions: str
    repo_path: str
    manual_notes: str
    file_url: str


class SourceIndex:
    """Repository source access plus cached enclosing-callable recovery."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self._python_files: tuple[str, ...] | None = None
        self._lines: dict[str, list[str]] = {}
        self._spans: dict[str, tuple[CallableSpan, ...]] = {}
        self._reference_index_built = False
        self._unqualified_calls: dict[str, tuple[tuple[str, int, str], ...]] = {}
        self._call_terminal_rows: dict[str, tuple[tuple[str, int, str], ...]] = {}
        self._unqualified_terminal_rows: dict[str, tuple[tuple[str, int, str], ...]] = {}
        self._reversed_call_terminal_names: tuple[tuple[str, str], ...] = ()
        self._reversed_call_terminal_keys: tuple[str, ...] = ()
        self._attribute_suffixes: dict[str, tuple[tuple[str, int, str], ...]] = {}
        self._attribute_groups: dict[
            tuple[str, ...], dict[str, tuple[tuple[str, int, str], ...]]
        ] = {}
        self._attribute_names: dict[tuple[str, ...], tuple[str, ...]] = {}
        self._constructor_calls: dict[str, tuple[tuple[str, int, str], ...]] = {}

    def python_files(self) -> tuple[str, ...]:
        if self._python_files is not None:
            return self._python_files
        command = ["git", "-C", str(self.repo_path), "ls-files", "--", "*.py"]
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode == 0:
            paths = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        else:
            paths = [str(path.relative_to(self.repo_path)) for path in self.repo_path.rglob("*.py") if path.is_file()]
        self._python_files = tuple(sorted(set(paths)))
        return self._python_files

    def lines(self, rel_path: str) -> list[str]:
        if rel_path not in self._lines:
            path = self.repo_path / rel_path
            try:
                self._lines[rel_path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                self._lines[rel_path] = []
        return self._lines[rel_path]

    def callable_spans(self, rel_path: str) -> tuple[CallableSpan, ...]:
        if rel_path in self._spans:
            return self._spans[rel_path]
        source = "\n".join(self.lines(rel_path))
        if not source:
            self._spans[rel_path] = ()
            return ()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            self._spans[rel_path] = ()
            return ()

        spans: list[CallableSpan] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scope: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                qualname = ".".join([*self.scope, node.name])
                spans.append(CallableSpan(qualname, node.lineno, getattr(node, "end_lineno", node.lineno)))
                self.scope.append(node.name)
                self.generic_visit(node)
                self.scope.pop()

        Visitor().visit(tree)
        self._spans[rel_path] = tuple(spans)
        return self._spans[rel_path]

    def enclosing_callable(self, rel_path: str, line_number: int) -> str:
        containing = [
            span
            for span in self.callable_spans(rel_path)
            if span.start_line <= line_number <= span.end_line
        ]
        if not containing:
            return ""
        containing.sort(key=lambda span: (span.end_line - span.start_line, -span.start_line))
        return containing[0].qualname

    def grep(self, pattern: str) -> list[tuple[str, int, str]]:
        """Apply the artifact's repository-wide regex search to tracked Python files."""
        command = ["git", "-C", str(self.repo_path), "grep", "-n", "-E", pattern, "--", "*.py"]
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode in {0, 1}:
            rows = []
            for raw in proc.stdout.splitlines():
                parts = raw.split(":", 2)
                if len(parts) != 3:
                    continue
                try:
                    line_number = int(parts[1])
                except ValueError:
                    continue
                rows.append((parts[0], line_number, parts[2]))
            return rows

        # Synthetic tests and extracted directories need not be Git repositories.
        compiled = re.compile(pattern)
        rows = []
        for rel_path in self.python_files():
            for line_number, line in enumerate(self.lines(rel_path), start=1):
                if compiled.search(line):
                    rows.append((rel_path, line_number, line))
        return rows

    def unqualified_call_rows(self, name: str) -> tuple[tuple[str, int, str], ...]:
        self._build_reference_index()
        if name in self._unqualified_calls:
            return self._unqualified_calls[name]
        reversed_prefix = name[::-1]
        index = bisect_left(self._reversed_call_terminal_keys, reversed_prefix)
        rows: set[tuple[str, int, str]] = set()
        while index < len(self._reversed_call_terminal_names):
            reversed_terminal, terminal = self._reversed_call_terminal_names[index]
            if not reversed_terminal.startswith(reversed_prefix):
                break
            if terminal == name:
                # GNU grep interprets ``[^.\w]`` in the released ERE as a
                # negated set containing literal ``w`` rather than Python's
                # word class. An exact terminal is nevertheless excluded when
                # it is immediately preceded by a dot.
                rows.update(self._unqualified_terminal_rows.get(terminal, ()))
            else:
                preceding = terminal[-len(name) - 1]
                if preceding not in {".", "w"}:
                    rows.update(self._call_terminal_rows.get(terminal, ()))
            index += 1
        result = tuple(sorted(rows, key=lambda row: (row[0], row[1], row[2])))
        self._unqualified_calls[name] = result
        return result

    def attribute_suffix_rows(self, suffix: str) -> tuple[tuple[str, int, str], ...]:
        self._build_reference_index()
        if suffix in self._attribute_suffixes:
            return self._attribute_suffixes[suffix]
        parts = suffix.split(".")
        path, terminal_prefix = tuple(parts[:-1]), parts[-1]
        names = self._attribute_names.get(path, ())
        terminal_rows = self._attribute_groups.get(path, {})
        rows: set[tuple[str, int, str]] = set()
        index = bisect_left(names, terminal_prefix)
        while index < len(names) and names[index].startswith(terminal_prefix):
            rows.update(terminal_rows[names[index]])
            index += 1
        result = tuple(sorted(rows, key=lambda row: (row[0], row[1], row[2])))
        self._attribute_suffixes[suffix] = result
        return result

    def constructor_call_rows(self, name: str) -> tuple[tuple[str, int, str], ...]:
        self._build_reference_index()
        return self._constructor_calls.get(name, ())

    def _build_reference_index(self) -> None:
        """Index the exact token shapes used by the artifact's grep regexes."""
        if self._reference_index_built:
            return
        call_terminals: dict[str, set[tuple[str, int, str]]] = defaultdict(set)
        unqualified_terminals: dict[str, set[tuple[str, int, str]]] = defaultdict(set)
        attributes: dict[
            tuple[str, ...], dict[str, set[tuple[str, int, str]]]
        ] = defaultdict(lambda: defaultdict(set))
        constructors: dict[str, set[tuple[str, int, str]]] = defaultdict(set)
        attribute_pattern = re.compile(r"\b\w+(?:\.\w+)+")
        call_chain_pattern = re.compile(r"\b(\w+(?:\.\w+)*)\s*\(")

        for file_path in self.python_files():
            for line_number, line in enumerate(self.lines(file_path), start=1):
                row = (file_path, line_number, line)
                for match in attribute_pattern.finditer(line):
                    parts = match.group(0).split(".")
                    # It can stop at any intermediate component. Store exact
                    # terminal names by their preceding suffix path; prefix
                    # matching is resolved compactly at query time.
                    for start in range(1, len(parts)):
                        for end in range(start + 1, len(parts) + 1):
                            prefix_parts = tuple(parts[start : end - 1])
                            terminal = parts[end - 1]
                            attributes[prefix_parts][terminal].add(row)
                for match in call_chain_pattern.finditer(line):
                    chain = match.group(1)
                    terminal = chain.rsplit(".", 1)[-1]
                    call_terminals[terminal].add(row)
                    preceding = line[match.start(1) - 1] if match.start(1) else ""
                    if "." not in chain and preceding not in {".", "\\"}:
                        unqualified_terminals[terminal].add(row)
                    constructors[terminal].add(row)

        sort_rows = lambda rows: tuple(sorted(rows, key=lambda row: (row[0], row[1], row[2])))
        self._call_terminal_rows = {name: sort_rows(rows) for name, rows in call_terminals.items()}
        self._unqualified_terminal_rows = {
            name: sort_rows(rows) for name, rows in unqualified_terminals.items()
        }
        self._reversed_call_terminal_names = tuple(
            sorted((name[::-1], name) for name in self._call_terminal_rows)
        )
        self._reversed_call_terminal_keys = tuple(row[0] for row in self._reversed_call_terminal_names)
        self._attribute_groups = {
            path: {name: sort_rows(rows) for name, rows in terminal_rows.items()}
            for path, terminal_rows in attributes.items()
        }
        self._attribute_names = {
            path: tuple(sorted(terminal_rows)) for path, terminal_rows in self._attribute_groups.items()
        }
        self._constructor_calls = {name: sort_rows(rows) for name, rows in constructors.items()}
        self._reference_index_built = True


class ReverseCallerTracer:
    """Faithful, deterministic adaptation of the released reverse-caller BFS."""

    def __init__(self, repo_path: Path, max_depth: int = 6, reference_backend: str = "indexed"):
        self.repo_path = repo_path.resolve()
        self.max_depth = max_depth
        self.reference_backend = reference_backend
        self.index = SourceIndex(self.repo_path)
        self._reference_cache: dict[str, tuple[tuple[str, int, str], ...]] = {}
        self._definition_cache: dict[str, tuple[tuple[str, int, str], ...]] = {}

    def trace_seed(
        self,
        case: AnnotationCase,
        seed_index: int,
        seed: CodeLocation,
        deadline: float | None = None,
    ) -> tuple[list[TraceRecord], str]:
        if not (self.repo_path / seed.file_path).exists():
            return [], "missing_seed_file"
        seed_callable = self.index.enclosing_callable(seed.file_path, seed.line_number)
        if not seed_callable:
            return [], "module_level_seed"

        records: list[TraceRecord] = []
        queue: deque[tuple[int, str]] = deque([(1, seed_callable)])
        visited_callers = {(seed.file_path, seed_callable)}
        expanded_targets: set[tuple[int, str]] = set()
        timed_out = False

        while queue:
            if deadline_reached(deadline):
                timed_out = True
                break
            depth, target = queue.popleft()
            if depth > self.max_depth or (depth, target) in expanded_targets:
                continue
            expanded_targets.add((depth, target))

            for file_path, line_number, _line in self.definition_rows(target):
                if deadline_reached(deadline):
                    timed_out = True
                    break
                records.append(self.record(case, seed_index, seed, seed_callable, "definition", depth, target, file_path, line_number, ""))
            if timed_out:
                break

            for file_path, line_number, _line in self.reference_rows(target):
                if deadline_reached(deadline):
                    timed_out = True
                    break
                caller = self.index.enclosing_callable(file_path, line_number)
                records.append(self.record(case, seed_index, seed, seed_callable, "reference", depth, target, file_path, line_number, caller))
                caller_key = (file_path, caller)
                if caller and caller_key not in visited_callers and depth < self.max_depth:
                    visited_callers.add(caller_key)
                    queue.append((depth + 1, caller))
            if timed_out:
                break

        records = dedupe_records(records)
        return records, "timed_out" if timed_out else "ok"

    def record(
        self,
        case: AnnotationCase,
        seed_index: int,
        seed: CodeLocation,
        seed_callable: str,
        record_type: str,
        depth: int,
        target: str,
        file_path: str,
        line_number: int,
        enclosing_callable: str,
    ) -> TraceRecord:
        return TraceRecord(
            workbook=case.workbook,
            case_id=case.case_id,
            owner_repo=case.owner_repo,
            commit=case.commit,
            model_id=case.model_id,
            seed_index=seed_index,
            seed_file=seed.file_path,
            seed_line=seed.line_number,
            seed_callable=seed_callable,
            record_type=record_type,
            depth=depth,
            target_callable=target,
            file_path=file_path,
            line_number=line_number,
            enclosing_callable=enclosing_callable,
            is_source_file=file_path == case.file_path,
            is_known_gold_file=file_path in case.known_gold_files,
        )

    def reference_rows(self, target: str) -> tuple[tuple[str, int, str], ...]:
        if target in self._reference_cache:
            return self._reference_cache[target]
        class_name, method_name, constructor = split_target(target)
        if not method_name or (not class_name and method_name == "main"):
            self._reference_cache[target] = ()
            return ()

        if self.reference_backend == "git_grep":
            rows = self.reference_rows_git_grep(class_name, method_name, constructor)
        else:
            rows = self.reference_rows_indexed(class_name, method_name, constructor)
        self._reference_cache[target] = tuple(sorted(set(rows), key=lambda row: (row[0], row[1], row[2])))
        return self._reference_cache[target]

    def reference_rows_indexed(
        self,
        class_name: str,
        method_name: str,
        constructor: bool,
    ) -> list[tuple[str, int, str]]:
        if constructor:
            candidate_groups = [self.index.constructor_call_rows(method_name)]
        elif class_name:
            candidate_groups = [
                self.index.attribute_suffix_rows(class_name + "." + method_name),
                self.index.attribute_suffix_rows(method_name),
            ]
        else:
            candidate_groups = [
                self.index.unqualified_call_rows(method_name),
                self.index.attribute_suffix_rows(method_name),
            ]
        for candidates in candidate_groups:
            rows = [row for row in candidates if is_reference_line(row[2], method_name)]
            if rows:
                return rows
        return []

    def reference_rows_git_grep(
        self,
        class_name: str,
        method_name: str,
        constructor: bool,
    ) -> list[tuple[str, int, str]]:
        if constructor:
            patterns = [rf"(\.{re.escape(method_name)}|\b{re.escape(method_name)})\s*\("]
        elif class_name:
            patterns = [
                rf"\b\w+\.{re.escape(class_name + '.' + method_name)}\s*",
                rf"\b\w+\.{re.escape(method_name)}\s*",
            ]
        else:
            patterns = [
                rf"(^|[^.\w]){re.escape(method_name)}\s*\(",
                rf"\b\w+\.{re.escape(method_name)}\s*",
            ]
        for pattern in patterns:
            rows = [row for row in self.index.grep(pattern) if is_reference_line(row[2], method_name)]
            if rows:
                return rows
        return []

    def definition_rows(self, target: str) -> tuple[tuple[str, int, str], ...]:
        class_name, _method_name, _constructor = split_target(target)
        if not class_name:
            return ()
        if class_name not in self._definition_cache:
            pattern = rf"class {re.escape(class_name)}\s*"
            rows = [row for row in self.index.grep(pattern) if not row[2].lstrip().startswith("#")]
            self._definition_cache[class_name] = tuple(sorted(set(rows), key=lambda row: (row[0], row[1], row[2])))
        return self._definition_cache[class_name]


def split_target(target: str) -> tuple[str, str, bool]:
    if "." not in target:
        return "", target, False
    class_name, method_name = target.rsplit(".", 1)
    if method_name == "__init__":
        return class_name, class_name, True
    return class_name, method_name, False


def is_reference_line(line: str, method_name: str) -> bool:
    if line.lstrip().startswith("#") or re.search(r"\bclass\b", line):
        return False
    terminal = method_name.rsplit(".", 1)[-1]
    return re.search(rf"\bdef\s+{re.escape(terminal)}\s*\(", line) is None


def dedupe_records(records: Iterable[TraceRecord]) -> list[TraceRecord]:
    seen = set()
    output = []
    for record in sorted(
        records,
        key=lambda row: (row.seed_index, row.depth, row.record_type, row.file_path, row.line_number, row.target_callable),
    ):
        key = (
            record.seed_index,
            record.record_type,
            record.depth,
            record.target_callable,
            record.file_path,
            record.line_number,
            record.enclosing_callable,
        )
        if key not in seen:
            seen.add(key)
            output.append(record)
    return output


def rebind_record(record: TraceRecord, case: AnnotationCase, seed_index: int) -> TraceRecord:
    values = asdict(record)
    values.update(
        workbook=case.workbook,
        case_id=case.case_id,
        owner_repo=case.owner_repo,
        commit=case.commit,
        model_id=case.model_id,
        seed_index=seed_index,
        is_source_file=record.file_path == case.file_path,
        is_known_gold_file=record.file_path in case.known_gold_files,
    )
    return TraceRecord(**values)


def build_case_result(
    case: AnnotationCase,
    repo_path: Path,
    tracer: ReverseCallerTracer,
    records: list[TraceRecord],
    seed_statuses: list[str],
    valid_seed_count: int,
    trace_elapsed_seconds: float,
) -> CaseResult:
    annotated_seed_files = {location.file_path for location in case.loader_locations}
    seed_files = {
        location.file_path
        for location in case.loader_locations
        if (repo_path / location.file_path).exists()
    }
    reference_files = {record.file_path for record in records if record.record_type == "reference"}
    definition_files = {record.file_path for record in records if record.record_type == "definition"}
    candidate_files = seed_files | reference_files | definition_files
    candidate_functions = {record.enclosing_callable for record in records if record.enclosing_callable}
    for location in case.loader_locations:
        caller = tracer.index.enclosing_callable(location.file_path, location.line_number)
        if caller:
            candidate_functions.add(caller)
    retrieved_gold = set(case.known_gold_files) & candidate_files
    source_depths = [record.depth for record in records if record.file_path == case.file_path]
    if case.file_path in seed_files:
        source_depths.append(0)
    status = aggregate_seed_status(seed_statuses, len(case.loader_locations))
    return CaseResult(
        workbook=case.workbook,
        case_id=case.case_id,
        owner_repo=case.owner_repo,
        commit=case.commit,
        model_id=case.model_id,
        gold_label="real_reuse",
        source_file=case.file_path,
        source_line=case.line_number,
        manual_num_file_touched="" if case.manual_num_file_touched is None else str(case.manual_num_file_touched),
        touch_scope=case.touch_scope,
        known_gold_complete=case.known_gold_complete,
        known_gold_files="|".join(case.known_gold_files),
        seed_files="|".join(sorted(annotated_seed_files)),
        seed_lines="|".join(str(location.line_number) for location in case.loader_locations),
        seed_callables="|".join(
            tracer.index.enclosing_callable(location.file_path, location.line_number)
            for location in case.loader_locations
        ),
        seed_count=len(case.loader_locations),
        valid_seed_count=valid_seed_count,
        trace_status=status,
        trace_timed_out=status == "timed_out",
        trace_elapsed_seconds=round(trace_elapsed_seconds, 4),
        source_file_retrieved=case.file_path in candidate_files,
        source_first_depth="" if not source_depths else str(min(source_depths)),
        known_gold_files_retrieved=len(retrieved_gold),
        known_gold_file_recall=safe_div(len(retrieved_gold), len(case.known_gold_files)),
        complete_known_path_retrieved=case.known_gold_complete and set(case.known_gold_files) <= candidate_files,
        candidate_file_count=len(candidate_files),
        reference_file_count=len(reference_files),
        definition_file_count=len(definition_files),
        candidate_function_count=len(candidate_functions),
        candidate_files="|".join(sorted(candidate_files)),
        candidate_functions="|".join(sorted(candidate_functions)),
        repo_path=str(repo_path.resolve()),
        manual_notes=case.manual_notes,
        file_url=case.file_url,
    )


def aggregate_seed_status(statuses: list[str], seed_count: int) -> str:
    if not seed_count:
        return "missing_annotated_loader_seed"
    if any(status == "timed_out" for status in statuses):
        return "timed_out"
    if any(status == "ok" for status in statuses):
        return "ok"
    if any(status == "module_level_seed" for status in statuses):
        return "module_level_seed"
    return "|".join(sorted(set(statuses))) if statuses else "missing_annotated_loader_seed"


def safe_div(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline
