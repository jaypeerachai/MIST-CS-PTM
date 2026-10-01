"""Recover location evidence for existing registry-to-config summary edges.

This module does not add value-flow edges or decide reuse. It checks the code
behind an already selected summary and records a unique constructor transfer.
Unsupported or ambiguous transfers remain unresolved, rather than looking local
merely because a summary's endpoints happen to be in the same file.
"""

from __future__ import annotations

import ast
from typing import Any


SUMMARY_EDGE = "adapter_config_registry_model_sink"


def _function_nodes(node: ast.AST):
    """Walk one body without entering nested functions or classes."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield from _function_nodes(child)


class RegistryLocationResolver:
    def __init__(self, repo_facts: Any):
        self.repo = repo_facts

    def _symbol(self, module: str, name: str, seen: frozenset = frozenset()) -> str:
        key = (module, name)
        if key in seen:
            return ""
        symbol = self.repo.classes_by_module_name.get(key, "")
        if symbol:
            return symbol
        facts = self.repo.files.get(self.repo.module_index.get(module, ""))
        ref = facts.imports.get(name) if facts else None
        if ref and ref.is_project and ref.symbol:
            return self._symbol(ref.module, ref.symbol, seen | {key})
        return ""

    def _class(self, facts: Any, node: ast.AST | None) -> str:
        # Generic type arguments do not change the parent implementation.
        if isinstance(node, ast.Subscript):
            return self._class(facts, node.value)
        if isinstance(node, ast.Name):
            return self._symbol(facts.module, node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            ref = facts.imports.get(node.value.id)
            if ref and ref.is_project and not ref.symbol:
                return self._symbol(ref.module, node.attr)
        return ""

    def _references(self, facts: Any, expression: ast.AST | None, symbol: str) -> bool:
        if expression is None:
            return False
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            try:
                expression = ast.parse(expression.value, mode="eval")
            except SyntaxError:
                return False
        return any(self._class(facts, node) == symbol for node in ast.walk(expression))

    def _location(self, facts: Any, node: ast.AST, kind: str, scope: str,
                  procedure: str = "") -> dict[str, Any]:
        return {
            "file_path": facts.rel_path,
            "line_number": node.lineno,
            "end_line_number": getattr(node, "end_lineno", node.lineno),
            "scope": scope,
            "procedure": procedure,
            "step_type": kind,
            "evidence": ast.unparse(node),
        }

    def _constructor(self, symbol: str, seen: frozenset = frozenset()):
        if symbol in seen:
            return None
        cls = self.repo.classes.get(symbol)
        if not cls:
            return None
        own = self.repo.functions.get(f"{symbol}.__init__")
        if own:
            return own
        # Do not guess the MRO or assume a missing external parent is harmless.
        if len(cls.node.bases) != 1:
            return None
        facts = self.repo.files[cls.rel_path]
        return self._constructor(self._class(facts, cls.node.bases[0]), seen | {symbol})

    def _transfer(self, init: Any, parameter: str, field_name: str,
                  seen: frozenset = frozenset()):
        """Find one direct assignment, optionally through explicit super calls."""
        key = (init.symbol, parameter)
        if key in seen:
            return None
        facts = self.repo.files[init.rel_path]
        args = [*init.node.args.posonlyargs, *init.node.args.args, *init.node.args.kwonlyargs]
        arg = next((arg for arg in args if arg.arg == parameter), None)
        if arg is None:
            return None
        nodes = list(_function_nodes(init.node))
        # Reassignment makes the simple parameter transfer insufficient evidence.
        if any(isinstance(node, ast.Name) and node.id == parameter
               and isinstance(node.ctx, (ast.Store, ast.Del)) for node in nodes):
            return None
        candidates = []
        assignments = []
        for node in nodes:
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
            )
            if any(isinstance(target, ast.Attribute) and target.attr == field_name
                   and isinstance(target.value, ast.Name) and target.value.id == "self"
                   for target in targets):
                assignments.append(node)
        if len(assignments) > 1:
            return None
        for node in assignments:
            if (facts.parents.get(node) is init.node
                    and isinstance(node.value, ast.Name) and node.value.id == parameter):
                candidates.append([self._location(
                    facts, node, "config_field_assignment", init.symbol, init.symbol)])
            else:
                return None
        for node in nodes:
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "__init__"
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == "super"):
                continue
            if not any(isinstance(value, ast.Name) and value.id == parameter
                       for value in [*node.args, *(kw.value for kw in node.keywords)]):
                continue
            parent = facts.parents.get(node)
            cls = self.repo.classes.get(init.class_symbol)
            if (not isinstance(parent, ast.Expr) or facts.parents.get(parent) is not init.node
                    or node.func.value.args or node.func.value.keywords
                    or not cls or len(cls.node.bases) != 1
                    or any(isinstance(value, ast.Starred) for value in node.args)
                    or any(kw.arg is None for kw in node.keywords)):
                return None
            base = self._constructor(self._class(facts, cls.node.bases[0]))
            if base is None:
                return None
            parameters = [*base.node.args.posonlyargs, *base.node.args.args][1:]
            forwarded = [parameters[index].arg for index, value in enumerate(node.args)
                         if index < len(parameters) and isinstance(value, ast.Name)
                         and value.id == parameter]
            forwarded += [kw.arg for kw in node.keywords
                          if isinstance(kw.value, ast.Name) and kw.value.id == parameter]
            if len(forwarded) != 1:
                return None
            remainder = self._transfer(base, forwarded[0], field_name, seen | {key})
            if remainder is None:
                return None
            candidates.append([self._location(
                facts, node, "constructor_argument_transfer", init.symbol, init.symbol),
                *remainder])
        if len(candidates) != 1:
            return None
        return [self._location(facts, arg, "constructor_parameter", init.symbol, init.symbol),
                *candidates[0]]

    def resolve(self, source_row: dict, sink_row: dict) -> dict[str, Any]:
        unresolved = {"summary_location_status": "unresolved", "summary_locations": []}
        facts = self.repo.files.get(sink_row.get("file_path", ""))
        if not facts or facts.tree is None:
            return unresolved
        calls = [node for node in facts.nodes if isinstance(node, ast.Call)
                 and node.lineno == int(sink_row["line_number"])
                 and ast.unparse(node.func) == sink_row.get("visible_call_chain")]
        if len(calls) != 1:
            return unresolved
        call = calls[0]
        # The supported summary reads a model member through an instance config.
        reads = []
        for keyword in call.keywords:
            if keyword.arg not in {"model", "model_id", "model_name"}:
                continue
            for node in ast.walk(keyword.value):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Attribute)
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == "self"):
                    reads.append(node)
        # Other keyword arguments may read the same config (e.g. timeout).
        source_facts = self.repo.files.get(source_row.get("file_path", ""))
        if not source_facts:
            return unresolved
        registry = self._symbol(source_facts.module, source_row.get("class_context", ""))
        if not registry:
            return unresolved
        config_fields = []
        for cls in source_facts.classes:
            for field in cls.node.body:
                if (isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
                        and (self._references(source_facts, field.annotation, registry)
                             or self._references(source_facts, field.value, registry))):
                    config_fields.append((cls, field))
        owner = facts.parents.get(call)
        while owner is not None and not isinstance(owner, ast.ClassDef):
            owner = facts.parents.get(owner)
        cls = next((cls for cls in facts.classes if cls.node is owner), None)
        fn_node = facts.parents.get(call)
        while fn_node is not None and not isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_node = facts.parents.get(fn_node)
        fn = next((fn for fn in facts.functions if fn.node is fn_node), None)
        init = self._constructor(cls.symbol) if cls else None
        if not init or not fn:
            return unresolved
        init_facts = self.repo.files[init.rel_path]
        candidates = []
        for config, field in config_fields:
            for read in reads:
                if read.attr != field.target.id:
                    continue
                # A later reassignment in the calling method invalidates this
                # constructor-only explanation of the stored configuration.
                if fn.symbol != init.symbol and any(
                    isinstance(node, ast.Attribute) and node.attr == read.value.attr
                    and isinstance(node.value, ast.Name) and node.value.id == "self"
                    and isinstance(node.ctx, (ast.Store, ast.Del))
                    for node in _function_nodes(fn.node)
                ):
                    continue
                for arg in [*init.node.args.posonlyargs, *init.node.args.args, *init.node.args.kwonlyargs]:
                    if not self._references(init_facts, arg.annotation, config.symbol):
                        continue
                    transfer = self._transfer(init, arg.arg, read.value.attr)
                    if transfer:
                        candidates.append([
                            self._location(source_facts, field, "registry_config_field", config.symbol),
                            *transfer,
                            self._location(facts, read, "config_model_read", fn.symbol, fn.symbol),
                        ])
        if len(candidates) != 1:
            return unresolved
        return {"summary_location_status": "verified", "summary_locations": candidates[0]}


def enrich_registry_summaries(graph: Any, repo_facts: Any, source_rows: dict,
                             sink_rows: dict) -> None:
    """Attach evidence only. Graph nodes, edges and their order stay unchanged."""
    resolver = RegistryLocationResolver(repo_facts)
    cache = {}
    for source, sink, edge in graph.edges(data=True):
        if edge.get("edge_type") != SUMMARY_EDGE:
            continue
        source_row, sink_row = source_rows.get(source), sink_rows.get(sink)
        if source_row is None or sink_row is None:
            edge.update(summary_location_status="unresolved", summary_locations=[])
            continue
        key = (source_row.get("file_path"), source_row.get("class_context"), sink)
        if key not in cache:
            cache[key] = resolver.resolve(source_row, sink_row)
        edge.update(cache[key])


def summary_locations(graph: Any, path: list[str]) -> list[dict]:
    if graph is None:
        return []
    return [location for source, target in zip(path, path[1:])
            for location in graph.edges[source, target].get("summary_locations", [])
            if graph.edges[source, target].get("summary_location_status") == "verified"]


def unresolved_summary(graph: Any, path: list[str]) -> bool:
    return graph is not None and any(
        graph.edges[source, target].get("edge_type") == SUMMARY_EDGE
        and graph.edges[source, target].get("summary_location_status") != "verified"
        for source, target in zip(path, path[1:])
    )
