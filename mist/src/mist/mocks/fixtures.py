"""Explicit fixture-to-client-to-service evidence, not name-based exclusion."""

import ast
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath

from mist.mocks.scope import Context
from mist.mocks.services import (
    Ref, ServiceCheck, Unsupported,
)
from mist.mocks.processes import Endpoint, ProcessCheck
from mist.mocks.routes import Routes
from mist.rules.library_rules import LIBRARY_RULES


@dataclass(frozen=True)
class Client:
    origin: str
    endpoint: Endpoint
    location: str


@dataclass(frozen=True)
class BuiltinFixture:
    name: str


class FixtureServiceCheck(ServiceCheck):
    def __init__(self, scope):
        super().__init__(scope)
        self.process = ProcessCheck(self)
        self.route_cache = {}
        self.fixture_cache = {}
        self.active_fixtures = set()
        self.last_reason = ''
        self._external_cache = {}
        self._visible_cache = {}
        self._fixture_meta_cache = {}
        self._fixture_lookup_cache = {}
        self._framework_cache = {}

    def external_symbol(self, facts, node):
        """Do not confuse the graph's suffix aliases with Python import roots.

        A real root/sibling module still prevents trusting an external API.
        Builtin modules have priority over unrelated suffix aliases. For other
        imports, even an ambiguous source-layout alias prevents this proof.
        """
        key = (facts.rel_path, id(node))
        if key not in self._external_cache:
            self._external_cache[key] = self._external_symbol(facts, node)
        return self._external_cache[key]

    def _external_symbol(self, facts, node):
        symbol = self.symbol(facts, node)
        parts = symbol.split('.')
        for cut in range(1, len(parts)):
            relative = PurePosixPath(*parts[:cut])
            for directory in (PurePosixPath('.'), PurePosixPath(facts.rel_path).parent):
                path = directory / relative
                if str(path.with_suffix('.py')) in self.repo.files or str(path / '__init__.py') in self.repo.files:
                    return ''
            if parts[0] not in sys.builtin_module_names and '.'.join(parts[:cut]) in self.repo.module_index:
                return ''
        return symbol

    def visible(self, facts):
        if facts.rel_path in self._visible_cache:
            return self._visible_cache[facts.rel_path]
        directory = PurePosixPath(facts.rel_path).parent
        result = [f for f in self.repo.files.values() if f.tree and (f is facts or (
            PurePosixPath(f.rel_path).name == 'conftest.py' and
            (PurePosixPath(f.rel_path).parent == directory or PurePosixPath(f.rel_path).parent in directory.parents)))]
        self._visible_cache[facts.rel_path] = result
        return result

    def fixture_meta(self, facts, fn, validate=False):
        key = (facts.rel_path, id(fn), validate)
        if key not in self._fixture_meta_cache:
            try:
                self._fixture_meta_cache[key] = self._fixture_meta(facts, fn, validate)
            except Unsupported as error:
                self._fixture_meta_cache[key] = str(error)
        result = self._fixture_meta_cache[key]
        if isinstance(result, str):
            raise Unsupported(result)
        return result

    def _fixture_meta(self, facts, fn, validate):
        found = []
        for dec in fn.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if self.external_symbol(facts, target) == 'pytest.fixture':
                kw = {k.arg: k.value for k in dec.keywords} if isinstance(dec, ast.Call) else {}
                if validate and isinstance(dec, ast.Call) and (dec.args or set(kw) - {'name', 'scope', 'params', 'ids', 'autouse'}):
                    raise Unsupported('unsupported fixture options')
                for option, value in kw.items():
                    if not validate and option != 'name':
                        continue
                    try:
                        ast.literal_eval(value)
                    except (ValueError, TypeError):
                        raise Unsupported('dynamic fixture options') from None
                name = ast.literal_eval(kw['name']) if 'name' in kw else fn.name
                if not isinstance(name, str):
                    raise Unsupported('dynamic fixture name')
                found.append((name, kw))
        if validate and found and (len(found) != 1 or len(fn.decorator_list) != 1):
            raise Unsupported('fixture has another decorator')
        return found[0] if found else None

    def find_fixture(self, test_facts, name):
        key = (test_facts.rel_path, name)
        if key not in self._fixture_lookup_cache:
            try:
                self._fixture_lookup_cache[key] = self._find_fixture(test_facts, name)
            except Unsupported as error:
                self._fixture_lookup_cache[key] = str(error)
        result = self._fixture_lookup_cache[key]
        if isinstance(result, str):
            raise Unsupported(result)
        return result

    def _find_fixture(self, test_facts, name):
        candidates = []
        for facts in self.visible(test_facts):
            for node in facts.tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                meta = self.fixture_meta(facts, node)
                if meta and meta[0] == name:
                    rank = (facts is test_facts, len(PurePosixPath(facts.rel_path).parts))
                    candidates.append((rank, facts, node))
        if not candidates:
            return None
        candidates.sort(key=lambda row: row[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            raise Unsupported('ambiguous fixture definition')
        _, facts, fn = candidates[0]
        definitions = [n for n in facts.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == fn.name]
        if len(definitions) != 1 or any(isinstance(n, ast.Name) and n.id == fn.name
                and isinstance(n.ctx, (ast.Store, ast.Del)) and self.scope.owner(facts, n) is None for n in facts.nodes):
            raise Unsupported('fixture definition overwritten')
        return facts, fn

    def parameter_override(self, test_facts, test, name):
        nodes = [*test.decorator_list]
        parent = test_facts.parents.get(test)
        if isinstance(parent, ast.ClassDef):
            nodes += parent.decorator_list
            nodes += [n.value for n in parent.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'pytestmark' for t in n.targets)]
        nodes += [n.value for n in test_facts.tree.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == 'pytestmark' for t in n.targets)]
        for item in nodes:
            for node in ast.walk(item):
                if isinstance(node, ast.Call) and self.external_symbol(test_facts, node.func) == 'pytest.mark.parametrize':
                    if not node.args:
                        return True
                    try:
                        names = ast.literal_eval(node.args[0])
                    except (ValueError, TypeError):
                        return True
                    if isinstance(names, str):
                        names = [s.strip() for s in names.split(',')]
                    if not isinstance(names, (list, tuple)) or name in names:
                        return True
        return False

    def fixture(self, test_facts, test, name):
        if self.parameter_override(test_facts, test, name):
            raise Unsupported('test parametrization can replace fixture value')
        key = (test_facts.rel_path, id(test), name)
        if key in self.fixture_cache:
            value = self.fixture_cache[key]
            if isinstance(value, str):
                raise Unsupported(value)
            return value
        if key in self.active_fixtures or len(self.active_fixtures) > 8:
            raise Unsupported('recursive fixture dependency')
        found = self.find_fixture(test_facts, name)
        if found is None:
            if name in {'request', 'monkeypatch'}:
                return (BuiltinFixture(name),)
            raise Unsupported('fixture value not resolved')
        facts, fn = found
        self.fixture_meta(facts, fn, validate=True)
        if not isinstance(fn, ast.FunctionDef):
            raise Unsupported('async fixture lifecycle not established')
        if sum(isinstance(n, ast.Yield) and self.scope.owner(facts, n) is fn for n in ast.walk(fn)) > 1:
            raise Unsupported('fixture yields more than once')
        if isinstance(test_facts.parents.get(test), ast.ClassDef):
            raise Unsupported('class fixture overrides not supported')
        self.active_fixtures.add(key)
        values = []
        try:
            self.fixture_block(facts, fn, fn.body, {}, (), test_facts, test, values)
            if not values:
                raise Unsupported('fixture has no established return or active yield')
            result = tuple(values)
        except Unsupported as error:
            self.fixture_cache[key] = str(error)
            raise
        finally:
            self.active_fixtures.remove(key)
        self.fixture_cache[key] = result
        return result

    def fixture_expr(self, facts, fn, node, env, test_facts, test):
        if isinstance(node, ast.Constant):
            return (node.value,)
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            if node.id in self.e.function_arg_names(fn):
                if fn is test and facts is test_facts and any(
                        isinstance(n, ast.Name) and n.id == node.id and isinstance(n.ctx, (ast.Store, ast.Del))
                        and self.scope.owner(facts, n) is fn and n.lineno <= node.lineno for n in facts.nodes):
                    raise Unsupported('fixture parameter changed before client construction')
                return self.fixture(test_facts, test, node.id)
            raise Unsupported('unknown fixture value')
        if isinstance(node, ast.Attribute):
            base = self.fixture_expr(facts, fn, node.value, env, test_facts, test)
            if base == (BuiltinFixture('request'),) and node.attr == 'param':
                meta = self.fixture_meta(facts, fn)
                if meta and 'params' in meta[1]:
                    values = ast.literal_eval(meta[1]['params'])
                    if isinstance(values, (list, tuple)) and values and all(type(v) in (bool, int, str, type(None)) for v in values):
                        return tuple(values)
            raise Unsupported('unknown fixture attribute')
        if isinstance(node, ast.Call):
            symbol = self.external_symbol(facts, node.func)
            endpoint_argument = LIBRARY_RULES.fixture_clients.get(symbol)
            if endpoint_argument:
                keywords = {k.arg: k.value for k in node.keywords}
                allowed_options = LIBRARY_RULES.service_clients[symbol]['allowed_options']
                if node.args or set(keywords) - allowed_options or endpoint_argument not in keywords:
                    raise Unsupported('client requires an explicit URL and known options')
                endpoints = self.fixture_expr(facts, fn, keywords[endpoint_argument], env, test_facts, test)
                for name, value in keywords.items():
                    if name != endpoint_argument and not isinstance(value, ast.Constant):
                        raise Unsupported('dynamic client options')
                if not endpoints or any(not isinstance(ep, Endpoint) for ep in endpoints):
                    raise Unsupported('client endpoint is not an active service fixture')
                return tuple(Client(symbol, ep, self.loc(facts, node)) for ep in endpoints)
        raise Unsupported('unsupported fixture expression')

    def fixture_block(self, facts, fn, body, env, owned, test_facts, test, values):
        for stmt in body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                env[stmt.targets[0].id] = self.fixture_expr(facts, fn, stmt.value, env, test_facts, test)
            elif isinstance(stmt, ast.With) and len(stmt.items) == 1:
                item = stmt.items[0]
                call = item.context_expr
                if not isinstance(call, ast.Call) or call.args or call.keywords or not isinstance(item.optional_vars, ast.Name):
                    raise Unsupported('unknown fixture context manager')
                ref = self.definition(self.symbol(facts, call.func))
                if not ref:
                    raise Unsupported('fixture context manager not in repository')
                endpoint = self.process.launch(ref)
                inner = dict(env)
                inner[item.optional_vars.id] = (endpoint,)
                self.fixture_block(facts, fn, stmt.body, inner, (*owned, endpoint), test_facts, test, values)
                # An address returned after leaving this block is no longer live.
                for name, candidates in inner.items():
                    if any(value == endpoint or isinstance(value, Client) and value.endpoint == endpoint for value in candidates):
                        env.pop(name, None)
            elif isinstance(stmt, ast.If):
                self.fixture_expr(facts, fn, stmt.test, env, test_facts, test)
                if not stmt.orelse:
                    raise Unsupported('fixture branch has no established alternative')
                before = len(values)
                self.fixture_block(facts, fn, stmt.body, dict(env), owned, test_facts, test, values)
                middle = len(values)
                self.fixture_block(facts, fn, stmt.orelse, dict(env), owned, test_facts, test, values)
                if middle == before or len(values) == middle:
                    raise Unsupported('not all fixture branches return a value')
                return
            elif isinstance(stmt, ast.Return) or isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield):
                yielded = isinstance(stmt, ast.Expr)
                expr = stmt.value.value if yielded else stmt.value
                outputs = self.fixture_expr(facts, fn, expr, env, test_facts, test)
                for value in outputs:
                    ep = value.endpoint if isinstance(value, Client) else value
                    if ep in owned and not yielded:
                        raise Unsupported('fixture returns after closing its service context')
                    values.append(value)
                if not yielded:
                    return
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if not isinstance(call.func, ast.Attribute) or call.func.attr != 'setenv' or len(call.args) != 2 or call.keywords:
                    raise Unsupported('fixture calls unknown setup code')
                receiver = self.fixture_expr(facts, fn, call.func.value, env, test_facts, test)
                if receiver != (BuiltinFixture('monkeypatch'),) or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
                    raise Unsupported('environment setter not established')
                if call.args[0].value not in LIBRARY_RULES.fixture_environment:
                    raise Unsupported('environment change could affect process or transport setup')
                # Environment values are not used to establish client routing.
                self.fixture_expr(facts, fn, call.args[1], env, test_facts, test)
            elif isinstance(stmt, ast.Pass) or isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            else:
                raise Unsupported('fixture mutates a client or has unknown setup')

    def client(self, facts, test, call):
        root = call.func
        names = []
        while isinstance(root, ast.Attribute):
            names.insert(0, root.attr)
            root = root.value
        route = LIBRARY_RULES.fixture_route('.'.join(names))
        if route is None or not isinstance(root, ast.Name):
            raise Unsupported('unsupported client method route')
        preceding = [s for s in self.scope.preceding(facts, call) if self.scope.owner(facts, s) is test]
        assignments = [s for s in preceding if isinstance(s, ast.Assign) and len(s.targets) == 1
                       and isinstance(s.targets[0], ast.Name) and s.targets[0].id == root.id]
        if root.id in self.e.function_arg_names(test):
            if assignments:
                raise Unsupported('fixture client reassigned')
            clients = self.fixture(facts, test, root.id)
            start = (test.lineno, test.col_offset)
        else:
            if len(assignments) != 1:
                raise Unsupported('client construction not unique')
            assignment = assignments[0]
            clients = self.fixture_expr(facts, test, assignment.value, {}, facts, test)
            start = (getattr(assignment, 'end_lineno', assignment.lineno), getattr(assignment, 'end_col_offset', assignment.col_offset))
        if not clients or any(not isinstance(c, Client) for c in clients):
            raise Unsupported('fixture does not return an established client')
        # Unknown aliases, writes and earlier uses could change routing. A
        # separate client or a constructor with the same name is not enough.
        end = (getattr(call, 'end_lineno', call.lineno), getattr(call, 'end_col_offset', call.col_offset))
        for n in facts.nodes:
            if (isinstance(n, ast.Name) and n.id == root.id and self.scope.owner(facts, n) is test
                    and start < (n.lineno, n.col_offset) <= end and n is not root):
                raise Unsupported('client is used or changed before this call')
        return clients, route

    def other_fixture_consumers(self, facts, test):
        """An uninspected dependent fixture may mutate an established client."""
        used = {name for path, owner, name in self.fixture_cache if path == facts.rel_path and owner == id(test)}
        pending = set(self.e.function_arg_names(test))
        for candidate in self.visible(facts):
            for fn in candidate.tree.body:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    meta = self.fixture_meta(candidate, fn)
                    if meta and 'autouse' in meta[1]:
                        option = meta[1]['autouse']
                        if not isinstance(option, ast.Constant) or option.value is not False:
                            pending.add(meta[0])
        seen = set()
        while pending - seen:
            name = next(iter(pending - seen))
            seen.add(name)
            found = self.find_fixture(facts, name)
            if found:
                _, fn = found
                deps = set(self.e.function_arg_names(fn))
                if name not in used and deps & (used - {'request', 'monkeypatch'}):
                    raise Unsupported('another active fixture can modify the supplied value')
                pending.update(deps)

    def framework_mutation(self, facts):
        key = (facts.rel_path, tuple(sorted(self.process.protected)))
        if key not in self._framework_cache:
            try:
                self._framework_mutation(facts)
                self._framework_cache[key] = ''
            except Unsupported as error:
                self._framework_cache[key] = str(error)
        if self._framework_cache[key]:
            raise Unsupported(self._framework_cache[key])

    def _framework_mutation(self, facts):
        protected = LIBRARY_RULES.protected_fixture_symbols | set(self.process.protected)
        for node in facts.nodes:
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                symbol = self.symbol(facts, node)
                if any(symbol == p or symbol.startswith(p + '.') for p in protected):
                    raise Unsupported('visible framework replacement')
            if isinstance(node, ast.Call):
                symbol = self.symbol(facts, node.func)
                if symbol in {'sys.path.insert', 'sys.path.append', 'sys.path.extend'}:
                    raise Unsupported('dynamic import search path')
                # Calls that receive the module itself, rather than a normal
                # API result, may alter its methods. Do not trust their effects.
                if symbol.startswith(('unittest.mock.patch', 'pytest.MonkeyPatch')) or (
                        isinstance(node.func, ast.Name) and node.func.id in {'setattr', 'delattr'}):
                    for arg in node.args:
                        target = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else self.symbol(facts, arg)
                        if target and any(target == p or target.startswith(p + '.') or p.startswith(target + '.') for p in protected):
                            raise Unsupported('visible framework patch')

    def analyze(self, source, facts, call):
        self.last_reason = ''
        test = self.scope.owner(facts, call)
        if (test is None or not test.name.startswith('test_') or source['file_path'] != facts.rel_path
                or not (test.lineno <= int(source['line_number']) <= getattr(test, 'end_lineno', test.lineno))):
            return None
        filename = PurePosixPath(facts.rel_path).name
        if not (filename.startswith('test_') or filename.endswith('_test.py')):
            return None
        try:
            clients, route = self.client(facts, test, call)
            self.other_fixture_consumers(facts, test)
            # Check modules supplying the proof, not unrelated repository files.
            related = {facts.rel_path, *(f.rel_path for f in self.visible(facts))}
            related.update(c.endpoint.script for c in clients)
            related.update(c.endpoint.launch.rsplit(':', 1)[0] for c in clients)
            for path in related:
                self.framework_mutation(self.repo.files[path])
            evidence = []
            for client in clients:
                ep = client.endpoint
                route_path = ep.base_path + route
                cache_key = (ep, route_path)
                if cache_key not in self.route_cache:
                    server_facts, app = self.process.application(ep)
                    self.route_cache[cache_key] = Routes(self, server_facts, app).prove(route_path)
                proof = self.route_cache[cache_key]
                evidence.append({'detail': 'explicit fixture endpoint reaches a Python service with a prepared-response route',
                                 'client': client.origin, 'constructor': client.location,
                                 'process_launch': ep.launch, 'service_url_yield': ep.yielded,
                                 'service_script': ep.script, 'route_path': route_path, **proof})
            return Context('mocked', evidence)
        except Unsupported as error:
            self.last_reason = str(error)
            return None
