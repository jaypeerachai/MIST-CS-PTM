"""Check active mock replacements along a binding.

Resolve known mocking APIs through imports, match targets structurally, and
carry active patches through project calls. Unsupported relevant replacements
or calling contexts are reported as unresolved, not as proven mocks.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from mist.rules.library_rules import LIBRARY_RULES


MOCKS = {"unittest.mock." + name for name in (
    "Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock", "create_autospec"
)}
PATCHES = {"unittest.mock.patch", "unittest.mock.patch.object"}


@dataclass
class Value:
    kind: str = "unknown"
    symbol: str = ""
    location: str = ""
    node: ast.AST | None = None
    object_id: str = ""


@dataclass
class Patch:
    target: str
    replacement: str
    location: str
    replacement_location: str = ""
    target_object: str = ""
    detail: str = ""
    uncertain_scope: bool = False
    lookup_target: str = ""


@dataclass
class Context:
    state: str
    evidence: list[dict] = field(default_factory=list)


class MockScope:
    def __init__(self, engine, builder):
        self.e = engine
        self.builder = builder
        self.repo = builder.repo_facts

    @staticmethod
    def location(facts, node):
        return f"{facts.rel_path}:{getattr(node, 'lineno', 0)}"

    def owner(self, facts, node):
        return self.e.enclosing_function(node, facts.parents)

    def preceding(self, facts, point):
        """Only earlier statements in enclosing blocks, not other functions."""
        levels = []
        child = point
        while child in facts.parents:
            parent = facts.parents[child]
            for _, value in ast.iter_fields(parent):
                if isinstance(value, list) and child in value:
                    levels.append([item for item in value[:value.index(child)] if isinstance(item, ast.stmt)])
            child = parent
        return [stmt for level in reversed(levels) for stmt in level]

    def bound_value(self, name, facts, point, seen):
        found = None
        for stmt in self.preceding(facts, point):
            if isinstance(stmt, ast.Import):
                for item in stmt.names:
                    alias = item.asname or item.name.split(".")[0]
                    if alias == name:
                        found = Value("symbol", item.name if item.asname else alias, self.location(facts, stmt), stmt)
            elif isinstance(stmt, ast.ImportFrom):
                module = self.e.resolve_relative_module(facts.module, stmt.module or "", stmt.level)
                for item in stmt.names:
                    if (item.asname or item.name) == name:
                        found = Value("symbol", f"{module}.{item.name}", self.location(facts, stmt), stmt)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if any(self.e.call_chain(target) == name for target in targets):
                    found = self.resolve(stmt.value, facts, stmt, seen)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and stmt.name == name:
                found = Value("symbol", f"{facts.module}.{name}", self.location(facts, stmt), stmt)
            elif isinstance(stmt, (ast.If, ast.For, ast.While, ast.Try)):
                # A conditional reassignment prevents a definite provenance claim.
                if any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name
                       for node in ast.walk(stmt)):
                    found = Value(location=self.location(facts, stmt))
        owner = self.owner(facts, point)
        if owner and name in self.e.function_arg_names(owner):
            # Do not resolve a shadowed imported name through the module scope.
            assigned = any(isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom))
                           and self.owner(facts, stmt) is owner
                           and any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name
                                   for node in ast.walk(stmt))
                           for stmt in self.preceding(facts, point))
            if not assigned:
                return Value(location=self.location(facts, owner))
        return found or Value()

    def resolve(self, expr, facts, point=None, seen=frozenset()):
        if expr is None:
            return Value()
        point = point or expr
        key = (facts.rel_path, id(expr), id(point))
        if key in seen or len(seen) > 30:
            return Value()
        seen = seen | {key}
        if isinstance(expr, ast.Name):
            return self.bound_value(expr.id, facts, point, seen)
        if isinstance(expr, ast.Attribute):
            if self.e.call_chain(expr).startswith(("self.", "cls.")):
                assigned = self.bound_value(self.e.call_chain(expr), facts, point, seen)
                if assigned.kind != "unknown":
                    return assigned
            base = self.resolve(expr.value, facts, point, seen)
            if base.kind in {"symbol", "instance"}:
                return Value(base.kind, base.symbol + "." + expr.attr, base.location, expr, base.object_id)
            if base.kind == "mock":
                return base
            return Value()
        if isinstance(expr, ast.Call):
            function = self.resolve(expr.func, facts, point, seen)
            keywords = {kw.arg: kw.value for kw in expr.keywords}
            if function.symbol in MOCKS:
                if "side_effect" in keywords:
                    return Value("unknown", location=self.location(facts, expr), node=expr)
                if "wraps" in keywords:
                    wrapped = self.resolve(keywords["wraps"], facts, point, seen)
                    return Value("delegate", wrapped.symbol, self.location(facts, expr), expr)
                return Value("mock", function.symbol, self.location(facts, expr), expr)
            if function.symbol in PATCHES:
                return Value("patcher", function.symbol, self.location(facts, expr), expr)
            if function.kind == "symbol":
                return Value("instance", function.symbol, self.location(facts, expr), expr,
                             f"{facts.rel_path}:{expr.lineno}:{expr.col_offset}")
        if isinstance(expr, ast.Lambda):
            if isinstance(expr.body, ast.Constant):
                return Value("stub", location=self.location(facts, expr), node=expr)
            return Value("unknown", location=self.location(facts, expr), node=expr)
        return Value(location=self.location(facts, expr), node=expr)

    def canonical_target(self, target):
        # Resolve project re-exports in string patch targets, such as app.LLM.
        parts = target.split(".")
        for count in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:count])
            file = self.repo.module_index.get(module)
            if not file:
                continue
            ref = self.repo.files[file].imports.get(parts[count])
            if ref:
                return ".".join([ref.module, *([ref.symbol] if ref.symbol else []), *parts[count + 1:]])
        return target

    def patch_from_call(self, facts, call):
        name = self.resolve(call.func, facts, call).symbol
        if name not in PATCHES:
            return None
        keywords = {kw.arg: kw.value for kw in call.keywords}
        object_id = ""
        if name.endswith(".object") and len(call.args) >= 2:
            obj = self.resolve(call.args[0], facts, call)
            attr = self.e.literal_string(call.args[1])
            target = obj.symbol + "." + attr if obj.symbol and attr else ""
            lookup_target = target
            object_id = obj.object_id
            new = keywords.get("new", call.args[2] if len(call.args) > 2 else None)
        elif name == "unittest.mock.patch" and call.args:
            lookup_target = self.e.literal_string(call.args[0])
            target = self.canonical_target(lookup_target)
            new = keywords.get("new", call.args[1] if len(call.args) > 1 else None)
        else:
            return Patch("", "unknown", self.location(facts, call), detail="dynamic patch target")
        kind, replacement_location, detail = "mock", self.location(facts, call), "default unittest.mock replacement"
        if "side_effect" in keywords or any(kw.arg is None for kw in call.keywords):
            kind, detail = "unknown", "replacement behaviour is not resolved"
        elif new is not None:
            value = self.resolve(new, facts, call)
            kind = value.kind if value.kind in {"mock", "stub"} else "unknown"
            replacement_location, detail = value.location, "explicit replacement"
        elif "new_callable" in keywords:
            factory = self.resolve(keywords["new_callable"], facts, call)
            kind = "mock" if factory.symbol in MOCKS else "unknown"
            replacement_location, detail = factory.location, "replacement factory"
        elif "wraps" in keywords:
            value = self.resolve(keywords["wraps"], facts, call)
            kind = "delegate" if value.symbol == target else "unknown"
            replacement_location, detail = value.location, "delegating replacement"
        elif target.endswith(".__new__"):
            if "return_value" not in keywords:
                kind, detail = "unknown", "constructor replacement result is not resolved"
            else:
                value = self.resolve(keywords["return_value"], facts, call)
                kind = "mock" if value.kind == "mock" else "unknown"
                replacement_location, detail = value.location, "constructor returns replacement"
        return Patch(target, kind, self.location(facts, call), replacement_location,
                     object_id, detail, lookup_target=lookup_target)

    def lookup_address(self, facts, function):
        if isinstance(function, ast.Name):
            value = self.resolve(function, facts, function)
            owner = self.owner(facts, value.node) if value.node else None
            if owner:
                return f"{facts.module}.{owner.name}.<locals>.{function.id}"
            return f"{facts.module}.{function.id}"
        return self.resolve(function, facts, function).symbol

    def match(self, patch, sink_value, sink_row, lookup):
        if not patch.target:
            return "possible"
        target = patch.target
        if patch.target_object:
            return "exact" if patch.target_object == sink_value.object_id and target == sink_value.symbol else "none"
        if target == sink_value.symbol:
            # Replacing module.X does not replace an already imported local X.
            if sink_value.kind == "instance" or patch.lookup_target == lookup:
                return "exact"
            return "none"
        if target.endswith((".__new__", ".__init__")) and target.rsplit(".", 1)[0] == sink_value.symbol:
            return "exact"
        if LIBRARY_RULES.is_endpoint_alias(target, sink_value.symbol):
            return "endpoint_alias"
        # A relevant but unresolved target is not a proven mock.
        origin = sink_row.get("linked_import_origin", "")
        endpoint = sink_row.get("terminal_call", "")
        if origin and target.startswith(origin + ".") and target.endswith("." + endpoint):
            return "possible"
        return "none"

    def active_here(self, facts, point, include_fixtures=True):
        patches = []
        child = point
        owner = self.owner(facts, point)
        while child in facts.parents:
            parent = facts.parents[child]
            if isinstance(parent, (ast.With, ast.AsyncWith)) and child in parent.body:
                for item in parent.items:
                    expr = item.context_expr
                    resolved = self.resolve(expr, facts, expr)
                    patch_call = expr if isinstance(expr, ast.Call) else resolved.node if resolved.kind == "patcher" else None
                    if isinstance(patch_call, ast.Call):
                        parsed = self.patch_from_call(facts, patch_call)
                        if parsed:
                            patches.append(parsed)
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in parent.decorator_list:
                    if isinstance(decorator, ast.Call):
                        parsed = self.patch_from_call(facts, decorator)
                        if parsed:
                            patches.append(parsed)
                # An outer lexical with-block does not remain active merely
                # because a function is defined inside it.
                break
            child = parent
        cls = self.e.enclosing_class(point, facts.parents)
        if cls and owner and owner.name.startswith("test"):
            for decorator in cls.decorator_list:
                if isinstance(decorator, ast.Call):
                    parsed = self.patch_from_call(facts, decorator)
                    if parsed:
                        patches.append(parsed)
            for method in cls.body:
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) and method.name in {"setUp", "asyncSetUp"}:
                    setup = self.started_patches(facts, method, before=None)
                    known_lifecycle = any(self.resolve(base, facts, cls).symbol in {
                        "unittest.TestCase", "unittest.IsolatedAsyncioTestCase"
                    } for base in cls.bases)
                    for item in setup:
                        item.uncertain_scope = not known_lifecycle
                        if not known_lifecycle:
                            item.detail += "; setup execution is not established"
                    patches += setup
        if owner:
            patches += self.started_patches(facts, owner, before=point)
            if include_fixtures and owner.name.startswith("test"):
                patches += self.fixture_patches(facts, owner)
        return patches

    def started_patches(self, facts, function, before):
        active = {}
        statements = self.preceding(facts, before) if before else function.body
        for stmt in statements:
            if self.owner(facts, stmt) is not function:
                continue
            conditional = isinstance(stmt, (ast.If, ast.For, ast.While, ast.Try))
            values = ([node for node in ast.walk(stmt) if isinstance(node, ast.Call)
                       and self.owner(facts, node) is function] if conditional else
                      [stmt.value] if isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign)) else [])
            for value in values:
                if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
                    continue
                if self.is_monkeypatch(facts, value.func.value, value):
                    key = "monkeypatch:" + self.e.call_chain(value.func.value)
                    if value.func.attr == "undo":
                        for old in list(active):
                            if old.startswith(key + ":"):
                                if conditional:
                                    active[old].uncertain_scope = True
                                else:
                                    del active[old]
                    elif value.func.attr == "setattr":
                        parsed = self.monkeypatch_from_call(facts, value)
                        if parsed:
                            parsed.uncertain_scope = conditional
                            active[key + ":" + parsed.target] = parsed
                    continue
                if value.func.attr not in {"start", "stop"}:
                    continue
                patcher = self.resolve(value.func.value, facts, value)
                if patcher.kind != "patcher":
                    continue
                parsed = self.patch_from_call(facts, patcher.node)
                if parsed:
                    if value.func.attr == "start":
                        parsed.uncertain_scope = conditional
                        active[patcher.location] = parsed
                    elif conditional and patcher.location in active:
                        active[patcher.location].uncertain_scope = True
                    else:
                        active.pop(patcher.location, None)
        return list(active.values())

    def is_monkeypatch(self, facts, receiver, point):
        if self.resolve(receiver, facts, point).symbol == "pytest.MonkeyPatch":
            return True
        owner = self.owner(facts, point)
        if not owner or not isinstance(receiver, ast.Name):
            return False
        for arg in [*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs]:
            if arg.arg == receiver.id and arg.annotation is not None:
                if self.resolve(arg.annotation, facts, owner).symbol == "pytest.MonkeyPatch":
                    return True
        # The standard pytest fixture is only assumed in a test function. A
        # custom visible fixture with this name must not inherit that assumption.
        if receiver.id == "monkeypatch" and owner.name.startswith("test") and receiver.id in self.e.function_arg_names(owner):
            parent = PurePosixPath(facts.rel_path).parent
            for candidate in self.repo.files.values():
                other = PurePosixPath(candidate.rel_path)
                visible = candidate is facts or (other.name == "conftest.py" and
                          (other.parent == parent or other.parent in parent.parents))
                if visible and candidate.tree and any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                                                     and node.name == "monkeypatch" for node in candidate.tree.body):
                    return False
            return True
        return False

    def monkeypatch_from_call(self, facts, call):
        if len(call.args) >= 3:
            obj = self.resolve(call.args[0], facts, call)
            attr = self.e.literal_string(call.args[1])
            target = obj.symbol + "." + attr if obj.symbol and attr else ""
            lookup, object_id, replacement = target, obj.object_id, call.args[2]
        elif len(call.args) == 2:
            lookup = self.e.literal_string(call.args[0])
            target, object_id, replacement = self.canonical_target(lookup), "", call.args[1]
        else:
            return Patch("", "unknown", self.location(facts, call), detail="dynamic monkeypatch arguments")
        value = self.resolve(replacement, facts, call)
        if isinstance(replacement, ast.Lambda) and not isinstance(replacement.body, ast.Constant):
            value = self.resolve(replacement.body, facts, call)
        kind = value.kind if value.kind in {"mock", "stub"} else "unknown"
        return Patch(target, kind, self.location(facts, call), value.location, object_id,
                     "pytest monkeypatch replacement", lookup_target=lookup)

    def fixture_patches(self, test_facts, test_function):
        names = set(self.e.function_arg_names(test_function))
        candidates = {}
        test_parent = PurePosixPath(test_facts.rel_path).parent
        for facts in self.repo.files.values():
            parent = PurePosixPath(facts.rel_path).parent
            visible = facts is test_facts or (
                PurePosixPath(facts.rel_path).name == "conftest.py"
                and (parent == test_parent or parent in test_parent.parents)
            )
            if not visible or not facts.tree:
                continue
            for function in facts.tree.body:
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in function.decorator_list:
                    target = decorator.func if isinstance(decorator, ast.Call) else decorator
                    if self.resolve(target, facts, decorator).symbol != "pytest.fixture":
                        continue
                    fixture_name = function.name
                    autouse = False
                    if isinstance(decorator, ast.Call):
                        for keyword in decorator.keywords:
                            if keyword.arg == "name":
                                fixture_name = self.e.literal_string(keyword.value) or fixture_name
                            if keyword.arg == "autouse":
                                autouse = isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                    candidates.setdefault(fixture_name, []).append((facts, function))
                    if autouse:
                        names.add(fixture_name)
        patches, seen = [], set()
        while names - seen:
            name = sorted(names - seen)[0]
            seen.add(name)
            matches = candidates.get(name, [])
            if not matches:
                continue
            # Prefer the nearest conftest, with local fixtures taking precedence.
            rank = lambda pair: (pair[0] is test_facts, len(PurePosixPath(pair[0].rel_path).parts))
            matches.sort(key=rank, reverse=True)
            facts, function = matches[0]
            if len(matches) > 1 and rank(matches[0]) == rank(matches[1]):
                patches.append(Patch("", "unknown", self.location(facts, function), detail="ambiguous fixture"))
                continue
            names.update(self.e.function_arg_names(function))
            yields = [node for node in ast.walk(function)
                      if isinstance(node, (ast.Yield, ast.YieldFrom)) and self.owner(facts, node) is function]
            if len(yields) == 1 and isinstance(yields[0], ast.Yield):
                patches += self.active_here(facts, yields[0], include_fixtures=False)
        return patches

    def context_at_sink(self, facts, call, inherited, sink_row):
        patches = [*inherited, *self.active_here(facts, call)]
        value = self.resolve(call.func, facts, call)
        evidence, states = [], []
        if value.kind == "mock":
            states.append("mocked")
            evidence.append({"replacement": value.location, "call": self.location(facts, call),
                             "detail": "call receiver resolves to unittest.mock instance"})
        for patch in patches:
            match = self.match(patch, value, sink_row, self.lookup_address(facts, call.func))
            if match == "none":
                continue
            state = ("unresolved" if match == "possible" or patch.uncertain_scope else
                     "mocked" if patch.replacement in {"mock", "stub"} else
                     "clear" if patch.replacement == "delegate" else "unresolved")
            states.append(state)
            evidence.append({"target": patch.target, "target_match": match, "patch": patch.location,
                             "replacement": patch.replacement_location, "replacement_kind": patch.replacement,
                             "call": self.location(facts, call), "detail": patch.detail, "state": state})
        # Unknown nested replacements must not be hidden by another mock rule.
        state = ("unresolved" if "unresolved" in states or {"mocked", "clear"}.issubset(states)
                 else "mocked" if "mocked" in states else "clear")
        return Context(state, evidence)

    def analyze(self, source, sink):
        source_facts = self.repo.files[source["file_path"]]
        sink_facts = self.repo.files[sink.loader_row["file_path"]]
        sink_call = self.e.find_call_at_line(sink_facts, int(sink.loader_row["line_number"]),
                                           sink.loader_row["visible_call_chain"])
        if sink_call is None:
            return Context("unresolved", [{"detail": "sink call location is not resolved"}])
        source_node = self.e.find_literal_node_id(
            source_facts, self.e.strip_quotes(source.get("matched_text", "")),
            int(source["line_number"]), int(source.get("column_start", "0")))
        literals = [node for node in source_facts.nodes if isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and self.e.literal_node(source_facts.rel_path, node.lineno, node.col_offset, node.value) == source_node]
        if len(literals) != 1:
            return Context("unresolved", [{"detail": "source occurrence location is not resolved"}])
        anchors, child = [], literals[0]
        while child in source_facts.parents:
            parent = source_facts.parents[child]
            if isinstance(parent, ast.Call):
                anchors.append(parent)
            if isinstance(parent, ast.stmt):
                break
            child = parent
        contexts = []

        def follow(facts, call, inherited, visited):
            key = (facts.rel_path, call.lineno, call.col_offset)
            if len(visited) >= 8 or key in visited:
                return []
            if facts is sink_facts and call is sink_call:
                return [self.context_at_sink(facts, call, inherited, sink.loader_row)]
            scope = self.builder.scope_for_node(facts, call)
            targets = self.builder.resolve_call_targets(facts, scope, call)
            active = [*inherited, *self.active_here(facts, call)]
            output = []
            for symbol in targets:
                function = self.repo.functions.get(symbol)
                if function is None:
                    continue
                child_facts = self.repo.files[function.rel_path]
                for nested in ast.walk(function.node):
                    if isinstance(nested, ast.Call) and self.owner(child_facts, nested) is function.node:
                        reached = follow(child_facts, nested, active, visited | {key})
                        for context in reached:
                            context.evidence.insert(0, {"invocation": self.location(facts, call),
                                                        "project_helper": symbol})
                        output += reached
            return output

        for anchor in anchors:
            contexts += follow(source_facts, anchor, [], set())
            if contexts:
                break
        if not contexts:
            direct = self.context_at_sink(sink_facts, sink_call, [], sink.loader_row)
            if direct.state == "clear" and not self.active_here(source_facts, literals[0]):
                return direct
            return Context("unresolved", [*direct.evidence, {"detail": "source calling context is not resolved"}])
        evidence = [item for context in contexts for item in context.evidence]
        # One clear invocation is enough for real reuse of this occurrence.
        state = "clear" if any(context.state == "clear" for context in contexts) else (
            "unresolved" if any(context.state == "unresolved" for context in contexts) else "mocked")
        return Context(state, evidence)
