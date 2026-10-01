"""Bounded proof for supplied project clients that produce local responses.

This extra check does not construct or change a binding. It checks a supplied
object at an already recorded sink. Unknown code is not evidence of a fake.
No repository code is imported or executed.
"""

import ast
import operator
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from mist.mocks.client_arguments import ClientFlow
from mist.mocks.client_values import State, Value, UNKNOWN
from mist.mocks.scope import Context
from mist.mocks.callables import Limit


class Unsupported(Exception):
    pass


@dataclass(frozen=True)
class Data(Value):
    items: tuple = ()


class LocalClientFlow(ClientFlow):
    """Reconstruct local client objects and check their patches and responses."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.strict = False
        self.proof_locations = []
        self.diagnostic = ''
        self.unsafe_allocations = set()
        self.active_patches = []
        self.patch_objects = {}
        self.patch_evidence = []

    def fixture_state(self, facts, owner, state):
        """Recognize pytest's builtin fixture, not an arbitrary parameter type.

        Fixture/API names are framework semantics. They are not project names
        or an assumption that a variable containing 'mock' holds a mock.
        """
        path = PurePosixPath(facts.rel_path)
        if (not owner.name.startswith('test') or not
                (path.name.startswith('test_') or path.name.endswith('_test.py'))
                or 'monkeypatch' not in self.c.e.function_arg_names(owner)):
            return
        imported = any(isinstance(n, ast.Import) and any(a.name == 'pytest' for a in n.names)
                       or isinstance(n, ast.ImportFrom) and n.module == 'pytest'
                       for n in facts.tree.body)
        if not imported:
            return
        if any(p.target.startswith(('pytest.MonkeyPatch', '_pytest.monkeypatch.MonkeyPatch'))
               for p in self.active_patches):
            return
        for candidate in self.repo.files.values():
            other = PurePosixPath(candidate.rel_path)
            visible = candidate is facts or (other.name == 'conftest.py' and
                      (other.parent == path.parent or other.parent in path.parent.parents))
            if not visible or candidate.tree is None:
                continue
            for node in ast.walk(candidate.tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in {'monkeypatch', 'pytest_generate_tests'}:
                        return
                    for decorator in node.decorator_list:
                        if isinstance(decorator, ast.Call) and self.c.resolve(decorator.func, candidate, decorator).symbol == 'pytest.fixture':
                            names = [k.value for k in decorator.keywords if k.arg == 'name']
                            if any(not isinstance(n, ast.Constant) or n.value == 'monkeypatch' for n in names):
                                return
                if isinstance(node, ast.Call) and self.c.resolve(node.func, candidate, node).symbol == 'pytest.mark.parametrize':
                    # An explicitly parametrized or indirectly supplied fixture
                    # is not necessarily pytest's builtin object.
                    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                        return
                    if 'monkeypatch' in node.args[0].value.replace(' ', '').split(','):
                        return
        state.env['monkeypatch'] = Value('patcher', 'pytest.MonkeyPatch',
                                       'fixture:' + self.location(facts, owner))

    def raising_replacement(self, expression, facts, state):
        resolved = self.c.resolve(expression, facts, expression)
        function = resolved.node
        if not isinstance(function, ast.FunctionDef) or function.decorator_list:
            return None
        defaults = [*function.args.defaults, *(d for d in function.args.kw_defaults if d is not None)]
        if any(not isinstance(d, ast.Constant) for d in defaults):
            return None
        annotations = [a.annotation for a in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
                       if a.annotation is not None]
        if function.returns is not None:
            annotations.append(function.returns)
        if any(not isinstance(a, ast.Constant) and not
               (isinstance(a, ast.Name) and a.id in {'object', 'str', 'int', 'bool', 'float'}
                and self.builtin(a.id, facts, function, state)) for a in annotations):
            return None
        body = [n for n in function.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                                                and isinstance(n.value.value, str))]
        if (len(body) != 1 or not isinstance(body[0], ast.Raise) or body[0].cause is not None
                or body[0].exc is None or not self.exception_value(body[0].exc, facts, state)):
            return None
        return Value('raise_only', evidence=(self.location(facts, function),), changed=True)

    def patch_call(self, node, facts, state, depth):
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {'setattr', 'undo'}:
            return None
        # Only a known fixture/constructor value reaches this branch. Unknown
        # APIs with the same spelling fall through to normal uncertainty rules.
        if not isinstance(node.func.value, ast.Name):
            return None
        patcher = state.env.get(node.func.value.id, UNKNOWN)
        if patcher.kind != 'patcher' or patcher.identity in state.uncertain:
            return None
        if any(identity == patcher.identity for identity, _ in state.heap):
            return None
        if node.func.attr == 'undo':
            for obj in self.patch_objects.get(patcher.identity, {}).values():
                self.forget(obj, state)
            return Value('literal', 'None')
        if (len(node.args) != 3 or any(k.arg != 'raising' or not isinstance(k.value, ast.Constant)
                                    or not isinstance(k.value.value, bool) for k in node.keywords)):
            return None
        name = node.args[1]
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str) or name.value.startswith('__'):
            return None
        obj = self.value(node.args[0], facts, state, depth)
        replacement = self.raising_replacement(node.args[2], facts, state)
        if (obj.kind != 'object' or obj.identity in state.uncertain or not self.plain_route(obj.symbol)
                or not self.method(obj.symbol, name.value) or replacement is None):
            return None
        target = ast.copy_location(ast.Attribute(value=node.args[0], attr=name.value, ctx=ast.Store()), node)
        self.assign(target, replacement, facts, state)
        self.patch_objects.setdefault(patcher.identity, {})[obj.identity] = obj
        self.patch_evidence.append({'patch': self.location(facts, node), 'field': name.value,
                                    'replacement': list(replacement.evidence),
                                    'detail': 'verified pytest patch of a helper that only raises a builtin exception'})
        return Value('literal', 'None')

    @staticmethod
    def forget(value, state, seen=frozenset()):
        if value.identity in seen:
            return
        if value.kind in {'list', 'tuple', 'dict'}:
            for child in getattr(state.heap.get((value.identity, ('items',))), 'items', ()):
                LocalClientFlow.forget(child, state, seen | {value.identity})
        ClientFlow.forget(value, state)

    def assign(self, target, value, facts, state, initialization=False, uncertain=False):
        if isinstance(target, ast.Subscript):
            container = self.value(target.value, facts, state)
            # A subscript write may alter a response list or an instance's
            # __dict__. Do not retain the old contents as proof of a local stub.
            self.forget(container, state)
            if container.identity:
                state.uncertain.add(container.identity)
            if isinstance(target.value, ast.Attribute):
                receiver = self.value(target.value.value, facts, state)
                if receiver.identity:
                    state.uncertain.add(receiver.identity)
            return
        return super().assign(target, value, facts, state, initialization, uncertain)

    def run_method(self, method, obj, args, kwargs, state, depth, initialization=False):
        if initialization and obj and self.plain_class(obj.symbol):
            before = self.strict
            self.strict = True
            try:
                trial = state.copy()
                self.invoke_local(method, obj, args, kwargs, trial, depth)
                state.heap, state.uncertain = trial.heap, trial.uncertain
                return
            except Unsupported:
                self.unsafe_allocations.add(obj.identity)
            finally:
                self.strict = before
        return super().run_method(method, obj, args, kwargs, state, depth, initialization)

    def plain_class(self, symbol):
        cls = self.repo.classes.get(symbol)
        if cls is None or cls.node.decorator_list or cls.node.keywords:
            return False
        facts = self.repo.files[cls.rel_path]
        if any(self.symbol(facts, b) not in {'object', 'builtins.object'} for b in cls.node.bases):
            return False
        for n in cls.node.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.decorator_list or n.name in {'__new__', '__getattribute__', '__getattr__', '__setattr__',
                                                 '__delattr__', '__del__', '__bool__', '__len__'}:
                    return False
            elif not (isinstance(n, ast.Pass) or isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)):
                return False
        return True

    def runtime_object_base(self, base, facts, before):
        """Accept a literal object base, including a TYPE_CHECKING-only alias."""
        if isinstance(base, ast.Name) and base.id == 'object':
            return self.builtin('object', facts, base, State())
        if not isinstance(base, ast.Name):
            return False
        answer = False
        for node in facts.tree.body:
            if node.lineno >= before:
                break
            selected = [node]
            if isinstance(node, ast.If):
                condition = self.c.resolve(node.test, facts, node.test)
                if condition.symbol == 'typing.TYPE_CHECKING':
                    selected = node.orelse
                elif any(isinstance(n, ast.Name) and n.id == base.id and isinstance(n.ctx, ast.Store) for n in ast.walk(node)):
                    answer = False
            for item in selected:
                if isinstance(item, (ast.Assign, ast.AnnAssign)):
                    targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                    if any(isinstance(t, ast.Name) and t.id == base.id for t in targets):
                        answer = (isinstance(item.value, ast.Name) and item.value.id == 'object'
                                  and self.builtin('object', facts, item, State()))
        return answer

    def plain_route(self, symbol):
        cls = self.repo.classes.get(symbol)
        if cls is None or cls.node.decorator_list or cls.node.keywords:
            return False
        facts = self.repo.files[cls.rel_path]
        if any(not self.runtime_object_base(b, facts, cls.node.lineno) for b in cls.node.bases):
            return False
        for node in cls.node.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.decorator_list or node.name in {'__new__', '__getattribute__', '__getattr__', '__setattr__', '__delattr__'}:
                    return False
            elif not (isinstance(node, ast.Pass) or isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
                return False
        return True

    def builtin(self, name, facts, node, state):
        if name in state.env:
            return False
        # Do not trust a builtin spelling when the module or function binds it.
        for n in facts.nodes:
            if (isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, (ast.Store, ast.Del))
                    or isinstance(n, ast.arg) and n.arg == name
                    or isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
                    or isinstance(n, ast.alias) and (n.asname or n.name.split('.')[0]) == name):
                return False
        return not any(p.target == 'builtins.' + name for p in self.c.active_here(facts, node))

    def container(self, kind, items, facts, node, state):
        identity = f'local:{facts.rel_path}:{node.lineno}:{node.col_offset}:{self.serial}'
        self.serial += 1
        state.heap[(identity, ('items',))] = Data('items', items=tuple(items))
        return Value(kind, identity=identity)

    def items(self, value, state):
        stored = state.heap.get((value.identity, ('items',)))
        if value.identity in state.uncertain or not isinstance(stored, Data):
            raise Unsupported('container contents no longer established')
        return stored.items

    def attribute(self, value, name, state):
        if value.kind in {'list', 'tuple', 'dict'}:
            if value.kind == 'list' and name == 'append':
                return Value('append', identity=value.identity)
            if self.strict:
                raise Unsupported('unsupported container operation')
        if self.strict and value.kind == 'object':
            if not self.plain_class(value.symbol) or value.identity in state.uncertain:
                raise Unsupported('object lookup is not established')
        result = super().attribute(value, name, state)
        if self.strict and result.kind == 'unknown' and value.kind != 'unknown':
            raise Unsupported('unknown object field')
        return result

    def value_truth(self, value, state):
        if value.kind == 'literal':
            return bool(ast.literal_eval(value.symbol))
        if value.kind in {'list', 'tuple', 'dict'}:
            return bool(self.items(value, state))
        if value.kind == 'object' and self.plain_class(value.symbol):
            return True
        return None

    def numeric(self, value):
        return value.kind == 'number' or value.kind == 'literal' and type(ast.literal_eval(value.symbol)) in {int, float}

    def value(self, node, facts, state, depth=0):
        self.tick()
        if depth >= self.MAX_DEPTH:
            raise Unsupported('local response depth limit')
        if isinstance(node, (ast.List, ast.Tuple, ast.Dict)):
            if isinstance(node, ast.Dict):
                if any(k is None for k in node.keys):
                    if self.strict:
                        raise Unsupported('dictionary expansion')
                    return UNKNOWN
                elements = [*node.keys, *node.values]
                kind = 'dict'
            else:
                elements, kind = node.elts, 'list' if isinstance(node, ast.List) else 'tuple'
            values = [self.value(n, facts, state, depth) for n in elements]
            return self.container(kind, values, facts, node, state)
        if isinstance(node, ast.BoolOp):
            for expression in node.values:
                value = self.value(expression, facts, state, depth)
                if expression is node.values[-1]:
                    return value
                truth = self.value_truth(value, state)
                if truth is None:
                    if self.strict:
                        raise Unsupported('unknown truth conversion')
                    return UNKNOWN
                if truth == isinstance(node.op, ast.Or):
                    return value
        if isinstance(node, ast.Compare) and self.strict:
            values = [self.value(n, facts, state, depth) for n in [node.left, *node.comparators]]
            if not all(self.numeric(v) for v in values):
                raise Unsupported('comparison might invoke project code')
            comparisons = {ast.Gt: operator.gt, ast.GtE: operator.ge, ast.Lt: operator.lt,
                           ast.LtE: operator.le, ast.Eq: operator.eq, ast.NotEq: operator.ne}
            if len(node.ops) == 1 and type(node.ops[0]) in comparisons and all(v.kind == 'literal' for v in values):
                answer = comparisons[type(node.ops[0])](*(ast.literal_eval(v.symbol) for v in values))
                return Value('literal', repr(answer))
            return Value('boolean')
        if isinstance(node, ast.BinOp) and self.strict:
            values = [self.value(n, facts, state, depth) for n in (node.left, node.right)]
            if not all(self.numeric(v) for v in values) or not isinstance(node.op, (ast.Add, ast.Sub)):
                raise Unsupported('unsupported arithmetic')
            return Value('number')
        if isinstance(node, ast.Subscript) and self.strict:
            sequence = self.value(node.value, facts, state, depth)
            index = self.value(node.slice, facts, state, depth)
            if sequence.kind not in {'list', 'tuple'} or not self.numeric(index):
                raise Unsupported('unsupported subscription')
            items = self.items(sequence, state)
            if not items:
                # Indexing a known empty builtin sequence can only stop here.
                # Keep checking later syntax conservatively, without inventing
                # a response object or invoking an unknown iterator.
                return Value('stopped')
            if index.kind == 'literal':
                try:
                    return items[ast.literal_eval(index.symbol)]
                except (IndexError, TypeError):
                    raise Unsupported('unknown sequence index')
            if len(set(items)) != 1:
                raise Unsupported('multiple possible sequence values')
            return items[0]
        if isinstance(node, ast.Call):
            if not self.strict:
                patched = self.patch_call(node, facts, state, depth)
                if patched is not None:
                    return patched
                if (not node.args and not node.keywords and
                        self.c.resolve(node.func, facts, node).symbol == 'pytest.MonkeyPatch'
                        and not any(p.target.startswith('pytest.MonkeyPatch') for p in self.active_patches)):
                    return Value('patcher', 'pytest.MonkeyPatch', 'patcher:' + self.location(facts, node))
                selected = UNKNOWN
                if isinstance(node.func, ast.Name):
                    selected = state.env.get(node.func.id, UNKNOWN)
                elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    obj = state.env.get(node.func.value.id, UNKNOWN)
                    if obj.identity not in state.uncertain:
                        selected = state.heap.get((obj.identity, (node.func.attr,)), UNKNOWN)
                if selected.kind == 'raise_only':
                    # Python still evaluates arguments, but the verified body
                    # cannot modify the client or forward to a real SDK.
                    for argument in [*node.args, *(k.value for k in node.keywords)]:
                        self.value(argument, facts, state, depth)
                    return UNKNOWN
            if isinstance(node.func, ast.Name) and node.func.id in {'list', 'len'} and self.builtin(node.func.id, facts, node, state):
                args = [self.value(n, facts, state, depth) for n in node.args]
                if node.keywords or len(args) != 1 or args[0].kind not in {'list', 'tuple', 'dict'}:
                    if self.strict:
                        raise Unsupported('builtin could invoke unknown code')
                    return UNKNOWN
                if node.func.id == 'len':
                    return Value('literal', repr(len(self.items(args[0], state))))
                return self.container('list', self.items(args[0], state), facts, node, state)
            if self.strict:
                if any(isinstance(n, ast.Starred) for n in node.args) or any(k.arg is None for k in node.keywords):
                    raise Unsupported('expanded call arguments')
                function = self.value(node.func, facts, state, depth)
                args = [self.value(n, facts, state, depth) for n in node.args]
                kwargs = {k.arg: self.value(k.value, facts, state, depth) for k in node.keywords}
                if function.kind == 'append' and len(args) == 1 and not kwargs:
                    key = (function.identity, ('items',))
                    state.heap[key] = Data('items', items=(*self.items(function, state), args[0]))
                    return Value('literal', 'None')
                if function.kind == 'symbol' and self.plain_class(function.symbol):
                    obj = Value('object', function.symbol,
                                f'proof:{facts.rel_path}:{node.lineno}:{self.serial}')
                    self.serial += 1
                    constructor = self.method(obj.symbol, '__init__')
                    if constructor:
                        self.invoke_local(constructor, obj, args, kwargs, state, depth + 1)
                    elif args or kwargs:
                        raise Unsupported('object has no matching constructor')
                    return obj
                if function.kind == 'method':
                    method = self.repo.functions[function.symbol]
                    obj = Value('object', method.class_symbol, function.identity)
                    return self.invoke_local(method, obj, args, kwargs, state, depth + 1)
                raise Unsupported('call may delegate to external or unknown code')
            # Preserve local list writes when reconstructing a supplied client.
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'append':
                function = self.value(node.func, facts, state, depth)
                if function.kind == 'append' and len(node.args) == 1 and not node.keywords:
                    value = self.value(node.args[0], facts, state, depth)
                    key = (function.identity, ('items',))
                    state.heap[key] = Data('items', items=(*self.items(function, state), value))
                    return Value('literal', 'None')
        if self.strict and not isinstance(node, (ast.Name, ast.Attribute, ast.Constant, ast.Await)) and node is not None:
            raise Unsupported('unsupported response expression')
        return super().value(node, facts, state, depth)

    def method_state(self, method, obj, args, kwargs, state):
        local = super().method_state(method, obj, args, kwargs, state)
        facts = self.repo.files[method.rel_path]
        for arg, default in zip(method.node.args.kwonlyargs, method.node.args.kw_defaults):
            if arg.arg not in kwargs and default is not None:
                local.env[arg.arg] = self.value(default, facts, state)
        if method.node.args.kwarg:
            # Unknown values can be stored for logging, but never called or
            # treated as a known response by the local-response proof.
            local.env[method.node.args.kwarg.arg] = UNKNOWN
        return local

    def invoke_local(self, method, obj, args, kwargs, state, depth=0):
        if (depth >= self.MAX_DEPTH or not self.plain_class(obj.symbol)
                or obj.identity in state.uncertain or obj.identity in self.unsafe_allocations):
            raise Unsupported('unknown client implementation')
        if (obj.identity, (method.node.name,)) in state.heap:
            raise Unsupported('instance method replaced')
        if any(not p.target_object and p.target.startswith(obj.symbol + '.')
               for p in self.active_patches):
            raise Unsupported('class method or constructor replaced')
        if method.node.decorator_list:
            raise Unsupported('decorated client method')
        facts = self.repo.files[method.rel_path]
        self.proof_locations.append(self.location(facts, method.node))
        local = self.method_state(method, obj, args, kwargs, state)
        outcomes = self.local_block(method.node.body, facts, local, depth)
        live = [(s, value) for s, value, aborted in outcomes if not aborted]
        if not live:
            return Value('literal', 'None')
        # Branches with different responses or state are left unresolved.
        first_state, first_value = live[0]
        if any(v != first_value or s.heap != first_state.heap for s, v in live[1:]):
            raise Unsupported('different possible response states')
        state.heap, state.uncertain = first_state.heap, first_state.uncertain
        return first_value

    def local_block(self, nodes, facts, state, depth):
        for index, node in enumerate(nodes):
            self.tick()
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.value(node.value, facts, state, depth)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if not isinstance(target, (ast.Name, ast.Attribute)):
                        raise Unsupported('unsupported assignment target')
                    self.assign(target, value, facts, state, initialization=True)
            elif isinstance(node, ast.AugAssign) and isinstance(node.op, (ast.Add, ast.Sub)):
                if not self.numeric(self.value(node.target, facts, state, depth)) or not self.numeric(self.value(node.value, facts, state, depth)):
                    raise Unsupported('unknown update operation')
                self.assign(node.target, Value('number'), facts, state, initialization=True)
            elif isinstance(node, ast.Expr):
                self.value(node.value, facts, state, depth)
            elif isinstance(node, ast.Return):
                return [(state, self.value(node.value, facts, state, depth) if node.value else Value('literal', 'None'), False)]
            elif isinstance(node, ast.If):
                test = self.value(node.test, facts, state, depth)
                truth = self.value_truth(test, state)
                if truth is None and test.kind != 'boolean':
                    raise Unsupported('unknown branch condition')
                branches = [node.body, node.orelse] if truth is None else [node.body if truth else node.orelse]
                output = []
                for branch in branches:
                    output.extend(self.local_block([*branch, *nodes[index + 1:]], facts, state.copy(), depth))
                return output
            elif isinstance(node, ast.Raise):
                if node.exc is not None and not self.exception_value(node.exc, facts, state):
                    raise Unsupported('unknown exception constructor')
                return [(state, Value('literal', 'None'), True)]
            elif not isinstance(node, ast.Pass):
                raise Unsupported('unsupported client statement')
        return [(state, Value('literal', 'None'), False)]

    def closed_response(self, value, state, seen=frozenset()):
        """Check returned objects, including their async/streaming methods.

        A local allocation alone is not enough: a returned object could later
        delegate to an SDK. Unknown methods, external bases, and stored response
        replacement prevent this bounded proof.
        """
        self.tick()
        if value.kind in {'literal', 'number', 'boolean', 'stopped'}:
            return
        if value.kind in {'list', 'tuple', 'dict'}:
            for child in self.items(value, state):
                self.closed_response(child, state, seen)
            return
        if (value.kind != 'object' or not self.plain_class(value.symbol)
                or value.identity in state.uncertain or value.identity in self.unsafe_allocations):
            raise Unsupported('returned value is not a closed local response')
        if value.identity in seen:
            return
        if len(seen) >= self.MAX_DEPTH:
            raise Unsupported('response object depth limit')
        methods = [m for m in self.repo.functions.values() if m.class_symbol == value.symbol and m.node.name != '__init__']
        reads, writes, container_writes = set(), [], set()
        for method in methods:
            positional = [*method.node.args.posonlyargs, *method.node.args.args]
            if not positional:
                raise Unsupported('method receiver not explicit')
            receiver = positional[0].arg
            facts = self.repo.files[method.rel_path]
            for node in ast.walk(method.node):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == receiver:
                    if isinstance(node.ctx, ast.Load):
                        parent = facts.parents.get(node)
                        if isinstance(parent, ast.Attribute) and parent.attr == 'append':
                            container_writes.add(node.attr)
                        else:
                            reads.add(node.attr)
                    else:
                        writes.append((node.attr, facts.parents.get(node)))
                if isinstance(node, (ast.Global, ast.Nonlocal, ast.Delete, ast.Yield, ast.YieldFrom)):
                    raise Unsupported('unsupported response lifetime or mutation')
        if container_writes & reads:
            raise Unsupported('response container may change between calls')
        for name, statement in writes:
            if name in reads and not (isinstance(statement, ast.AugAssign) and isinstance(statement.op, (ast.Add, ast.Sub))
                                      and self.numeric(state.heap.get((value.identity, (name,)), UNKNOWN))):
                raise Unsupported('response field may be replaced between calls')
        for method in methods:
            positional = [*method.node.args.posonlyargs, *method.node.args.args][1:]
            local = state.copy()
            for name, statement in writes:
                if isinstance(statement, ast.AugAssign):
                    # Iterators can be called repeatedly. Do not prove their
                    # safety using only the first counter value.
                    local.heap[(value.identity, (name,))] = Value('number')
            result = self.invoke_local(method, value, [UNKNOWN] * len(positional), {}, local)
            self.closed_response(result, local, seen | {value.identity})
        # Data-only response classes may themselves contain other response objects.
        for (identity, path), child in state.heap.items():
            if identity == value.identity and len(path) == 1:
                self.closed_response(child, state, seen | {value.identity})

    def analyze(self):
        facts = self.repo.files[self.source['file_path']]
        source_id = self.c.e.find_literal_node_id(facts, self.c.e.strip_quotes(self.source.get('matched_text', '')),
                                                int(self.source['line_number']), int(self.source.get('column_start', '0')))
        literals = [n for n in facts.nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and self.c.e.literal_node(facts.rel_path, n.lineno, n.col_offset, n.value) == source_id]
        if len(literals) != 1:
            return None
        owner = self.c.owner(facts, literals[0])
        anchor = literals[0]
        while anchor in facts.parents and not isinstance(anchor, ast.stmt):
            anchor = facts.parents[anchor]
            if isinstance(anchor, ast.Call):
                break
        if owner is None or not isinstance(anchor, ast.Call) or not isinstance(anchor.func, ast.Attribute):
            return None
        sink_facts, sink_call = self.sink
        sink_owner = self.c.owner(sink_facts, sink_call)
        # This extra proof supports an explicit call to the method containing
        # the sink, on a plain project object. Rule out other routes before
        # replaying preceding statements. Receiver aliases still work.
        methods = [m for m in self.repo.functions.values() if m.node is sink_owner]
        if len(methods) != 1:
            return None
        sink_method = methods[0]
        if (sink_method.node.decorator_list
                or not self.plain_route(sink_method.class_symbol)):
            return None
        state = State()
        try:
            self.active_patches = [*self.c.active_here(facts, anchor), *self.c.active_here(sink_facts, sink_call)]
            self.fixture_state(facts, owner, state)
            self.statements([n for n in self.c.preceding(facts, anchor) if self.c.owner(facts, n) is owner], facts, state)
            target = self.value(anchor.func, facts, state)
            if target.kind != 'method':
                return None
            method = self.repo.functions[target.symbol]
            if method.node is not sink_owner or method.node.decorator_list or not self.plain_route(method.class_symbol):
                return None
            if any(not p.target_object and p.target.startswith(method.class_symbol + '.') for p in self.active_patches):
                return None
            obj = self.value(anchor.func.value, facts, state)
            context_node = anchor
            while context_node in facts.parents:
                context_node = facts.parents[context_node]
                if isinstance(context_node, (ast.With, ast.AsyncWith)):
                    for item in context_node.items:
                        for expression in ast.walk(item.context_expr):
                            if isinstance(expression, ast.Call):
                                for argument in [*expression.args, *(k.value for k in expression.keywords)]:
                                    self.forget(self.value(argument, facts, state), state)
                if context_node is owner:
                    break
            if obj.identity in state.uncertain or not self.feasible(facts, anchor, state):
                return None
            args = [self.value(n, facts, state) for n in anchor.args]
            kwargs = {k.arg: self.value(k.value, facts, state) for k in anchor.keywords}
            local = self.method_state(method, obj, args, kwargs, state)
            self.statements([n for n in self.c.preceding(sink_facts, sink_call) if self.c.owner(sink_facts, n) is sink_owner], sink_facts, local)
            if not self.feasible(sink_facts, sink_call, local):
                return None
            receiver = self.value(sink_call.func, sink_facts, local)
            if receiver.kind != 'method' or receiver.identity == obj.identity:
                return None
            supplied_method = self.repo.functions[receiver.symbol]
            supplied_obj = Value('object', supplied_method.class_symbol, receiver.identity)
            evidence = {'detail': 'supplied project client checked through its returned response implementation',
                        'invocation': self.location(facts, anchor), 'call': self.location(sink_facts, sink_call),
                        'client_method': supplied_method.symbol}
            if self.patch_evidence:
                evidence['helper_patches'] = self.patch_evidence
            # An active patch can change the implementation we are about to read.
            if any(p.target in {target.symbol, receiver.symbol} and not p.target_object
                   for p in [*self.c.active_here(facts, anchor), *self.c.active_here(sink_facts, sink_call)]):
                return Context('unresolved', [{**evidence, 'detail': 'supplied client method is replaced'}])
            self.strict = True
            call_args = [self.value(n, sink_facts, local) for n in sink_call.args]
            call_kwargs = {k.arg: self.value(k.value, sink_facts, local) for k in sink_call.keywords}
            result = self.invoke_local(supplied_method, supplied_obj, call_args, call_kwargs, local)
            self.closed_response(result, local)
            return Context('mocked', [{**evidence, 'state': 'mocked',
                                       'response_implementation': list(dict.fromkeys(self.proof_locations))}])
        except (Unsupported, Limit) as error:
            # Do not turn unknown code or an SDK delegation into proof of non-use.
            # The existing checker remains authoritative when this extra proof
            # does not establish a closed local response.
            self.diagnostic = str(error)
            return None
