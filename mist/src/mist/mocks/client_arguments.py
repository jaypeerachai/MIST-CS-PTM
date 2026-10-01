"""Follow client arguments toward the selected sink."""

import ast
import builtins
from dataclasses import replace

from mist.mocks.client_values import (
    ClientFlow as ValueFlow, State, Value, UNKNOWN,
)
from mist.mocks.scope import Context, MOCKS
from mist.mocks.callables import (
    Limit, State as CallableState, Value as CallableValue,
)
from mist.mocks.wrapper_arguments import ForwardingFlow, ArgumentSafety


class ClientFlow(ValueFlow):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._owned_calls = {}
        self._route_cache = {}
        self._shadowed = {}
        self._initializing = False

    def run_method(self, method, obj, args, kwargs, state, depth, initialization=False):
        before = self._initializing
        self._initializing = initialization
        try:
            return super().run_method(method, obj, args, kwargs, state, depth, initialization)
        finally:
            self._initializing = before

    def exception_value(self, node, facts, state):
        if isinstance(node, ast.Name) and state.env.get(node.id, UNKNOWN).kind == 'exception':
            return True
        expression = node.func if isinstance(node, ast.Call) else node
        if isinstance(node, ast.Call) and (node.keywords or not all(isinstance(n, ast.Constant) for n in node.args)):
            return False
        symbol = self.symbol(facts, expression)
        name = symbol.removeprefix('builtins.')
        if isinstance(expression, ast.Name) and not symbol.startswith('builtins.'):
            name = expression.id
            if facts.rel_path not in self._shadowed:
                self._shadowed[facts.rel_path] = {
                    n.id for n in facts.nodes if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
                } | {n.name for n in facts.nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))} | {
                    n.arg for n in facts.nodes if isinstance(n, ast.arg)
                } | {n.asname or n.name.split('.')[0] for n in facts.nodes if isinstance(n, ast.alias)}
            if name in state.env or name in self._shadowed[facts.rel_path]:
                return False
        elif not symbol.startswith('builtins.'):
            return False
        kind = getattr(builtins, name, None)
        if not isinstance(kind, type) or not issubclass(kind, BaseException):
            return False
        if any(p.target == 'builtins.' + name for p in self.c.active_here(facts, expression)):
            return False
        return True

    def value(self, node, facts, state, depth=0):
        if isinstance(node, (ast.Name, ast.Attribute, ast.Call)) and self.exception_value(node, facts, state):
            self.tick()
            return Value('exception')
        # Follow a visible, single-base super initializer using the same object.
        # Multiple inheritance and explicit/dynamic super arguments stay outside
        # this rule. No project code is executed.
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == '__init__' and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name) and node.func.value.func.id == 'super'
                and not node.func.value.args and not node.func.value.keywords):
            owner = self.c.owner(facts, node)
            method = next((m for m in self.repo.functions.values() if m.node is owner), None)
            cls = self.repo.classes.get(method.class_symbol) if method else None
            positional = [*owner.args.posonlyargs, *owner.args.args] if owner else []
            obj = state.env.get(positional[0].arg) if positional else None
            # Do not interpret a shadowed builtin super.
            unshadowed = 'super' not in state.env and not any(
                isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == 'super'
                or isinstance(n, ast.Name) and n.id == 'super' and isinstance(n.ctx, ast.Store)
                or isinstance(n, ast.alias) and (n.asname or n.name) == 'super'
                for n in ast.walk(facts.tree))
            if cls and len(cls.node.bases) == 1 and obj and unshadowed and depth < self.MAX_DEPTH:
                base = self.symbol(facts, cls.node.bases[0])
                constructor = self.method(base, '__init__')
                if constructor:
                    args = [self.value(n, facts, state, depth) for n in node.args]
                    kwargs = {k.arg: self.value(k.value, facts, state, depth) for k in node.keywords}
                    self.run_method(constructor, obj, args, kwargs, state, depth + 1, initialization=self._initializing)
                    return UNKNOWN
        result = super().value(node, facts, state, depth)
        if isinstance(node, ast.Call) and result.kind == 'unknown':
            options = {k.arg: k.value for k in node.keywords}
            side_effect = options.get('side_effect')
            if (side_effect is not None and 'wraps' not in options
                    and self.exception_value(side_effect, facts, state)
                    and self.value(node.func, facts, state, depth).symbol in MOCKS):
                # A statically known builtin exception makes this Mock raise,
                # not call a real client. Callable/iterable effects remain unknown.
                return replace(result, kind='mock')
        return result

    def plain_fields(self, obj, state):
        """Literal fields on classes without custom lookup or descriptors."""
        fields = {path[0] for (identity, path), value in state.heap.items()
                  if identity == obj.identity and len(path) == 1 and value.kind == 'literal'}
        pending, seen = [obj.symbol], set()
        while pending:
            symbol = pending.pop()
            if symbol in seen:
                continue
            seen.add(symbol)
            cls = self.repo.classes.get(symbol)
            if cls is None:
                if symbol not in {'object', 'builtins.object', 'abc.ABC'}:
                    return None
                continue
            if cls.node.decorator_list or cls.node.keywords:
                return None
            facts = self.repo.files[cls.rel_path]
            for node in cls.node.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in {'__class__', '__getattribute__', '__getattr__', '__setattr__', '__bool__', '__len__'}:
                        return None
                    fields.discard(node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        if isinstance(target, ast.Name):
                            if target.id == '__class__':
                                return None
                            fields.discard(target.id)
            pending.extend(self.symbol(facts, base) for base in cls.node.bases)
        return fields

    def method_forwards(self, method, obj, state):
        if not method.node.decorator_list:
            return True
        if super().decorators_forward(method):
            return True
        fields = self.plain_fields(obj, state)
        if fields is None:
            return False
        key = method.symbol + '|literal fields:' + ','.join(sorted(fields))
        if key in self.decorator_cache:
            return self.decorator_cache[key]
        flow = ForwardingFlow(self.c, '')
        facts = self.repo.files[method.rel_path]
        initial = CallableState()
        scope = flow.module(facts, initial)
        flow.live_scopes = [scope]
        values = [(initial, CallableValue('original', node=method.node, facts=facts, tracked=True))]
        try:
            for node in reversed(method.node.decorator_list):
                following = []
                for current, wrapped in values:
                    for changed, decorator in flow.evaluate(node, facts, scope, current):
                        following.extend(flow.invoke(decorator, [wrapped], {}, changed, 0, self.location(facts, node)))
                values = flow.bounded(following)
            outcomes = set()
            for current, value in values:
                if value.kind != 'function' or not ArgumentSafety(value, flow, current, fields).check():
                    self.decorator_cache[key] = False
                    return False
                current.effects = set()
                for final, _ in flow.invoke(replace(value, tracked=True), [], {}, current, 0,
                                            self.location(facts, method.node)):
                    outcomes.update(final.effects or {'unknown'})
            result = 'delegate' in outcomes and outcomes <= {'delegate', 'abort'}
        except Limit:
            result = False
        self.decorator_cache[key] = result
        return result

    def owned_calls(self, method):
        if method.symbol not in self._owned_calls:
            facts = self.repo.files[method.rel_path]
            self._owned_calls[method.symbol] = [
                n for n in ast.walk(method.node)
                if isinstance(n, ast.Call) and self.c.owner(facts, n) is method.node
            ]
        return self._owned_calls[method.symbol]

    def may_reach(self, method, class_symbol, visited=frozenset()):
        """A conservative route filter, not a new source-to-sink graph."""
        key = (method.symbol, class_symbol)
        if key in self._route_cache:
            return self._route_cache[key]
        if method.symbol in visited or len(visited) >= self.MAX_DEPTH:
            return True  # A bound is not proof that the sink is unreachable.
        for call in self.owned_calls(method):
            if call is self.sink[1]:
                self._route_cache[key] = True
                return True
            # Name calls can be aliases or supplied bound methods. Retain them.
            if not isinstance(call.func, ast.Attribute):
                return True
            nested = self.method(class_symbol, call.func.attr)
            if nested and self.may_reach(nested, class_symbol, visited | {method.symbol}):
                return True
        self._route_cache[key] = False
        return False

    def relevant_calls(self, method, class_symbol):
        for call in self.owned_calls(method):
            if call is self.sink[1] or not isinstance(call.func, ast.Attribute):
                yield call
            else:
                nested = self.method(class_symbol, call.func.attr)
                if nested and self.may_reach(nested, class_symbol):
                    yield call

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
        for call in self.relevant_calls(method, obj.symbol):
            local = self.method_state(method, obj, args, kwargs, state)
            preceding = [n for n in self.c.preceding(facts, call) if self.c.owner(facts, n) is method.node]
            # Irrelevant calls are not investigated as routes, but their earlier
            # mutations still run through the same conservative state checks.
            self.statements(preceding, facts, local)
            if not self.feasible(facts, call, local):
                continue
            receiver = self.value(call.func, facts, local)
            if facts is sink_facts and call is sink_call:
                if not receiver.changed and obj.identity not in local.uncertain:
                    return [Context("clear", [])]
                state_name = "mocked" if receiver.kind == "mock" else (
                    "clear" if receiver.kind == "external" else "unresolved")
                if receiver.kind == "mock":
                    for suffix in ("side_effect", "wraps", "_mock_wraps"):
                        setting = local.heap.get((receiver.identity, (*receiver.path, suffix)))
                        if setting is not None and not (suffix == 'side_effect' and setting.kind == 'exception'):
                            state_name = "unresolved"
                if method_patches or obj.identity in local.uncertain or not self.method_forwards(method, obj, state):
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
                if contexts and (method_patches or not self.method_forwards(method, obj, state)):
                    for context in contexts:
                        context.state = "unresolved"
                output.extend(contexts)
            if any(context.state == "clear" for context in output):
                return output
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
        anchor, constructor = literal, None
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
                # The reuse criterion is existential. Once this same instance
                # reaches the recorded sink with real provenance, later calls
                # cannot undo that earlier invocation.
                if any(context.state == "clear" for context in contexts):
                    return Context("clear", [e for c in contexts for e in c.evidence])
        except Limit as error:
            return Context("unresolved", [{"detail": str(error)}])
        if not contexts or not any(c.evidence for c in contexts):
            return None
        states = {c.state for c in contexts}
        state = "unresolved" if "unresolved" in states else "mocked"
        return Context(state, [e for c in contexts for e in c.evidence])
