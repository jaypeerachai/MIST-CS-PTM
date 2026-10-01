"""Bounded object-state checks for an explicitly constructed project instance.

Follow assignments to that instance before its later method calls. This is an
extra mock check, not a replacement for the source-to-sink graph. Repository code
is parsed, never imported or executed. Unsupported mutations lose provenance.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace

from mist.mocks.scope import Context, MOCKS
from mist.mocks.callables import (
    CallableFlow, State as CallableState, Value as CallableValue, Limit,
)


@dataclass(frozen=True)
class Value:
    kind: str = "unknown"
    symbol: str = ""
    identity: str = ""
    path: tuple = ()
    evidence: tuple = ()
    changed: bool = False


UNKNOWN = Value()


class ProjectCallableFlow(CallableFlow):
    """Use the parser's import-root aliases without changing its shared index."""

    def symbol(self, name):
        value = super().symbol(name)
        if value.name in self.c.repo.classes or value.name in self.c.repo.functions:
            return value
        parts = value.name.split('.')
        for index in range(len(parts) - 1, 0, -1):
            path = self.c.repo.module_index.get('.'.join(parts[:index]))
            if path:
                facts = self.c.repo.files[path]
                candidate = '.'.join([facts.module, *parts[index:]])
                if candidate in self.c.repo.classes or candidate in self.c.repo.functions:
                    return replace(value, name=candidate)
        return value


@dataclass
class State:
    env: dict = field(default_factory=dict)
    heap: dict = field(default_factory=dict)
    uncertain: set = field(default_factory=set)
    context: tuple = ()

    def copy(self):
        return State(dict(self.env), dict(self.heap), set(self.uncertain), self.context)


class ClientFlow:
    MAX_STEPS = 6000
    MAX_DEPTH = 8

    def __init__(self, checker, source, sink):
        self.c = checker
        self.repo = checker.repo
        self.source = source
        self.sink = sink
        self.steps = 0
        self.resolver = ProjectCallableFlow(checker, "")
        self.focus = ""
        self.serial = 0
        self.decorator_cache = checker._client_decorators

    def tick(self):
        self.steps += 1
        if self.steps > self.MAX_STEPS:
            raise Limit("client statement budget")

    def location(self, facts, node):
        return self.c.location(facts, node)

    @staticmethod
    def forget(value, state):
        # An unresolved field passed elsewhere is not an alias of the containing
        # object. Forget that field only, not unrelated fields on its owner.
        if value.identity and value.kind == 'unknown' and value.path:
            state.heap[(value.identity, value.path)] = Value(changed=True)
        elif value.identity:
            state.uncertain.add(value.identity)

    def symbol(self, facts, node):
        raw = self.c.resolve(node, facts, node)
        result = self.resolver.symbol(raw.symbol).name if raw.symbol else ""
        if result and result not in self.repo.classes and result not in self.repo.functions:
            scope = self.c.builder.scope_for_node(facts, node)
            candidates = {s for s in self.c.builder.resolve_callable_symbols(facts, scope, node)
                          if s in self.repo.classes or s in self.repo.functions}
            if len(candidates) == 1:
                return candidates.pop()
        return result

    def method(self, symbol, name, seen=frozenset()):
        if symbol in seen or len(seen) >= self.MAX_DEPTH:
            return None
        direct = self.repo.functions.get(symbol + "." + name)
        if direct:
            return direct
        cls = self.repo.classes.get(symbol)
        if cls is None:
            return None
        candidates = []
        facts = self.repo.files[cls.rel_path]
        for base in cls.node.bases:
            found = self.method(self.symbol(facts, base), name, seen | {symbol})
            if found:
                candidates.append(found)
        return candidates[0] if len(candidates) == 1 else None

    def attribute(self, value, name, state):
        if not value.identity:
            return (Value("symbol", value.symbol + "." + name) if value.kind == "symbol" else
                    Value(evidence=value.evidence, changed=value.changed))
        path = (*value.path, name)
        key = (value.identity, path)
        if value.kind == "object" and not value.path and key not in state.heap:
            method = self.method(value.symbol, name)
            if method:
                return Value("method", method.symbol, value.identity, evidence=value.evidence)
        if value.identity in state.uncertain:
            return Value(evidence=value.evidence, changed=True)
        if key in state.heap:
            stored = state.heap[key]
            return replace(stored, evidence=tuple(dict.fromkeys((*value.evidence, *stored.evidence))),
                           changed=value.changed or stored.changed)
        if value.kind in {"external", "mock", "unknown", "delegate"}:
            return replace(value, symbol=value.symbol + "." + name, path=path)
        return Value(identity=value.identity, path=path, evidence=value.evidence, changed=value.changed)

    def value(self, node, facts, state, depth=0):
        self.tick()
        if node is None:
            return UNKNOWN
        if isinstance(node, ast.Name):
            if node.id in state.env:
                return state.env[node.id]
            symbol = self.symbol(facts, node)
            return Value("symbol", symbol) if symbol else UNKNOWN
        if isinstance(node, ast.Attribute):
            return self.attribute(self.value(node.value, facts, state, depth), node.attr, state)
        if isinstance(node, ast.Await):
            return self.value(node.value, facts, state, depth)
        if isinstance(node, ast.Constant):
            return Value("literal", repr(node.value))
        if isinstance(node, ast.Call):
            function = self.value(node.func, facts, state, depth)
            args = [self.value(n, facts, state, depth) for n in node.args]
            kwargs = {k.arg: self.value(k.value, facts, state, depth) for k in node.keywords}
            evidence = (self.location(facts, node),)
            identity = f"{facts.rel_path}:{node.lineno}:{node.col_offset}:{self.serial}"
            if state.context:
                identity += "|" + "|".join(state.context)
            if function.symbol in MOCKS:
                if "side_effect" in kwargs:
                    return Value(identity=identity, evidence=evidence, changed=True)
                if "wraps" in kwargs:
                    # Attribute mocks and later return_value overrides need their
                    # own semantics. Do not equate a wrapping Mock with its target.
                    return Value(identity=identity, evidence=evidence, changed=True)
                return Value("mock", function.symbol, identity, evidence=evidence, changed=True)
            if function.kind == "symbol" and function.symbol in self.repo.classes:
                obj = Value("object", function.symbol, identity, evidence=evidence)
                constructor = self.method(function.symbol, "__init__")
                if constructor and depth < self.MAX_DEPTH:
                    self.run_method(constructor, obj, args, kwargs, state, depth + 1, initialization=True)
                return obj
            if function.kind == "symbol" and function.symbol and function.symbol not in self.repo.functions:
                # This is provenance, not proof that an external call is a model
                # loader. Sink eligibility has already been checked by MIST.
                relevant = [p for p in self.c.active_here(facts, node)
                            if p.target == function.symbol]
                if relevant:
                    if all(p.replacement in {"mock", "stub"} and not p.uncertain_scope for p in relevant):
                        return Value("mock", function.symbol, identity,
                                     evidence=tuple(p.location for p in relevant), changed=True)
                    return Value(identity=identity, evidence=evidence, changed=True)
                for argument in [*args, *kwargs.values()]:
                    self.forget(argument, state)
                return Value("external", function.symbol, identity, evidence=evidence)
            if function.kind == "method":
                method = self.repo.functions.get(function.symbol)
                obj = Value("object", method.class_symbol, function.identity) if method else UNKNOWN
                if method and depth < self.MAX_DEPTH:
                    self.run_method(method, obj, args, kwargs, state, depth + 1)
            elif function.kind == "symbol" and function.symbol in self.repo.functions:
                helper = self.repo.functions[function.symbol]
                if any(v.identity for v in [*args, *kwargs.values()]) and depth < self.MAX_DEPTH:
                    self.run_method(helper, None, args, kwargs, state, depth + 1)
            else:
                for value in [*args, *kwargs.values()]:
                    self.forget(value, state)
                if isinstance(node.func, ast.Attribute):
                    receiver = self.value(node.func.value, facts, state, depth)
                    if receiver.kind == "object":
                        state.uncertain.add(receiver.identity)
            return UNKNOWN
        if isinstance(node, ast.Lambda):
            kind, evidence = self.c.callable_kind(node, facts, node, "")
            return Value("mock" if kind in {"mock", "stub"} else "unknown",
                         evidence=(evidence,), changed=True)
        # Constants and containers do not establish callable/client provenance.
        return UNKNOWN

    def truth(self, node, facts, state):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = self.truth(node.operand, facts, state)
            return None if value is None else not value
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            left = self.value(node.left, facts, state)
            right = self.value(node.comparators[0], facts, state)
            answer = None
            if right.kind == 'literal' and right.symbol == 'None':
                if left.kind == 'literal':
                    answer = left.symbol == 'None'
                elif left.kind in {'object', 'external', 'mock'}:
                    answer = False
            return None if answer is None else not answer if isinstance(node.ops[0], ast.IsNot) else answer
        value = self.value(node, facts, state)
        if value.kind == 'literal' and value.symbol in {'True', 'False', 'None'}:
            return value.symbol == 'True'
        return None

    def feasible(self, facts, call, state):
        child = call
        while child in facts.parents:
            parent = facts.parents[child]
            if isinstance(parent, ast.If):
                condition = self.truth(parent.test, facts, state)
                if (child in parent.body and condition is False) or (child in parent.orelse and condition is True):
                    return False
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            child = parent
        return True

    def assign(self, target, value, facts, state, initialization=False, uncertain=False):
        if isinstance(target, ast.Name):
            state.env[target.id] = UNKNOWN if uncertain else value
        elif isinstance(target, ast.Attribute):
            receiver = self.value(target.value, facts, state)
            if not receiver.identity:
                return
            key = (receiver.identity, (*receiver.path, target.attr))
            if uncertain:
                value = Value(changed=True)
            location = self.location(facts, target)
            changed = value.changed or not initialization
            # A fresh write replaces only this slot, not the old object and aliases.
            state.heap[key] = replace(value, changed=changed,
                                      evidence=tuple(dict.fromkeys((*value.evidence, location))))
            # Reassigning a whole object invalidates descendants of the old slot.
            for stored in list(state.heap):
                if stored[0] == key[0] and len(stored[1]) > len(key[1]) and stored[1][:len(key[1])] == key[1]:
                    del state.heap[stored]
        elif isinstance(target, (ast.Tuple, ast.List)):
            for child in target.elts:
                self.assign(child, UNKNOWN, facts, state, initialization, True)

    def statements(self, nodes, facts, state, depth=0, initialization=False, uncertain=False):
        for node in nodes:
            self.tick()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.value(node.value, facts, state, depth)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    self.assign(target, value, facts, state, initialization, uncertain)
            elif isinstance(node, ast.Expr):
                before = dict(state.heap)
                self.value(node.value, facts, state, depth)
                if uncertain:
                    for key in set(before) | set(state.heap):
                        if before.get(key) != state.heap.get(key):
                            state.heap[key] = Value(changed=True)
            elif isinstance(node, (ast.AugAssign, ast.Delete)):
                for target in node.targets if isinstance(node, ast.Delete) else [node.target]:
                    self.assign(target, UNKNOWN, facts, state, initialization, True)
            elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)):
                if isinstance(node, ast.If):
                    condition = self.truth(node.test, facts, state)
                    if condition is not None:
                        self.statements(node.body if condition else node.orelse, facts, state,
                                        depth, initialization, uncertain)
                        continue
                # Branch/loop writes cannot be claimed definite after the block.
                # selected branches are handled by preceding() at the call site.
                for _, children in ast.iter_fields(node):
                    if isinstance(children, list):
                        for child in children:
                            if isinstance(child, ast.stmt):
                                self.statements([child], facts, state, depth, initialization, True)
                            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                                self.statements(child.body, facts, state, depth, initialization, True)
                if isinstance(node, ast.Try):
                    self.statements(node.finalbody, facts, state, depth, initialization, uncertain)
            elif isinstance(node, (ast.Return, ast.Raise)):
                break

    def method_state(self, method, obj, args, kwargs, state):
        local = state.copy()
        local.env = {}
        if obj:
            local.context = (*state.context, obj.identity)
        positional = [*method.node.args.posonlyargs, *method.node.args.args]
        supplied = ([obj] if obj else []) + args
        defaults = dict(zip([n.arg for n in positional[-len(method.node.args.defaults):]]
                            if method.node.args.defaults else [], method.node.args.defaults))
        facts = self.repo.files[method.rel_path]
        for i, param in enumerate(positional):
            default = self.value(defaults[param.arg], facts, state) if param.arg in defaults else UNKNOWN
            local.env[param.arg] = kwargs.get(param.arg, supplied[i] if i < len(supplied) else default)
        for param in method.node.args.kwonlyargs:
            local.env[param.arg] = kwargs.get(param.arg, UNKNOWN)
        return local

    def run_method(self, method, obj, args, kwargs, state, depth, initialization=False):
        if depth >= self.MAX_DEPTH or method.node.decorator_list:
            for value in [*([obj] if obj else []), *args, *kwargs.values()]:
                self.forget(value, state)
            return
        local = self.method_state(method, obj, args, kwargs, state)
        facts = self.repo.files[method.rel_path]
        self.statements(method.node.body, facts, local, depth, initialization)
        state.heap, state.uncertain = local.heap, local.uncertain

    def decorators_forward(self, method):
        key = method.symbol
        if key in self.decorator_cache:
            return self.decorator_cache[key]
        if not method.node.decorator_list:
            return True
        flow = ProjectCallableFlow(self.c, "")
        facts = self.repo.files[method.rel_path]
        initial = CallableState()
        scope = flow.module(facts, initial)
        flow.live_scopes = [scope]
        values = [(initial, CallableValue("original", tracked=True))]
        try:
            for node in reversed(method.node.decorator_list):
                following = []
                for state, wrapped in values:
                    for changed, decorator in flow.evaluate(node, facts, scope, state):
                        following.extend(flow.invoke(decorator, [wrapped], {}, changed, 0, self.location(facts, node)))
                values = flow.bounded(following)
            outcomes = set()
            for state, value in values:
                if not self.wrapper_preserves_arguments(value, flow, state):
                    self.decorator_cache[key] = False
                    return False
                state.effects = set()
                for final, _ in flow.invoke(replace(value, tracked=True), [], {}, state, 0,
                                            self.location(facts, method.node)):
                    outcomes.update(final.effects or {"unknown"})
            result = outcomes == {"delegate"}
        except Limit:
            result = False
        self.decorator_cache[key] = result
        return result

    def wrapper_preserves_arguments(self, value, flow, state):
        """A forwarding callable is not enough if it can mutate its receiver."""
        if value.kind != 'function':
            return value.kind == 'original'
        node, facts = value.node, value.facts
        names = {a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
        if node.args.vararg:
            names.add(node.args.vararg.arg)
        if node.args.kwarg:
            names.add(node.args.kwarg.arg)
        nodes = [n for n in ast.walk(node) if self.c.owner(facts, n) is node]
        for _ in range(3):
            for current in nodes:
                if isinstance(current, ast.Assign) and any(isinstance(n, ast.Name) and n.id in names
                                                          for n in ast.walk(current.value)):
                    names.update(n.id for target in current.targets for n in ast.walk(target) if isinstance(n, ast.Name))
        for current in nodes:
            if isinstance(current, (ast.Attribute, ast.Subscript)) and isinstance(current.ctx, (ast.Store, ast.Del)):
                return False
            if isinstance(current, ast.Call):
                takes_arguments = any(isinstance(n, ast.Name) and n.id in names
                                      for arg in [*current.args, *(k.value for k in current.keywords)] for n in ast.walk(arg))
                if takes_arguments and flow.reference(current.func, facts, value.scope, state).kind != 'original':
                    return False
        return True

    def at_method(self, method, obj, args, kwargs, state, caller, visited=frozenset()):
        if method.symbol in visited or len(visited) >= self.MAX_DEPTH:
            return []
        caller_facts, invocation = caller
        invoked_at = self.location(caller_facts, invocation)
        method_patches = [p for p in self.c.active_here(caller_facts, invocation)
                          if p.target and self.resolver.symbol(p.target).name == method.symbol]
        facts = self.repo.files[method.rel_path]
        sink_facts, sink_call = self.sink
        output = []
        for call in facts.nodes:
            if not isinstance(call, ast.Call) or self.c.owner(facts, call) is not method.node:
                continue
            local = self.method_state(method, obj, args, kwargs, state)
            preceding = [n for n in self.c.preceding(facts, call) if self.c.owner(facts, n) is method.node]
            self.statements(preceding, facts, local)
            if not self.feasible(facts, call, local):
                continue
            receiver = self.value(call.func, facts, local)
            if facts is sink_facts and call is sink_call:
                if not receiver.changed and obj.identity not in local.uncertain:
                    output.append(Context("clear", []))
                    continue
                state_name = "mocked" if receiver.kind == "mock" else (
                    "clear" if receiver.kind == "external" else "unresolved")
                if receiver.kind == "mock":
                    # A later side_effect/wraps assignment can make a Mock call
                    # real code. Its import alone is then insufficient evidence.
                    for suffix in ("side_effect", "wraps", "_mock_wraps"):
                        if (receiver.identity, (*receiver.path, suffix)) in local.heap:
                            state_name = "unresolved"
                if method_patches or obj.identity in local.uncertain or not self.decorators_forward(method):
                    state_name = "unresolved"
                output.append(Context(state_name, [{
                    "detail": "client object followed from construction to later method call",
                    "construction": self.focus, "invocation": invoked_at,
                    "method": method.symbol, "call": self.location(facts, call),
                    "receiver_kind": receiver.kind, "assignments": list(receiver.evidence),
                    "method_replacements": [p.location for p in method_patches],
                    "state": state_name,
                }]))
            elif receiver.kind == "method" and receiver.identity == obj.identity:
                nested = self.repo.functions[receiver.symbol]
                subargs = [self.value(n, facts, local) for n in call.args]
                subkwargs = {k.arg: self.value(k.value, facts, local) for k in call.keywords}
                contexts = self.at_method(nested, obj, subargs, subkwargs, local,
                                          (facts, call), visited | {method.symbol})
                if contexts and (method_patches or not self.decorators_forward(method)):
                    for context in contexts:
                        context.state = "unresolved"
                output.extend(contexts)
        return output

    def analyze(self):
        facts = self.repo.files[self.source["file_path"]]
        source_node = self.c.e.find_literal_node_id(
            facts, self.c.e.strip_quotes(self.source.get("matched_text", "")),
            int(self.source["line_number"]), int(self.source.get("column_start", "0")))
        literals = [n for n in facts.nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and self.c.e.literal_node(facts.rel_path, n.lineno, n.col_offset, n.value) == source_node]
        if len(literals) != 1:
            return None
        literal = literals[0]
        owner = self.c.owner(facts, literal)
        if owner is None:
            return None
        anchor = literal
        constructor = None
        while anchor in facts.parents:
            if isinstance(anchor, ast.Call) and self.symbol(facts, anchor.func) in self.repo.classes:
                constructor = anchor
                break
            if isinstance(anchor, ast.stmt):
                break
            anchor = facts.parents[anchor]
        if constructor is None:
            return None
        parent = facts.parents.get(constructor)
        if not isinstance(parent, (ast.Assign, ast.AnnAssign)) or parent.value is not constructor:
            return None
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        if not all(isinstance(n, ast.Name) for n in targets):
            return None
        self.focus = f"{facts.rel_path}:{constructor.lineno}:{constructor.col_offset}:0"
        contexts = []
        try:
            for call in facts.nodes:
                if (not isinstance(call, ast.Call) or call.lineno <= constructor.lineno
                        or self.c.owner(facts, call) is not owner or not isinstance(call.func, ast.Attribute)):
                    continue
                state = State()
                # Scope-local statements only. Imports are resolved from their
                # declarations without running any module or fixture code.
                preceding = [n for n in self.c.preceding(facts, call) if self.c.owner(facts, n) is owner]
                self.statements(preceding, facts, state)
                if not self.feasible(facts, call, state):
                    continue
                target = self.value(call.func, facts, state)
                if target.kind != "method" or target.identity != self.focus:
                    continue
                method = self.repo.functions[target.symbol]
                obj = self.value(call.func.value, facts, state)
                args = [self.value(n, facts, state) for n in call.args]
                kwargs = {k.arg: self.value(k.value, facts, state) for k in call.keywords}
                contexts.extend(self.at_method(method, obj, args, kwargs, state, (facts, call)))
        except Limit as error:
            # Never keep an apparent earlier mock when a later call could not
            # be checked within the bound.
            return Context("unresolved", [{"detail": str(error)}])
        if not contexts or not any(c.evidence for c in contexts):
            return None
        # A confirmed real call of this same instance keeps reuse. If different
        # possible object states remain, do not call the occurrence a proven mock.
        states = {c.state for c in contexts}
        state = "clear" if "clear" in states else "unresolved" if "unresolved" in states else "mocked"
        return Context(state, [e for c in contexts for e in c.evidence])
