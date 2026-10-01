"""Match annotated sinks and follow reverse internal edges in a PyCG FASTEN graph."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NamespaceRecord:
    node_id: str
    uri: str
    file_path: str
    first_line: int
    last_line: int

    @property
    def is_callable(self) -> bool:
        return self.uri.endswith("()")


class FastenProjectGraph:
    def __init__(self, payload: dict[str, Any]):
        self.records: dict[str, NamespaceRecord] = {}
        self.by_file: dict[str, list[NamespaceRecord]] = {}
        internal_modules = payload.get("modules", {}).get("internal", {})
        for module in internal_modules.values():
            file_path = normalize_path(module.get("sourceFile", ""))
            if not file_path:
                continue
            for raw_id, namespace in module.get("namespaces", {}).items():
                metadata = namespace.get("metadata", {})
                record = NamespaceRecord(
                    node_id=str(raw_id),
                    uri=str(namespace.get("namespace", "")),
                    file_path=file_path,
                    first_line=parse_int(metadata.get("first")),
                    last_line=parse_int(metadata.get("last")),
                )
                self.records[record.node_id] = record
                self.by_file.setdefault(file_path, []).append(record)

        self.predecessors: dict[str, set[str]] = {}
        for raw_edge in payload.get("graph", {}).get("internalCalls", []):
            if len(raw_edge) < 2:
                continue
            source, target = str(raw_edge[0]), str(raw_edge[1])
            self.predecessors.setdefault(target, set()).add(source)

    def find_seed_nodes(
        self,
        file_path: str,
        line_number: int,
        expected_callable: str,
    ) -> tuple[set[str], str]:
        candidates = self.by_file.get(normalize_path(file_path), [])
        expected = expected_callable.strip()
        if expected:
            named = [record for record in candidates if callable_matches(record.uri, expected)]
            containing = [record for record in named if contains_line(record, line_number)]
            if containing:
                return narrowest_ids(containing), "callable_and_line"
            if named:
                return narrowest_ids(named), "callable_name"

        containing = [record for record in candidates if contains_line(record, line_number)]
        callable_containing = [record for record in containing if record.is_callable]
        if callable_containing:
            return narrowest_ids(callable_containing), "line_span"
        if containing:
            return narrowest_ids(containing), "module_line_span"
        return set(), "missing"

    def reverse_distances(self, seeds: set[str], max_depth: int | None = None) -> dict[str, int]:
        distances = {seed: 0 for seed in seeds}
        queue = deque(sorted(seeds))
        while queue:
            node = queue.popleft()
            depth = distances[node]
            if max_depth is not None and depth >= max_depth:
                continue
            for predecessor in sorted(self.predecessors.get(node, set())):
                if predecessor in distances:
                    continue
                distances[predecessor] = depth + 1
                queue.append(predecessor)
        return distances

    def candidate_files(self, distances: dict[str, int]) -> set[str]:
        return {
            self.records[node].file_path
            for node in distances
            if node in self.records and self.records[node].file_path
        }

    def candidate_functions(self, distances: dict[str, int]) -> set[str]:
        return {
            self.records[node].uri
            for node in distances
            if node in self.records and self.records[node].is_callable
        }

    def first_file_depth(self, distances: dict[str, int], file_path: str) -> str:
        normalized = normalize_path(file_path)
        values = [
            depth
            for node, depth in distances.items()
            if node in self.records and self.records[node].file_path == normalized
        ]
        return "" if not values else str(min(values))


def narrowest_ids(records: list[NamespaceRecord]) -> set[str]:
    widths = [max(0, record.last_line - record.first_line) for record in records]
    minimum = min(widths)
    return {
        record.node_id
        for record in records
        if max(0, record.last_line - record.first_line) == minimum
    }


def contains_line(record: NamespaceRecord, line_number: int) -> bool:
    return record.first_line > 0 and record.first_line <= line_number <= record.last_line


def callable_matches(uri: str, expected: str) -> bool:
    cleaned = uri[:-2] if uri.endswith("()") else uri
    return cleaned.endswith("/" + expected) or cleaned.endswith("." + expected) or cleaned == expected


def normalize_path(value: str) -> str:
    value = str(value or "").replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def parse_int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0
