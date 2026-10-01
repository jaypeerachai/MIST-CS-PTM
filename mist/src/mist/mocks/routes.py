"""Inspect selected FastAPI routes without executing the service.

Only JSON data, plain Pydantic field models, local helpers and data-only streams
are supported. Unknown calls, validators, properties, middleware, dependencies,
redirects, custom response objects, and application mutation prevent proof.
"""

import ast

from mist.mocks.services import Unsupported
from mist.mocks.processes import main_block


DATA = 'json-data'
ITER = ('iter', DATA)


class Routes:
    PURE = {'json.dumps', 'json.loads', 'base64.b64encode', 'base64.b64decode'}
    BUILTINS = {'str', 'int', 'float', 'bool', 'len', 'list', 'tuple', 'dict', 'range', 'isinstance'}
    METHODS = {'get', 'items', 'keys', 'values', 'encode', 'decode'}

    def __init__(self, check, facts, app):
        self.check, self.facts, self.app = check, facts, app
        self.active = set()
        self.globals = {}
        self.schemas = {}
        self.steps = 0
        self.locations = set()

    def tick(self):
        self.steps += 1
        if self.steps > 4000:
            raise Unsupported('route proof budget')

    def external(self, node):
        return self.check.external_symbol(self.facts, node)

    @staticmethod
    def plain(kind):
        return kind == DATA or (isinstance(kind, tuple) and kind[0] in {'iter', 'model'})

    def builtin(self, node, env, allowed):
        return isinstance(node, ast.Name) and node.id not in env and self.check.builtin(self.facts, node, allowed)

    def prove(self, path):
        assignments = [n for n in self.facts.tree.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == self.app for t in n.targets)]
        if len(assignments) != 1 or len(assignments[0].targets) != 1:
            raise Unsupported('ambiguous FastAPI application')
        create = assignments[0].value
        if not isinstance(create, ast.Call) or self.external(create.func) != 'fastapi.FastAPI' or create.args or create.keywords:
            raise Unsupported('application has unknown startup options')
        selected = []
        allowed_app_nodes = {assignments[0].targets[0]}
        for stmt in self.facts.tree.body:
            if stmt is assignments[0] or main_block(stmt) or isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not stmt.decorator_list:
                    continue
                if len(stmt.decorator_list) != 1:
                    raise Unsupported('unknown route decorator')
                dec = stmt.decorator_list[0]
                if (not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute)
                        or not isinstance(dec.func.value, ast.Name) or dec.func.value.id != self.app
                        or dec.func.attr not in {'get', 'post'} or len(dec.args) != 1
                        or not isinstance(dec.args[0], ast.Constant) or not isinstance(dec.args[0].value, str)
                        or any(k.arg != 'response_model_exclude_unset' or not isinstance(k.value, ast.Constant)
                               or type(k.value.value) is not bool for k in dec.keywords)):
                    raise Unsupported('unknown route registration or dependencies')
                allowed_app_nodes.add(dec.func.value)
                if dec.func.attr == 'post' and any(c in dec.args[0].value for c in '{}'):
                    raise Unsupported('parameterized POST route could intercept the selected path')
                if dec.func.attr == 'post' and dec.args[0].value == path:
                    selected.append(stmt)
            elif isinstance(stmt, ast.ClassDef):
                # Every class is created at module import time. Reject dynamic
                # bases, class decorators, executable bodies and custom hooks.
                self.schema(stmt)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                    raise Unsupported('module mutates a service object')
                self.global_value(targets[0].id)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                continue
            else:
                raise Unsupported('unknown service initialization')
        # Outside the one startup block, the application may only be assigned
        # and used as the receiver of the checked route decorators.
        entry = next(n for n in self.facts.tree.body if main_block(n))
        entry_nodes = set(ast.walk(entry))
        for node in self.facts.nodes:
            if isinstance(node, ast.Name) and node.id == self.app and node not in allowed_app_nodes and node not in entry_nodes:
                raise Unsupported('application escapes or is modified')
        if len(selected) != 1:
            raise Unsupported('no unique matching POST route')
        route = selected[0]
        arguments = [self.annotation(p.annotation) for p in [*route.args.posonlyargs, *route.args.args]]
        self.function(route, arguments, {}, route=True)
        if not self.locations:
            raise Unsupported('no prepared response')
        return {'route': self.check.loc(self.facts, route), 'response_locations': sorted(self.locations)}

    def annotation(self, node):
        if isinstance(node, ast.Constant) and node.value is None:
            return DATA
        if node is None:
            raise Unsupported('untyped route input')
        if self.check.builtin(self.facts, node, {'str', 'int', 'float', 'bool'}) or self.external(node) == 'typing.Any':
            return DATA
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            left, right = self.annotation(node.left), self.annotation(node.right)
            return left if left == right else DATA
        if isinstance(node, ast.Subscript):
            if self.check.builtin(self.facts, node.value, {'list', 'tuple', 'dict'}):
                if isinstance(node.slice, ast.Tuple):
                    values = [self.annotation(n) for n in node.slice.elts]
                    if any(not self.plain(v) for v in values):
                        raise Unsupported('unknown schema parameter')
                    return DATA
                item = self.annotation(node.slice)
                return ('iter', item) if isinstance(node.value, ast.Name) and node.value.id in {'list', 'tuple'} else DATA
            if self.external(node.value) == 'typing.Literal':
                if all(isinstance(n, ast.Constant) for n in (node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice])):
                    return DATA
        symbol = self.check.symbol(self.facts, node)
        ref = self.check.definition(symbol)
        if ref and ref.file == self.facts.rel_path and isinstance(ref.node, ast.ClassDef):
            return self.schema(ref.node)
        raise Unsupported('request model is not a plain local data schema')

    def schema(self, cls):
        if cls in self.schemas:
            return self.schemas[cls]
        if (cls in self.active or cls.decorator_list or cls.keywords or len(cls.bases) != 1
                or self.external(cls.bases[0]) != 'pydantic.BaseModel'):
            raise Unsupported('custom request model machinery')
        self.active.add(cls)
        fields = {}
        try:
            for stmt in cls.body:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                    continue
                if isinstance(stmt, ast.Pass):
                    continue
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name) or stmt.target.id.startswith('_'):
                    raise Unsupported('request model has methods, validators or custom state')
                fields[stmt.target.id] = self.annotation(stmt.annotation)
                if stmt.value is not None:
                    self.expr(stmt.value, {})
        finally:
            self.active.remove(cls)
        kind = ('model', tuple(fields.items()))
        self.schemas[cls] = kind
        return kind

    def global_value(self, name):
        if name in self.globals:
            return self.globals[name]
        key = ('global', name)
        if key in self.active:
            raise Unsupported('recursive global data')
        statements = [n for n in self.facts.tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                      and any(isinstance(t, ast.Name) and t.id == name for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        if len(statements) != 1:
            raise Unsupported('unknown response global')
        initializer = statements[0]
        for node in self.facts.nodes:
            if isinstance(node, ast.Global) and name in node.names:
                raise Unsupported('response global may be modified')
            if isinstance(node, ast.Name) and node.id == name and self.check.scope.owner(self.facts, node) is None:
                parent = self.facts.parents.get(node)
                if isinstance(node.ctx, (ast.Store, ast.Del)) and parent is not initializer:
                    raise Unsupported('response global overwritten')
                if isinstance(parent, ast.Attribute) and parent.value is node:
                    raise Unsupported('response global escapes through a method')
        self.active.add(key)
        try:
            value = self.expr(initializer.value, {})
        finally:
            self.active.remove(key)
        if not self.plain(value):
            raise Unsupported('global contains an unknown object')
        self.globals[name] = value
        return value

    def function(self, fn, positional, keywords, route=False):
        self.tick()
        if fn in self.active or len(self.active) > 20 or (fn.decorator_list and not route):
            raise Unsupported('recursive or decorated response helper')
        args = fn.args
        params = [*args.posonlyargs, *args.args]
        if args.vararg or args.kwarg or args.kwonlyargs or len(positional) > len(params):
            raise Unsupported('unknown helper arguments')
        env = dict(zip([p.arg for p in params], positional))
        default_start = len(params) - len(args.defaults)
        for index, param in enumerate(params):
            if param.arg in keywords:
                if param.arg in env:
                    raise Unsupported('duplicate helper argument')
                env[param.arg] = keywords[param.arg]
            elif param.arg not in env:
                if index < default_start:
                    raise Unsupported('missing helper argument')
                env[param.arg] = self.expr(args.defaults[index - default_start], {})
        if set(keywords) - {p.arg for p in params}:
            raise Unsupported('extra helper argument')
        self.active.add(fn)
        results = []
        try:
            self.block(fn.body, env, results)
        finally:
            self.active.remove(fn)
        if not results:
            raise Unsupported('helper has no local response')
        if any(n[0] == 'yield' for n in results):
            return ITER
        values = [v for _, v in results]
        return values[0] if len(set(values)) == 1 else DATA

    def expr(self, node, env):
        self.tick()
        if node is None or isinstance(node, ast.Constant):
            return DATA
        if isinstance(node, ast.Name):
            return env[node.id] if node.id in env else self.global_value(node.id)
        if isinstance(node, ast.Attribute):
            kind = self.expr(node.value, env)
            if isinstance(kind, tuple) and kind[0] == 'model' and node.attr in dict(kind[1]):
                return dict(kind[1])[node.attr]
            raise Unsupported('unknown response attribute')
        if isinstance(node, ast.Subscript):
            base = self.expr(node.value, env)
            self.expr(node.slice, env)
            return base[1] if isinstance(base, tuple) and base[0] == 'iter' else DATA
        if isinstance(node, ast.NamedExpr):
            kind = self.expr(node.value, env)
            self.assign(node.target, kind, env)
            return kind
        if isinstance(node, (ast.Yield, ast.Await)):
            return self.expr(node.value, env)
        if isinstance(node, ast.Call):
            if any(k.arg is None for k in node.keywords) or any(isinstance(a, ast.Starred) for a in node.args):
                raise Unsupported('expanded response arguments')
            symbol = self.external(node.func)
            if self.builtin(node.func, env, {'isinstance'}):
                if len(node.args) != 2 or node.keywords or not self.check.builtin(self.facts, node.args[1], {'str', 'int', 'float', 'bool', 'dict', 'list'}):
                    raise Unsupported('unknown runtime type test')
                self.expr(node.args[0], env)
                return DATA
            arguments = [self.expr(a, env) for a in node.args]
            keywords = {k.arg: self.expr(k.value, env) for k in node.keywords}
            if not all(self.plain(v) for v in [*arguments, *keywords.values()]):
                raise Unsupported('non-data response argument')
            if symbol in self.PURE or self.builtin(node.func, env, self.BUILTINS):
                return ITER if isinstance(node.func, ast.Name) and node.func.id in {'list', 'tuple', 'range'} else DATA
            if symbol in {'starlette.responses.StreamingResponse', 'fastapi.responses.StreamingResponse'}:
                if (len(arguments) != 1 or set(keywords) - {'media_type'}
                        or any(k.arg == 'media_type' and (not isinstance(k.value, ast.Constant) or not isinstance(k.value.value, str)) for k in node.keywords)):
                    raise Unsupported('stream has custom status, headers or callbacks')
                return DATA
            if symbol == 'fastapi.HTTPException':
                kw = {k.arg: k.value for k in node.keywords}
                if (node.args or set(kw) - {'status_code', 'detail'} or not isinstance(kw.get('status_code'), ast.Constant)
                        or type(kw['status_code'].value) is not int or not 400 <= kw['status_code'].value < 600):
                    raise Unsupported('redirect or custom exception response')
                return DATA
            ref = self.check.definition(self.check.symbol(self.facts, node.func))
            if ref and ref.file == self.facts.rel_path and isinstance(ref.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(node.func, ast.Name) and node.func.id in env:
                    raise Unsupported('response helper is shadowed')
                return self.function(ref.node, arguments, keywords)
            if isinstance(node.func, ast.Attribute):
                kind = self.expr(node.func.value, env)
                if kind == DATA and node.func.attr in self.METHODS:
                    return DATA
                if isinstance(kind, tuple) and kind[0] == 'model' and node.func.attr == 'model_dump':
                    return DATA
            raise Unsupported('response handler calls unknown or external code')
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            inner = dict(env)
            for gen in node.generators:
                value = self.expr(gen.iter, inner)
                if gen.is_async and not (isinstance(value, tuple) and value[0] == 'iter'):
                    raise Unsupported('unknown async iterator')
                item = value[1] if isinstance(value, tuple) and value[0] == 'iter' else DATA
                self.assign(gen.target, item, inner)
                for condition in gen.ifs:
                    self.expr(condition, inner)
            if isinstance(node, ast.DictComp):
                self.expr(node.key, inner)
                self.expr(node.value, inner)
                return DATA
            return ('iter', self.expr(node.elt, inner))
        if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Slice, ast.BinOp,
                             ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.JoinedStr, ast.FormattedValue)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr) and not self.plain(self.expr(child, env)):
                    raise Unsupported('operation on a non-data value')
            return DATA
        raise Unsupported('unsupported response expression')

    @staticmethod
    def assign(target, kind, env):
        if not isinstance(target, ast.Name):
            raise Unsupported('response code mutates an object')
        env[target.id] = kind

    def block(self, body, env, results):
        for stmt in body:
            self.tick()
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                kind = self.expr(stmt.value, env)
                for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                    self.assign(target, kind, env)
            elif isinstance(stmt, ast.If):
                self.expr(stmt.test, env)
                left, right = dict(env), dict(env)
                self.block(stmt.body, left, results)
                self.block(stmt.orelse, right, results)
                common = {k: left[k] if left[k] == right[k] else DATA for k in left.keys() & right.keys()}
                env.clear()
                env.update(common)
            elif isinstance(stmt, ast.For):
                kind = self.expr(stmt.iter, env)
                inner = dict(env)
                self.assign(stmt.target, kind[1] if isinstance(kind, tuple) and kind[0] == 'iter' else DATA, inner)
                self.block(stmt.body, inner, results)
                self.block(stmt.orelse, dict(env), results)
            elif isinstance(stmt, ast.Return) or (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield)):
                value = stmt.value if isinstance(stmt, ast.Return) else stmt.value.value
                kind = self.expr(value, env)
                if not self.plain(kind):
                    raise Unsupported('response is not local data')
                results.append(('return' if isinstance(stmt, ast.Return) else 'yield', kind))
                self.locations.add(self.check.loc(self.facts, stmt))
            elif isinstance(stmt, ast.Expr):
                self.expr(stmt.value, env)
            elif isinstance(stmt, ast.Raise):
                self.expr(stmt.exc, env)
                if stmt.cause:
                    self.expr(stmt.cause, env)
            elif isinstance(stmt, ast.Pass):
                continue
            else:
                raise Unsupported('unsupported response statement')
