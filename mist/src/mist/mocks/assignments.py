"""Check assignments that replace imported objects or their aliases.

Only simple replacement bodies are summarized. Conditional writes and
unsupported relevant helper effects remain unresolved.
It does not execute repository code or alter the binding graph.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from mist.mocks.scope import (
    Context, MockScope as PatchScope, Patch,
)


@dataclass
class Write:
    target: str
    object_id: str
    facts: object
    statement: ast.AST
    value: ast.AST | None
    uncertain: bool = False
    detail: str = "direct assignment"


class MockScope(PatchScope):
    def __init__(self, engine, builder):
        super().__init__(engine, builder)
        self._writes_cache = {}
        self._helper_cache = {}
        self._analysis_sink = None

    def analyze(self, source, sink):
        # Only mutations that can reach this endpoint belong in its context.
        # An unrelated object write is not evidence of a mocked call, including
        # when the source reaches the sink through a path we cannot follow here.
        facts = self.repo.files[sink.loader_row["file_path"]]
        call = self.e.find_call_at_line(facts, int(sink.loader_row["line_number"]),
                                       sink.loader_row["visible_call_chain"])
        previous = self._analysis_sink
        self._analysis_sink = None if call is None else (
            self.resolve(call.func, facts, call), sink.loader_row,
            self.lookup_address(facts, call.func))
        try:
            return super().analyze(source, sink)
        finally:
            self._analysis_sink = previous

    def match(self, patch, sink_value, sink_row, lookup):
        """Match resolved identities without assuming SDK class aliases."""
        if not patch.target:
            return "possible"
        target = patch.target
        if patch.target_object:
            return "exact" if (patch.target_object == sink_value.object_id
                               and target == sink_value.symbol) else "none"
        if target == sink_value.symbol:
            return "exact" if (sink_value.kind == "instance"
                               or patch.lookup_target == lookup) else "none"
        if target.endswith((".__new__", ".__init__")):
            if target.rsplit(".", 1)[0] == sink_value.symbol:
                return "exact"
        # A shared, verified import root and endpoint do not establish an SDK
        # class alias. Report uncertainty instead of guessing that relationship.
        origin = sink_row.get("linked_import_origin", "")
        endpoint = sink_row.get("terminal_call", "")
        if origin and endpoint and target.startswith(origin + ".") and target.endswith("." + endpoint):
            return "possible"
        return "none"

    def assignment_address(self, facts, target, point):
        if not isinstance(target, ast.Attribute):
            return None
        value = self.resolve(target, facts, point)
        if value.kind not in {"symbol", "instance"} or not value.symbol:
            return None
        return value.symbol, value.object_id

    def helper_writes(self, facts, call, visited=frozenset()):
        """Find possible writes in a called project helper, not in unused defs.

        A discovered write is evidence of a possible effect, not proof that a
        branch executes. Complex helper bodies are deliberately not interpreted.
        """
        key = (facts.rel_path, id(call))
        if key in visited or len(visited) >= 4:
            return []
        cache_key = (key, visited)
        if cache_key in self._helper_cache:
            return self._helper_cache[cache_key]
        scope = self.builder.scope_for_node(facts, call)
        output = []
        for symbol in self.builder.resolve_call_targets(facts, scope, call):
            function = self.repo.functions.get(symbol)
            if function is None:
                continue
            child_facts = self.repo.files[function.rel_path]
            for node in ast.walk(function.node):
                if self.owner(child_facts, node) is not function.node:
                    continue
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        address = self.assignment_address(child_facts, target, node)
                        if address:
                            output.append(Write(*address, child_facts, node, node.value, True,
                                                "possible assignment in called project helper"))
                elif isinstance(node, ast.Call):
                    output.extend(self.helper_writes(child_facts, node, visited | {key}))
        self._helper_cache[cache_key] = output
        return output

    def writes_before(self, facts, point):
        """Latest definite or possible write to each resolved object attribute."""
        cache_key = (facts.rel_path, id(point))
        if cache_key in self._writes_cache:
            return self._writes_cache[cache_key]
        active = {}

        def record(stmt, uncertain=False):
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                # Calls in the RHS happen before storing the assignment.
                if stmt.value is not None:
                    for call in self.expression_calls(stmt.value):
                        for write in self.helper_writes(facts, call):
                            active[(write.target, write.object_id)] = write
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    address = self.assignment_address(facts, target, stmt)
                    if address:
                        active[address] = Write(*address, facts, stmt, stmt.value, uncertain)
            elif isinstance(stmt, ast.Expr):
                for call in self.expression_calls(stmt.value):
                    for write in self.helper_writes(facts, call):
                        active[(write.target, write.object_id)] = write
            elif isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)):
                for _, children in ast.iter_fields(stmt):
                    if isinstance(children, list):
                        for child in children:
                            if isinstance(child, ast.stmt):
                                record(child, True)
                            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                                for nested in child.body:
                                    record(nested, True)
                # After a completed try, its finally block is the last effect.
                # Keep it uncertain when the enclosing statement is conditional.
                if isinstance(stmt, ast.Try):
                    for child in stmt.finalbody:
                        record(child, uncertain)
            elif isinstance(stmt, (ast.AugAssign, ast.Delete)):
                targets = stmt.targets if isinstance(stmt, ast.Delete) else [stmt.target]
                for target in targets:
                    address = self.assignment_address(facts, target, stmt)
                    if address:
                        active[address] = Write(*address, facts, stmt, None, True,
                                                "unsupported attribute update")

        for stmt in self.preceding(facts, point):
            record(stmt)
        self._writes_cache[cache_key] = active
        return active

    def expression_calls(self, node):
        if isinstance(node, ast.Lambda):
            # Defaults are evaluated now, the lambda body is not.
            children = [*node.args.defaults, *(n for n in node.args.kw_defaults if n is not None)]
        else:
            children = list(ast.iter_child_nodes(node))
        for child in children:
            yield from self.expression_calls(child)
        if isinstance(node, ast.Call):
            yield node

    def latest_binding(self, facts, name, point):
        found = None
        for stmt in self.preceding(facts, point):
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                    found = (stmt.value, stmt)
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                if any((a.asname or (a.name if isinstance(stmt, ast.ImportFrom) else a.name.split('.')[0])) == name
                       for a in stmt.names):
                    found = (stmt, stmt) if isinstance(stmt, ast.ImportFrom) else None
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == name:
                found = (stmt, stmt)
            elif isinstance(stmt, (ast.If, ast.For, ast.While, ast.Try)):
                if any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == name
                       for n in ast.walk(stmt)):
                    found = (None, stmt)
        owner = self.owner(facts, point)
        if owner and name in self.e.function_arg_names(owner):
            if found is None or self.owner(facts, found[1]) is not owner:
                return None
        return found

    def callable_kind(self, expr, facts, point, target, invoked_at=None, seen=frozenset()):
        """Return mock/stub/delegate/unknown plus the supporting location.

        delegate means this value resolves to the original target, not that any
        function whose name resembles the target is safe. References captured
        before an assignment retain that earlier value. Function bodies use the
        invocation location for free variables, because closures are late bound.
        """
        if expr is None:
            return "unknown", self.location(facts, point)
        invoked_at = invoked_at or point
        key = (facts.rel_path, id(expr), id(point), target)
        if key in seen or len(seen) >= 24:
            return "unknown", self.location(facts, expr)
        seen = seen | {key}
        if isinstance(expr, ast.ImportFrom):
            write = self.writes_before(facts, expr).get((target, ""))
            if write:
                if write.uncertain:
                    return "unknown", self.location(write.facts, write.statement)
                return self.callable_kind(write.value, write.facts, write.statement, target, invoked_at, seen)
            return "delegate", self.location(facts, expr)
        if isinstance(expr, ast.Name):
            binding = self.latest_binding(facts, expr.id, point)
            if binding:
                value, stored_at = binding
                if value is expr:
                    return "unknown", self.location(facts, expr)
                return self.callable_kind(value, facts, stored_at, target, invoked_at, seen)
        if isinstance(expr, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(expr, ast.Lambda):
                body = expr.body
            else:
                if expr.decorator_list:
                    return "unknown", self.location(facts, expr)
                statements = [n for n in expr.body if not (
                    isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
                if len(statements) != 1 or not isinstance(statements[0], ast.Return):
                    return "unknown", self.location(facts, expr)
                body = statements[0].value
            if body is None:
                return "stub", self.location(facts, expr)
            if isinstance(body, ast.Await):
                body = body.value
            if isinstance(body, ast.Call):
                parameters = self.e.function_arg_names(expr) if not isinstance(expr, ast.Lambda) else [
                    a.arg for a in [*expr.args.posonlyargs, *expr.args.args, *expr.args.kwonlyargs]]
                root = self.e.call_chain(body.func).split('.')[0]
                if root in parameters:
                    return "unknown", self.location(facts, expr)
                # Extra calls in arguments could make real service calls too.
                if any(isinstance(n, ast.Call) for arg in [*body.args, *(kw.value for kw in body.keywords)]
                       for n in ast.walk(arg)):
                    return "unknown", self.location(facts, expr)
                kind, _ = self.callable_kind(body.func, facts, invoked_at, target, invoked_at, seen)
                return kind, self.location(facts, expr)
            # Reading a local/captured name or returning literal data does not
            # invoke the selected service. Attribute reads may run descriptors.
            pure = (ast.Constant, ast.Name, ast.List, ast.Tuple, ast.Set, ast.Dict,
                    ast.Load, ast.Store)
            if all(isinstance(n, pure) for n in ast.walk(body)):
                return "stub", self.location(facts, expr)
            return "unknown", self.location(facts, expr)
        value = self.resolve(expr, facts, point)
        if value.kind in {"mock", "stub"}:
            return value.kind, value.location
        if value.kind == "delegate":
            return ("delegate" if value.symbol == target else "unknown"), value.location
        if isinstance(expr, ast.Attribute):
            address = self.assignment_address(facts, expr, point)
            write = self.writes_before(facts, point).get(address)
            if write:
                if write.uncertain:
                    return "unknown", self.location(write.facts, write.statement)
                return self.callable_kind(write.value, write.facts, write.statement, target, invoked_at, seen)
        if value.kind == "symbol" and value.symbol == target:
            return "delegate", value.location
        return "unknown", value.location or self.location(facts, expr)

    def active_here(self, facts, point, include_fixtures=True):
        patches = super().active_here(facts, point, include_fixtures)
        for write in self.writes_before(facts, point).values():
            patch = Patch(write.target, "unknown", self.location(write.facts, write.statement),
                          target_object=write.object_id, detail=write.detail,
                          uncertain_scope=write.uncertain, lookup_target=write.target)
            if self._analysis_sink and self.match(patch, *self._analysis_sink) == "none":
                continue
            kind, location = self.callable_kind(write.value, write.facts, write.statement,
                                                write.target, point)
            patch.replacement = kind
            patch.replacement_location = location
            patches.append(patch)
        return patches

    def context_at_sink(self, facts, call, inherited, sink_row):
        result = super().context_at_sink(facts, call, inherited, sink_row)
        # A local alias may have captured the replacement before the module
        # attribute was restored. Inspect that stored callable as well.
        if isinstance(call.func, ast.Name):
            binding = self.latest_binding(facts, call.func.id, call)
            if binding:
                raw = self.resolve(call.func, facts, call)
                kind, location = self.callable_kind(call.func, facts, call, raw.symbol, call)
                value, assigned_at = binding
                previous = self.resolve(call.func, facts, assigned_at)
                if (kind == "unknown" and isinstance(value, ast.Call)
                        and previous.kind == "unknown"):
                    # Initializing a callable through a factory is not evidence
                    # of replacing an existing callable. Sink eligibility was
                    # established by call and binding analysis. Do not invalidate it
                    # merely because this mock checker cannot resolve a factory.
                    return result
                state = "mocked" if kind in {"mock", "stub"} else "unresolved" if kind == "unknown" else "clear"
                if state != "clear":
                    evidence = [*result.evidence, {"replacement": location,
                        "call": self.location(facts, call), "replacement_kind": kind,
                        "detail": "stored callable assignment", "state": state}]
                    return Context("unresolved" if "unresolved" in {result.state, state} else state, evidence)
        return result
