"""Combine mock evidence with bounded constructor and object-patch checks."""
import ast
from dataclasses import asdict, replace
from unittest.mock import patch as temporary_attribute

from mist.mocks.guards import MockDecision as GuardedDecision
from mist.mocks.context_guard import ContextGuard
from mist.mocks.scope import Context, Value, MOCKS
from mist.mocks.client_values import ClientFlow as ValueFlow
from mist.mocks.client_arguments import ClientFlow


def plain_instance(scope, symbol, seen=frozenset()):
    """Reject custom attribute lookup or unknown base classes."""
    if symbol in {'object', 'builtins.object', 'abc.ABC'}:
        return True
    if symbol in seen or len(seen) >= 8:
        return False
    cls = scope.repo.classes.get(symbol)
    if cls is None or cls.node.decorator_list or cls.node.keywords:
        return False
    names = {n.name for n in cls.node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if names & {'__getattr__', '__getattribute__', '__setattr__', '__new__'}:
        return False
    facts = scope.repo.files[cls.rel_path]
    return all(plain_instance(scope, scope.resolve(base, facts, cls.node).symbol, seen | {symbol}) for base in cls.node.bases)


class ObjectPatchScope(GuardedDecision):
    """Resolve instance fields assigned during test setup."""

    def resolve(self, expr, facts, point=None, seen=frozenset()):
        value = super().resolve(expr, facts, point, seen)
        if value.kind != 'unknown' or not isinstance(expr, ast.Attribute):
            return value
        point = point or expr
        chain = self.e.call_chain(expr)
        owner = self.owner(facts, point)
        cls = self.e.enclosing_class(point, facts.parents)
        if not owner or not cls or not owner.name.startswith('test'):
            return value
        parameters = [*owner.args.posonlyargs, *owner.args.args]
        if not parameters or not chain.startswith(parameters[0].arg + '.'):
            return value
        # Reject custom lookup and test setup whose execution is unclear.
        if cls.decorator_list or cls.keywords:
            return value
        names = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if names & {'__getattr__', '__getattribute__', '__setattr__', '__new__'}:
            return value
        if cls.bases:
            if len(cls.bases) != 1 or super().resolve(cls.bases[0], facts, cls, seen).symbol not in {
                    'unittest.TestCase', 'unittest.IsolatedAsyncioTestCase'}:
                return value
            setups = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'setUp']
        else:
            if not cls.name.startswith('Test') or '__init__' in names:
                return value
            setups = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'setup_method']
        if len(setups) != 1 or setups[0].decorator_list:
            return value
        setup = setups[0]
        params = [*setup.args.posonlyargs, *setup.args.args]
        if not params:
            return value
        field = chain.split('.')[1]
        # The test may overwrite or delete a field set during setup.
        # The inherited resolver handles direct writes before this call.
        for stmt in self.preceding(facts, point):
            if self.owner(facts, stmt) is not owner:
                continue
            if any(isinstance(n, ast.Call) and any(isinstance(arg, ast.Name) and arg.id == parameters[0].arg
                   for arg in ast.walk(n)) for n in ast.walk(stmt)):
                return value
            if any(isinstance(n, (ast.Attribute, ast.Name)) and isinstance(n.ctx, (ast.Store, ast.Del))
                   and self.e.call_chain(n) in {parameters[0].arg, parameters[0].arg + '.' + field}
                   for n in ast.walk(stmt)):
                return value
        target = params[0].arg + '.' + field
        writes = []
        for stmt in setup.body:
            if isinstance(stmt, (ast.Expr, ast.Pass)) and (not isinstance(stmt, ast.Expr) or isinstance(stmt.value, ast.Constant)):
                continue
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                return value
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if any(self.e.call_chain(t) == target for t in targets):
                writes.append(stmt)
        if len(writes) != 1:
            return value
        stmt = writes[0]
        known = super().resolve(stmt.value, facts, stmt, seen)
        if known.kind != 'instance' or not known.object_id:
            return value
        if not plain_instance(self, known.symbol):
            return value
        for attr in chain.split('.')[2:]:
            known = replace(known, symbol=known.symbol + '.' + attr)
        return known

    def patch_from_call(self, facts, call):
        parsed = super().patch_from_call(facts, call)
        if parsed is None:
            return None
        options = {k.arg: k.value for k in call.keywords}
        if 'return_value' in options and self.resolve(options['return_value'], facts, call).kind != 'mock':
            parsed.replacement = 'unknown'
        return parsed

    def active_here(self, facts, point, include_fixtures=True):
        result = super().active_here(facts, point, include_fixtures)
        # Share pytest-mock recognition with the object proof.
        guard = object.__new__(ContextGuard)
        guard.c = self
        guard.repo = self.repo
        owner = self.owner(facts, point)
        added = []
        for stmt in self.preceding(facts, point):
            if self.owner(facts, stmt) is not owner:
                continue
            call = stmt.value if isinstance(stmt, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Attribute) and call.func.attr in {'stop', 'stopall'}:
                added = []
            parsed = guard.mocker_call(facts, call)
            if parsed:
                # A constructor explicitly returning a real/unknown object is
                # not proven mocked merely because patch() itself is a mock.
                options = {k.arg: k.value for k in call.keywords}
                if 'return_value' in options:
                    returned = self.resolve(options['return_value'], facts, call)
                    if returned.kind != 'mock' and not self.mock_factory(options['return_value'], facts, call):
                        parsed.replacement = 'unknown'
                added.append(parsed)
        return [*result, *added]

    def mock_factory(self, expr, facts, point):
        if isinstance(expr, ast.Name):
            value = self.resolve(expr, facts, point)
            expr = value.node
        if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
            return False
        if expr.func.attr not in {'Mock', 'MagicMock', 'AsyncMock'}:
            return False
        if any(k.arg in {'side_effect', 'wraps', None} for k in expr.keywords):
            return False
        receiver = expr.func.value
        owner = self.owner(facts, expr)
        if not isinstance(receiver, ast.Name) or owner is None:
            return False
        for param in [*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs]:
            if param.arg == receiver.id and param.annotation is not None:
                return self.resolve(param.annotation, facts, owner).symbol == 'pytest_mock.MockerFixture'
        return False


class ProofFlow(ClientFlow):
    """Follow an active constructor replacement into the stored client."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inherited = []
        self.guard = object.__new__(ContextGuard)
        self.guard.c = self.c
        self.guard.repo = self.repo

    def value(self, node, facts, state, depth=0):
        if not isinstance(node, ast.Call):
            return super().value(node, facts, state, depth)
        inherited = list(self.inherited)
        # Carry lexical patches only while evaluating a project call, not to
        # unrelated later calls. Attribute identity remains in the flow heap.
        symbol = self.symbol(facts, node.func)
        project = symbol in self.repo.classes or symbol in self.repo.functions
        if project:
            self.inherited = [*inherited, *self.c.active_here(facts, node)]
        try:
            result = super().value(node, facts, state, depth)
        finally:
            self.inherited = inherited
        if result.kind != 'external':
            return result
        addresses = self.guard.addresses(facts, node.func)
        relevant = [p for p in [*inherited, *self.c.active_here(facts, node)]
                    if any(self.guard.covers(p.lookup_target or p.target, a) for a in addresses)]
        if not relevant:
            return result
        certain = all(p.replacement == 'mock' and not p.uncertain_scope for p in relevant)
        return replace(result, kind='mock' if certain else 'unknown', changed=True,
                       evidence=tuple(dict.fromkeys((*result.evidence, *(p.location for p in relevant)))))

    def at_method(self, method, obj, args, kwargs, state, caller, visited=frozenset()):
        if not plain_instance(self.c, obj.symbol):
            return [Context('unresolved', [{'detail': 'custom or unknown instance lookup'}])]
        facts, call = caller
        # patch.object must refer to this selected object, not another instance
        # of the same class. Unsupported object expressions remain unresolved.
        for p in self.c.active_here(facts, call):
            if p.target != method.symbol or p.replacement != 'mock' or p.uncertain_scope:
                continue
            receiver = self.c.resolve(call.func, facts, call)
            if p.target_object and p.target_object != receiver.object_id:
                continue
            if not p.target_object and p.lookup_target != self.c.lookup_address(facts, call.func):
                continue
            if self.may_reach(method, obj.symbol):
                return [Context('mocked', [{'detail': 'selected project method is explicitly replaced',
                                            'patch': p.location, 'target': p.target, 'invocation': self.location(facts, call),
                                            'object': p.target_object, 'state': 'mocked'}])]
        return super().at_method(method, obj, args, kwargs, state, caller, visited)


class MockDecision(GuardedDecision):
    RETRY_STEPS = 24000

    def object_call_proof(self, scope, source):
        facts = self.repo.files[source['file_path']]
        literals = [n for n in facts.nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.lineno == int(source['line_number']) and n.col_offset == int(source.get('column_start', 0))
                    and n.value == self.e.strip_quotes(source.get('matched_text', ''))]
        if len(literals) != 1:
            return None
        node = literals[0]
        while node in facts.parents:
            node = facts.parents[node]
            if isinstance(node, ast.stmt):
                return None
            if not isinstance(node, ast.Call):
                continue
            value = scope.resolve(node.func, facts, node)
            if not value.object_id:
                return None
            if not plain_instance(scope, value.symbol.rsplit('.', 1)[0]):
                return None
            patches = scope.active_here(facts, node)
            if any(not p.target or p.uncertain_scope for p in patches):
                return None
            matches = [p for p in patches if p.target_object == value.object_id and p.target == value.symbol]
            if matches and all(p.replacement == 'mock' for p in matches):
                return Context('mocked', [{'detail': 'same object and method replaced at the source invocation',
                                           'target': value.symbol, 'object': value.object_id, 'call': scope.location(facts, node),
                                           'patches': [p.location for p in matches]}])
            return None
        return None

    def analyze(self, source, sink):
        result = super().analyze(source, sink)
        key = (source.get('file_path'), source.get('line_number'), source.get('column_start'),
               source.get('matched_text'), sink.loader_row.get('loader_candidate_id'),
               sink.loader_row.get('file_path'), sink.loader_row.get('line_number'))
        if key in getattr(self, '_refined', {}):
            return self._refined[key]
        if result.state == 'unresolved' and any(e.get('detail') == 'client statement budget' for e in result.evidence):
            # A fresh checker avoids reusing a cached budget failure. The cap
            # changes in this worker process only and is restored immediately.
            with temporary_attribute.object(ValueFlow, 'MAX_STEPS', self.RETRY_STEPS):
                retry = GuardedDecision(self.e, self.builder).analyze(source, sink)
            result = Context(retry.state, [*retry.evidence, {
                'detail': 'one bounded client-analysis retry', 'step_limit': self.RETRY_STEPS}])
        if result.state == 'unresolved':
            object_scope = ObjectPatchScope(self.e, self.builder)
            proof = self.object_call_proof(object_scope, source)
            details = {e.get('detail') for e in result.evidence}
            if proof is None and details & {'default unittest.mock replacement', 'replacement behaviour is not resolved'}:
                checked = object_scope.analyze(source, sink)
                if checked.state == 'mocked':
                    proof = checked
            if proof is None and 'selected client construction or SDK lookup has an active replacement' in details:
                facts = self.repo.files[sink.loader_row['file_path']]
                call = self.e.find_call_at_line(facts, int(sink.loader_row['line_number']), sink.loader_row['visible_call_chain'])
                proof = ProofFlow(object_scope, source, (facts, call)).analyze() if call else None
            # Keep uncertainty unless the additional check proves a mock.
            if proof is not None and proof.state == 'mocked':
                result = Context('mocked', [*proof.evidence, {'detail': 'completed selected-client mock proof'}])
        if not hasattr(self, '_refined'):
            self._refined = {}
        self._refined[key] = result
        self.completed[key] = result
        self.cache[key] = result
        for record in reversed(self.audit):
            if record['source_file'] == source.get('file_path') and record['source_line'] == source.get('line_number') and record['sink'] == sink.loader_row.get('loader_candidate_id'):
                record.update(asdict(result))
                break
        return result


def binding_context(engine, builder, source, sink):
    checker = getattr(builder, '_mock_checker', None)
    if checker is None:
        checker = MockDecision(engine, builder)
        builder._mock_checker = checker
    return checker.analyze(source, sink)
