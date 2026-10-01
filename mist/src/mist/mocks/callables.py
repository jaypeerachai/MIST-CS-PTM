"""Bounded AST interpretation of saved callables and wrapper factories.

No repository code is imported or executed. Values describe callable identity,
not runtime responses. Unknown effects on a tracked callable remain unknown.
This refines wrapper evidence only; the existing binding graph is not changed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field, replace

from mist.mocks.scope import MOCKS


FUNCTION_KIND_CHECKS = {
    "inspect.isgeneratorfunction": (False, True),
    "inspect.isasyncgenfunction": (True, True),
    "inspect.iscoroutinefunction": (True, False),
    "asyncio.iscoroutinefunction": (True, False),
}


def function_kind_test(name, node, facts, owner):
    """Evaluate a supported inspection call on a known function definition."""
    asynchronous = isinstance(node, ast.AsyncFunctionDef)
    generator = any(isinstance(item, (ast.Yield, ast.YieldFrom)) and owner(facts, item) is node
                    for item in ast.walk(node))
    return (asynchronous, generator) == FUNCTION_KIND_CHECKS[name]


@dataclass(frozen=True, eq=False)
class Value:
    kind: str = "unknown"
    name: str = ""
    node: object = None
    facts: object = None
    scope: str = ""
    data: object = None
    tracked: bool = False


UNKNOWN = Value()
NONE = Value("data", data=None)


@dataclass
class State:
    env: dict = field(default_factory=dict)
    target: Value = field(default_factory=lambda: Value("original", tracked=True))
    effects: set = field(default_factory=set)
    opaque: bool = False
    evidence: tuple = ()
    returned: bool = False
    result: Value = UNKNOWN

    def fork(self):
        return State({s: dict(v) for s, v in self.env.items()}, self.target,
                     set(self.effects), self.opaque, self.evidence,
                     self.returned, self.result)


class Limit(Exception):
    pass


class CallableFlow:
    """Interpret only relevant, visible Python code under explicit limits."""

    MAX_STATES = 32
    MAX_STEPS = 12000
    MAX_DEPTH = 24

    def __init__(self, checker, target, object_id=""):
        self.c = checker
        self.target = target
        self.object_id = object_id
        self.scopes = {}
        self.counter = 0
        self.steps = 0
        self.loading = set()
        self.stack = []
        self.live_scopes = []
        self._free_names = {}
        self._relevant_functions = {}
        modules = {}
        for facts in self.c.repo.files.values():
            modules.setdefault(facts.module, []).append(facts)
        self.modules = {name: files[0] for name, files in modules.items() if len(files) == 1}

    def tick(self):
        self.steps += 1
        if self.steps > self.MAX_STEPS:
            raise Limit("statement budget")

    def bounded(self, values):
        if len(values) > 1:
            unique = {}
            for item in values:
                state = item if isinstance(item, State) else item[0]
                key = (self.state_key(state), () if isinstance(item, State) else
                       tuple(self.value_key(v, state) for v in item[1:]))
                unique.setdefault(key, item)
            values = list(unique.values())
        if len(values) > self.MAX_STATES:
            raise Limit("branch budget")
        return values

    def value_key(self, value, state, seen=frozenset()):
        if isinstance(value, list):
            return tuple(self.value_key(v, state, seen) for v in value)
        if isinstance(value, dict):
            return tuple(sorted((str(k), self.value_key(v, state, seen)) for k, v in value.items()))
        if not isinstance(value, Value):
            return value
        simple = (value.kind, value.name, repr(value.data), value.tracked)
        if value.kind != "function":
            return simple
        identity = (value.facts.rel_path, value.node.lineno, value.node.col_offset)
        if id(value.node) in seen:
            return simple, identity, "recursive"
        if id(value.node) not in self._free_names:
            nodes = list(ast.walk(value.node))
            # Include nested-function references conservatively. Removing every
            # nested argument/store could merge different captured callables.
            self._free_names[id(value.node)] = {n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        closure = tuple((name, self.value_key(self.lookup(name, value.scope, state), state, seen | {id(value.node)}))
                        for name in sorted(self._free_names[id(value.node)]))
        return simple, identity, closure

    def state_key(self, state):
        relevant = set(self.live_scopes) | {s for s in state.env if s.startswith("module:")}
        scopes = tuple((scope, tuple(sorted((name, self.value_key(value, state))
                                           for name, value in state.env.get(scope, {}).items())))
                       for scope in sorted(relevant))
        return (self.value_key(state.target, state), scopes, tuple(sorted(state.effects)),
                state.opaque, state.returned, self.value_key(state.result, state))

    def relevant_function(self, function, args, kwargs, state):
        if any(v.tracked or v.kind == "function" for v in [*args, *kwargs.values()]):
            return True
        if any((v := self.lookup(n.id, function.scope, state)).tracked or v.kind == "function"
               for n in ast.walk(function.node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
            return True
        key = (function.facts.rel_path, id(function.node))
        if key not in self._relevant_functions:
            nodes = list(ast.walk(function.node))
            # A factory can return a callable without receiving one as an argument.
            nested = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not function.node for n in nodes)
            accesses = any(isinstance(n, ast.Attribute) and
                           self.c.assignment_address(function.facts, n, n) == (self.target, self.object_id)
                           for n in nodes)
            indirect = any(any((w.target, w.object_id) == (self.target, self.object_id)
                               for w in self.c.helper_writes(function.facts, n))
                           for n in nodes if isinstance(n, ast.Call))
            self._relevant_functions[key] = nested or accesses or indirect
        return self._relevant_functions[key]

    def symbol(self, name):
        for _ in range(8):
            other = name
            parts = name.split('.')
            for count in range(len(parts) - 1, 0, -1):
                facts = self.modules.get('.'.join(parts[:count]))
                if facts is None or facts.tree is None:
                    continue
                for stmt in facts.tree.body:
                    if isinstance(stmt, ast.ImportFrom):
                        for alias in stmt.names:
                            if (alias.asname or alias.name) == parts[count]:
                                other = '.'.join([self.import_module(facts, stmt), alias.name, *parts[count + 1:]])
                if other != name:
                    break
            if not other or other == name:
                break
            name = other
        return Value("symbol", name=name)

    def import_module(self, facts, stmt):
        current = facts.module + ('.__init__' if facts.rel_path.endswith('/__init__.py') else '')
        return self.c.e.resolve_relative_module(current, stmt.module or '', stmt.level)

    def scope(self, facts, parent=None, node=None):
        self.counter += 1
        key = f"scope:{self.counter}"
        self.scopes[key] = (facts, parent, node)
        return key

    def module(self, facts, state):
        key = "module:" + facts.rel_path
        if key in state.env:
            return key
        self.scopes[key] = (facts, None, None)
        state.env[key] = {}
        # Read module declarations, not arbitrary module-level executions.
        for stmt in facts.tree.body if facts.tree else []:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self.imports(stmt, facts, key, state)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                state.env[key][stmt.name] = (UNKNOWN if stmt.decorator_list else
                                             Value("function", node=stmt, facts=facts, scope=key))
            elif isinstance(stmt, ast.ClassDef):
                state.env[key][stmt.name] = self.symbol(facts.module + "." + stmt.name)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                value = NONE if stmt.value is None else self.constant(stmt.value)
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        state.env[key][target.id] = value
        return key

    @staticmethod
    def constant(node):
        if isinstance(node, ast.Constant):
            return Value("data", data=node.value)
        return UNKNOWN

    def imports(self, stmt, facts, scope, state):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                state.env[scope][alias.asname or alias.name.split('.')[0]] = self.symbol(
                    alias.name if alias.asname else alias.name.split('.')[0])
        else:
            module = self.import_module(facts, stmt)
            for alias in stmt.names:
                state.env[scope][alias.asname or alias.name] = self.symbol(module + "." + alias.name)

    def lookup(self, name, scope, state):
        while scope:
            if name in state.env.get(scope, {}):
                value = state.env[scope][name]
                if value.kind == "symbol" and value.name == self.target:
                    return replace(state.target, tracked=True)
                return value
            scope = self.scopes[scope][1]
        if name in {"callable", "hasattr", "getattr", "setattr", "type", "isinstance", "dict", "list", "tuple", "str", "len"}:
            return self.symbol("builtins." + name)
        return UNKNOWN

    def function(self, value, state):
        if value.kind == "function":
            return value
        if value.kind != "symbol":
            return None
        function = self.c.repo.functions.get(value.name)
        if function is None or function.node.decorator_list:
            return None
        facts = self.c.repo.files[function.rel_path]
        return Value("function", node=function.node, facts=facts,
                     scope=self.module(facts, state))

    def address(self, node, facts, scope, state):
        if not isinstance(node, ast.Attribute):
            return None
        raw = self.c.assignment_address(facts, node, node)
        # The client resolver preserves object identities for constructed clients.
        if raw and raw == (self.target, self.object_id):
            return raw
        base = self.reference(node.value, facts, scope, state)
        if base.kind == "symbol":
            return base.name + "." + node.attr, ""
        return raw

    def reference(self, node, facts, scope, state):
        if isinstance(node, ast.Name):
            return self.lookup(node.id, scope, state)
        if isinstance(node, ast.Attribute):
            address = self.address(node, facts, scope, state)
            if address == (self.target, self.object_id):
                return replace(state.target, tracked=True)
            if address:
                return self.symbol(address[0])
        return UNKNOWN

    def assign(self, target, value, facts, scope, state):
        if isinstance(target, ast.Name):
            _, _, owner = self.scopes[scope]
            declarations = [n for n in ast.walk(owner) if isinstance(n, (ast.Global, ast.Nonlocal))
                            and self.c.owner(facts, n) is owner] if owner else []
            globals_ = {name for n in declarations if isinstance(n, ast.Global) for name in n.names}
            nonlocals = {name for n in declarations if isinstance(n, ast.Nonlocal) for name in n.names}
            destination = self.module(facts, state) if target.id in globals_ else scope
            if target.id in nonlocals:
                destination = self.scopes[scope][1]
                while destination and target.id not in state.env.get(destination, {}):
                    destination = self.scopes[destination][1]
                if destination is None:
                    state.effects.add('unknown')
                    return
            state.env[destination][target.id] = value
        elif isinstance(target, ast.Attribute):
            if self.address(target, facts, scope, state) == (self.target, self.object_id):
                state.target = replace(value, tracked=True)
                state.evidence += (self.c.location(facts, target),)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for child in target.elts:
                self.assign(child, UNKNOWN, facts, scope, state)

    def evaluate(self, node, facts, scope, state, depth=0):
        self.tick()
        if node is None:
            return [(state, NONE)]
        if isinstance(node, (ast.Name, ast.Attribute)):
            value = self.reference(node, facts, scope, state)
            if isinstance(node, ast.Attribute) and value.kind == "unknown":
                # An unknown descriptor can execute code while reading a field.
                state.opaque = True
            return [(state, value)]
        if isinstance(node, ast.Constant):
            return [(state, self.constant(node))]
        if isinstance(node, ast.Lambda):
            return [(state, Value("function", node=node, facts=facts, scope=scope))]
        if isinstance(node, ast.Await):
            return self.evaluate(node.value, facts, scope, state, depth)
        if isinstance(node, ast.Call):
            return self.call(node, facts, scope, state, depth)
        if isinstance(node, ast.IfExp):
            output = []
            for current, truth in self.test(node.test, facts, scope, state, depth):
                for branch in ([node.body] if truth is True else [node.orelse] if truth is False else [node.body, node.orelse]):
                    output.extend(self.evaluate(branch, facts, scope, current.fork(), depth))
            return self.bounded(output)
        # Evaluate child calls even when their data value is irrelevant.
        states = [state]
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                states = [next_state for current in states for next_state, _ in self.evaluate(child, facts, scope, current, depth)]
                self.bounded(states)
        return [(current, self.constant(node)) for current in states]

    def test(self, node, facts, scope, state, depth):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return [(s, None if t is None else not t) for s, t in self.test(node.operand, facts, scope, state, depth)]
        if isinstance(node, ast.BoolOp):
            result = [(state, isinstance(node.op, ast.And))]
            for child in node.values:
                next_result = []
                for current, truth in result:
                    if (isinstance(node.op, ast.And) and truth is False) or (isinstance(node.op, ast.Or) and truth is True):
                        next_result.append((current, truth))
                    else:
                        for changed, other in self.test(child, facts, scope, current, depth):
                            if isinstance(node.op, ast.And):
                                value = False if other is False else True if truth is True and other is True else None
                            else:
                                value = True if other is True else False if truth is False and other is False else None
                            next_result.append((changed, value))
                result = self.bounded(next_result)
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            output = []
            for current, left in self.evaluate(node.left, facts, scope, state, depth):
                for changed, right in self.evaluate(node.comparators[0], facts, scope, current, depth):
                    truth = None
                    if left.kind == right.kind == "data":
                        if isinstance(node.ops[0], (ast.Is, ast.Eq)):
                            truth = left.data == right.data
                        elif isinstance(node.ops[0], (ast.IsNot, ast.NotEq)):
                            truth = left.data != right.data
                    elif right.kind == "data" and right.data is None and left.kind in {"function", "original", "mock", "symbol"}:
                        if isinstance(node.ops[0], ast.Is): truth = False
                        if isinstance(node.ops[0], ast.IsNot): truth = True
                    output.append((changed, truth))
            return output
        return [(s, bool(v.data) if v.kind == "data" else None)
                for s, v in self.evaluate(node, facts, scope, state, depth)]

    def call(self, node, facts, scope, state, depth):
        if isinstance(node.func, ast.Attribute) and node.func.attr in {'start', 'stop'}:
            patcher = self.c.resolve(node.func.value, facts, node)
            if patcher.kind == 'patcher' and isinstance(patcher.node, ast.Call):
                patch = self.c.patch_from_call(facts, patcher.node)
                if patch is not None and (not patch.target or (patch.target, patch.target_object) == (self.target, self.object_id)):
                    # Started patch lifecycles are not interpreted here. Do not
                    # let a captured alias silently keep the unpatched identity.
                    state.target = replace(UNKNOWN, tracked=True)
                    return [(state, UNKNOWN)]
        # Binding a known Python function to a receiver keeps its callable identity.
        if isinstance(node.func, ast.Attribute) and node.func.attr == "__get__":
            values = self.evaluate(node.func.value, facts, scope, state, depth)
            return [(s, v if v.kind in {"function", "original"} else UNKNOWN) for s, v in values]
        output = []
        for current, function in self.evaluate(node.func, facts, scope, state, depth):
            arguments = [(current, [], {})]
            for arg in node.args:
                arguments = [(s, [*args, value], kwargs) for before, args, kwargs in arguments
                             for s, value in self.evaluate(arg.value if isinstance(arg, ast.Starred) else arg, facts, scope, before, depth)]
                self.bounded(arguments)
            for kw in node.keywords:
                arguments = [(s, args, {**kwargs, kw.arg: value}) for before, args, kwargs in arguments
                             for s, value in self.evaluate(kw.value, facts, scope, before, depth)]
                self.bounded(arguments)
            for changed, args, kwargs in arguments:
                # Only import-verified metadata decorators get this treatment.
                # functools.wraps does not establish forwarding by itself.
                if function.name == "functools.wraps":
                    output.append((changed, Value("metadata_decorator")))
                    continue
                if function.name in FUNCTION_KIND_CHECKS and args:
                    arg = args[0]
                    if arg.kind == "function":
                        truth = function_kind_test(function.name, arg.node, arg.facts, self.c.owner)
                        output.append((changed, Value("data", data=truth)))
                        continue
                if function.name == "builtins.callable" and args:
                    value = args[0]
                    truth = True if value.kind in {"function", "original", "mock"} else False if value.kind == "data" else None
                    output.append((changed, UNKNOWN if truth is None else Value("data", data=truth)))
                    continue
                if function.name == 'inspect.signature' and args and args[0].kind == 'function':
                    # Introspection does not invoke this statically known function.
                    output.append((changed, UNKNOWN))
                    continue
                if (function.name == 'builtins.setattr' and len(args) == 3
                        and args[0].kind == 'function' and args[1].kind == 'data'
                        and isinstance(args[1].data, str) and not args[1].data.startswith('__')):
                    # Ordinary metadata on a Python function does not replace its body.
                    output.append((changed, NONE))
                    continue
                if function.name in MOCKS:
                    if "side_effect" in kwargs:
                        output.append((changed, UNKNOWN))
                    elif "wraps" in kwargs:
                        output.append((changed, kwargs["wraps"]))
                    else:
                        output.append((changed, Value("mock")))
                    continue
                output.extend(self.invoke(function, args, kwargs, changed, depth + 1,
                                          self.c.location(facts, node)))
        return self.bounded(output)

    def invoke(self, value, args, kwargs, state, depth, location):
        if value.kind == "metadata_decorator":
            return [(state, args[0] if args else UNKNOWN)]
        if value.kind in {"original", "mock"}:
            state.effects.add("delegate" if value.kind == "original" else "mock")
            state.evidence += (location,)
            return [(state, UNKNOWN)]
        function = self.function(value, state)
        if function is None:
            state.opaque = True
            if value.tracked or any(v.tracked or v.kind == "function" for v in [*args, *kwargs.values()]):
                state.effects.add("unknown")
            return [(state, UNKNOWN)]
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) and self.c.owner(function.facts, n) is function.node
               for n in ast.walk(function.node)):
            # Calling a generator does not execute its body yet.
            state.effects.add('unknown')
            return [(state, UNKNOWN)]
        if (not value.tracked and not isinstance(function.node, ast.Lambda)
                and not self.relevant_function(function, args, kwargs, state)):
            state.opaque = True
            return [(state, UNKNOWN)]
        key = (function.facts.rel_path, id(function.node), function.scope)
        if depth > self.MAX_DEPTH or key in self.stack:
            state.effects.add("unknown")
            return [(state, UNKNOWN)]
        self.stack.append(key)
        try:
            return self.invoke_function(function, value.tracked, args, kwargs, state, depth, location)
        finally:
            self.stack.pop()

    def invoke_function(self, function, tracked, args, kwargs, state, depth, location):
        facts, node = function.facts, function.node
        scope = self.scope(facts, function.scope, node)
        state.env[scope] = {}
        self.live_scopes.append(scope)
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = dict(zip([a.arg for a in positional[-len(node.args.defaults):]] if node.args.defaults else [], node.args.defaults))
        defaults.update({a.arg: v for a, v in zip(node.args.kwonlyargs, node.args.kw_defaults) if v is not None})
        for index, arg in enumerate([*positional, *node.args.kwonlyargs]):
            default = self.constant(defaults[arg.arg]) if arg.arg in defaults else UNKNOWN
            state.env[scope][arg.arg] = kwargs.get(arg.arg, args[index] if index < len(args) and index < len(positional) else default)
        if node.args.vararg: state.env[scope][node.args.vararg.arg] = UNKNOWN
        if node.args.kwarg: state.env[scope][node.args.kwarg.arg] = UNKNOWN
        saved_effects, saved_opaque = set(state.effects), state.opaque
        state.effects, state.opaque = set(), False
        state.returned, state.result = False, NONE
        if isinstance(node, ast.Lambda):
            returned = []
            for current, result in self.evaluate(node.body, facts, scope, state, depth):
                current.result, current.returned = result, True
                returned.append(current)
        else:
            returned = self.block(node.body, facts, scope, [state], depth)
        output = []
        for current in returned:
            result = current.result if current.returned else NONE
            if tracked and not current.effects:
                current.effects.add("unknown" if current.opaque else "stub")
            effects = current.effects | saved_effects
            current.effects, current.opaque = effects, saved_opaque or current.opaque
            if tracked:
                current.evidence += (location, self.c.location(facts, node))
            current.returned, current.result = False, NONE
            output.append((current, result))
        self.live_scopes.pop()
        return self.bounded(output)

    def decorate(self, node, facts, scope, state, depth):
        value = Value("function", node=node, facts=facts, scope=scope)
        states = [(state, value)]
        for decorator in reversed(node.decorator_list):
            output = []
            for current, wrapped in states:
                for changed, factory in self.evaluate(decorator, facts, scope, current, depth):
                    output.extend(self.invoke(factory, [wrapped], {}, changed, depth + 1,
                                              self.c.location(facts, decorator)))
            states = self.bounded(output)
        return states

    def block(self, statements, facts, scope, states, depth=0):
        for stmt in statements:
            next_states = []
            for state in states:
                if state.returned:
                    next_states.append(state)
                else:
                    next_states.extend(self.statement(stmt, facts, scope, state, depth))
            states = self.bounded(next_states)
        return states

    def statement(self, stmt, facts, scope, state, depth):
        self.tick()
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            self.imports(stmt, facts, scope, state)
            return [state]
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            output = []
            for current, function in self.decorate(stmt, facts, scope, state, depth):
                current.env[scope][stmt.name] = function
                output.append(current)
            return output
        if isinstance(stmt, ast.ClassDef):
            state.env[scope][stmt.name] = self.symbol(facts.module + "." + stmt.name)
            return [state]
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)):
            values = self.evaluate(stmt.value, facts, scope, state, depth)
            output = []
            for current, value in values:
                if isinstance(stmt, ast.Return):
                    current.returned, current.result = True, value
                elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                        self.assign(target, value, facts, scope, current)
                output.append(current)
            return output
        if isinstance(stmt, ast.If):
            output = []
            for current, truth in self.test(stmt.test, facts, scope, state, depth):
                branches = [stmt.body] if truth is True else [stmt.orelse] if truth is False else [stmt.body, stmt.orelse]
                for branch in branches:
                    output.extend(self.block(branch, facts, scope, [current.fork()], depth))
            return self.bounded(output)
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            states = [(state, [])]
            for item in stmt.items:
                expr = item.context_expr
                resolved = self.c.resolve(expr, facts, expr)
                call = expr if isinstance(expr, ast.Call) else resolved.node if resolved.kind == 'patcher' else None
                patch = self.c.patch_from_call(facts, call) if isinstance(call, ast.Call) else None
                relevant = patch is not None and (not patch.target or
                            (patch.target, patch.target_object) == (self.target, self.object_id))
                next_states = []
                for current, restore in states:
                    if relevant:
                        original = current.target
                        current.target = (Value('mock', tracked=True) if patch.replacement == 'mock' and patch.target else
                                          original if patch.replacement == 'delegate' else replace(UNKNOWN, tracked=True))
                        current.evidence += (self.c.location(facts, expr),)
                        if item.optional_vars:
                            self.assign(item.optional_vars, current.target, facts, scope, current)
                        next_states.append((current, [*restore, original]))
                    else:
                        for changed, _ in self.evaluate(expr, facts, scope, current, depth):
                            if item.optional_vars:
                                self.assign(item.optional_vars, UNKNOWN, facts, scope, changed)
                            next_states.append((changed, restore))
                states = self.bounded(next_states)
            output = []
            for current, restore in states:
                for changed in self.block(stmt.body, facts, scope, [current], depth):
                    for original in reversed(restore):
                        changed.target = original
                    output.append(changed)
            return self.bounded(output)
        if isinstance(stmt, ast.Try):
            output = [state.fork()]
            prefixes = [state.fork()]
            for child in stmt.body:
                output = self.block([child], facts, scope, output, depth)
                prefixes.extend(current.fork() for current in output if not current.returned)
                prefixes = self.bounded(prefixes)
            output = self.block(stmt.orelse, facts, scope, output, depth)
            for handler in stmt.handlers:
                output.extend(self.block(handler.body, facts, scope, [current.fork() for current in prefixes], depth))
            # Finally runs even following a return.
            result = []
            for current in self.bounded(output):
                was_returned, returned_value = current.returned, current.result
                current.returned = False
                for final in self.block(stmt.finalbody, facts, scope, [current], depth):
                    if not final.returned:
                        final.returned, final.result = was_returned, returned_value
                    result.append(final)
            return self.bounded(result)
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            # Do not assume an iteration occurs. Forget values written in loops
            # rather than keeping a stale saved-callable identity.
            for n in ast.walk(stmt):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    self.assign(n, UNKNOWN, facts, scope, state)
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
                    self.assign(n, UNKNOWN, facts, scope, state)
                if isinstance(n, ast.Call):
                    if any((w.target, w.object_id) == (self.target, self.object_id)
                           for w in self.c.helper_writes(facts, n)):
                        state.target = UNKNOWN
                    function = self.reference(n.func, facts, scope, state)
                    captures = (function.kind == 'function' and any(
                        self.lookup(child.id, function.scope, state).tracked
                        for child in ast.walk(function.node)
                        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)))
                    if function.tracked or captures:
                        state.effects.add('unknown')
            state.opaque = True
            return [state]
        if isinstance(stmt, (ast.AugAssign, ast.Delete)):
            targets = stmt.targets if isinstance(stmt, ast.Delete) else [stmt.target]
            for target in targets:
                self.assign(target, UNKNOWN, facts, scope, state)
            return [state]
        if isinstance(stmt, (ast.Global, ast.Nonlocal, ast.Pass, ast.Assert, ast.Break, ast.Continue)):
            return [state]
        if isinstance(stmt, ast.Raise):
            state.returned, state.result = True, UNKNOWN
            state.effects.add("unknown")
            return [state]
        state.opaque = True
        return [state]

    def at(self, facts, point, expression=None):
        """Classify the tracked callable after the visible preceding statements."""
        state = State()
        module = self.module(facts, state)
        owner = self.c.owner(facts, point)
        scope = self.scope(facts, module, owner) if owner else module
        state.env.setdefault(scope, {})
        self.live_scopes = [scope]
        try:
            states = self.block(self.c.preceding(facts, point), facts, scope, [state])
            outcomes, evidence = set(), []
            for current in states:
                current.effects, current.opaque = set(), False
                values = [(current, current.target)] if expression is None else self.evaluate(expression, facts, scope, current)
                for changed, value in values:
                    value = replace(value, tracked=True)
                    for final, _ in self.invoke(value, [], {}, changed, 0, self.c.location(facts, point)):
                        outcomes.update(final.effects or {"unknown"})
                        evidence.extend(final.evidence)
            kind = "delegate" if outcomes == {"delegate"} else "stub" if outcomes and outcomes <= {"stub", "mock"} else "unknown"
            return kind, tuple(dict.fromkeys(evidence)), ""
        except Limit as error:
            return "unknown", (), str(error)
