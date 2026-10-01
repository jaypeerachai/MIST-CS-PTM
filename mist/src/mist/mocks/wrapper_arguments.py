"""Check forwarding and receiver preservation separately for visible wrappers.

Literal metadata reads from ordinary instance fields are supported.
It does not treat decorator names, functools.wraps, or arbitrary field reads as
proof. Passing a client/receiver to another callable still fails the check.
"""

import ast

from mist.mocks.client_values import ProjectCallableFlow
from mist.mocks.callables import Value, UNKNOWN, FUNCTION_KIND_CHECKS, function_kind_test


class ForwardingFlow(ProjectCallableFlow):
    def call(self, node, facts, scope, state, depth):
        function = self.reference(node.func, facts, scope, state)
        if function.name in FUNCTION_KIND_CHECKS and len(node.args) == 1:
            value = self.reference(node.args[0], facts, scope, state)
            if value.kind == "original" and value.node is not None:
                answer = function_kind_test(function.name, value.node, value.facts, self.c.owner)
                return [(state, Value("data", data=answer))]
        return super().call(node, facts, scope, state, depth)

    def statement(self, stmt, facts, scope, state, depth):
        if isinstance(stmt, ast.Raise):
            # A raising branch does not invoke a different receiver. Preserve
            # calls in its exception expression, then stop that branch.
            output = self.evaluate(stmt.exc, facts, scope, state, depth)
            if stmt.cause is not None:
                output = [(changed, value) for before, _ in output
                          for changed, value in self.evaluate(stmt.cause, facts, scope, before, depth)]
            for current, _ in output:
                current.returned, current.result = True, UNKNOWN
                current.effects.add('abort')
            return [current for current, _ in output]
        return super().statement(stmt, facts, scope, state, depth)


DATA = frozenset({'data'})
OTHER = frozenset({'other'})
OBJECT = frozenset({'object'})
BORROWED = frozenset({'borrowed'})
PROTECTED = {'object', 'borrowed', 'args', 'kwargs', 'class', 'wrapped'}


class UnsafeWrapper(Exception):
    pass


class ArgumentSafety:
    """Conservative alias/effect check for a *args, **kwargs wrapper body."""

    def __init__(self, function, flow, state, fields):
        self.function, self.flow, self.state = function, flow, state
        self.fields = fields
        self.node, self.facts, self.scope = function.node, function.facts, function.scope
        self.args = self.node.args.vararg.arg if self.node.args.vararg else ''
        self.kwargs = self.node.args.kwarg.arg if self.node.args.kwarg else ''
        self.forwarded = False

    @staticmethod
    def protected(value):
        return bool(value & PROTECTED)

    def attr(self, value, name):
        result = set()
        for kind in value:
            if kind == 'object':
                if name != '__class__' and name not in self.fields:
                    raise UnsafeWrapper('field read is not known to be plain literal metadata')
                result.add('class' if name == '__class__' else 'data')
            elif kind == 'class':
                result.add('data' if name in {'__name__', '__qualname__', '__module__'} else 'borrowed')
            elif kind == 'wrapped':
                result.add('data' if name in {'__name__', '__qualname__', '__module__', '__doc__'} else 'borrowed')
            elif kind in PROTECTED:
                result.add('borrowed')
            else:
                result.add(kind)
        return frozenset(result)

    def expression(self, node, env):
        if node is None or isinstance(node, ast.Constant):
            return DATA
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            value = self.flow.reference(node, self.facts, self.scope, self.state)
            return frozenset({'wrapped'}) if value.kind == 'original' else DATA if value.kind == 'data' else OTHER
        if isinstance(node, ast.Attribute):
            return self.attr(self.expression(node.value, env), node.attr)
        if isinstance(node, ast.Subscript):
            owner = self.expression(node.value, env)
            index = self.expression(node.slice, env)
            if self.protected(index):
                raise UnsafeWrapper('unknown index')
            if owner == frozenset({'args'}) and isinstance(node.slice, ast.Constant) and node.slice.value == 0:
                return OBJECT
            return BORROWED if self.protected(owner) else owner
        if isinstance(node, ast.IfExp):
            self.expression(node.test, env)
            return self.expression(node.body, env) | self.expression(node.orelse, env)
        if isinstance(node, ast.BoolOp):
            return frozenset().union(*(self.expression(n, env) for n in node.values))
        if isinstance(node, ast.Await):
            return self.expression(node.value, env)
        if isinstance(node, ast.Call):
            function = self.expression(node.func, env)
            arguments = [self.expression(n.value if isinstance(n, ast.Starred) else n, env) for n in node.args]
            keywords = [self.expression(k.value, env) for k in node.keywords]
            if function == frozenset({'wrapped'}):
                identity_forwarding = (len(node.args) == 1 and isinstance(node.args[0], ast.Starred)
                    and isinstance(node.args[0].value, ast.Name) and node.args[0].value.id == self.args
                    and len(node.keywords) == 1 and node.keywords[0].arg is None
                    and isinstance(node.keywords[0].value, ast.Name) and node.keywords[0].value.id == self.kwargs
                    and env.get(self.args) == frozenset({'args'}) and env.get(self.kwargs) == frozenset({'kwargs'}))
                if not identity_forwarding:
                    raise UnsafeWrapper('changed forwarding arguments')
                self.forwarded = True
                return OTHER
            symbol = self.flow.reference(node.func, self.facts, self.scope, self.state).name
            locally_rebound = isinstance(node.func, ast.Name) and node.func.id in env
            if symbol == 'builtins.getattr' and not locally_rebound and len(arguments) in {2, 3}:
                if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    if len(arguments) == 3 and self.protected(arguments[2]):
                        raise UnsafeWrapper('borrowed getattr default')
                    return self.attr(arguments[0], node.args[1].value)
            if self.protected(function) or any(self.protected(v) for v in [*arguments, *keywords]):
                raise UnsafeWrapper('receiver, argument, or wrapped callable escapes')
            return OTHER
        if isinstance(node, (ast.Lambda, ast.NamedExpr, ast.Yield, ast.YieldFrom,
                             ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            raise UnsafeWrapper('unsupported nested evaluation')
        children = [self.expression(n, env) for n in ast.iter_child_nodes(node) if isinstance(n, ast.expr)]
        value = frozenset().union(*children) if children else DATA
        if self.protected(value):
            # Do not use formatting, arithmetic, or containers to hide an alias
            # or an unknown magic method call on the borrowed object.
            raise UnsafeWrapper('unsupported operation on a borrowed value')
        return value

    @staticmethod
    def merge(env, branches):
        for name in set(env).union(*(set(branch) for branch in branches)):
            env[name] = frozenset().union(*(branch.get(name, env.get(name, OTHER)) for branch in branches))

    def block(self, statements, env):
        for node in statements:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.expression(node.value, env)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if not isinstance(target, ast.Name):
                        raise UnsafeWrapper('non-local write')
                    env[target.id] = value
            elif isinstance(node, ast.Expr):
                self.expression(node.value, env)
            elif isinstance(node, ast.Return):
                self.expression(node.value, env)
                break
            elif isinstance(node, ast.Raise):
                self.expression(node.exc, env)
                self.expression(node.cause, env)
                break
            elif isinstance(node, ast.If):
                self.expression(node.test, env)
                branches = [dict(env), dict(env)]
                self.block(node.body, branches[0])
                self.block(node.orelse, branches[1])
                self.merge(env, branches)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    self.expression(item.context_expr, env)
                    if item.optional_vars:
                        if not isinstance(item.optional_vars, ast.Name):
                            raise UnsafeWrapper('unsupported context target')
                        env[item.optional_vars.id] = OTHER
                self.block(node.body, env)
            elif isinstance(node, ast.Try):
                original = dict(env)
                self.block(node.body, env)
                self.block(node.orelse, env)
                branches = [dict(env)]
                for handler in node.handlers:
                    local = dict(original)
                    self.merge(local, [env, original])
                    if handler.name:
                        local[handler.name] = OTHER
                    self.block(handler.body, local)
                    branches.append(local)
                self.merge(env, branches)
                self.block(node.finalbody, env)
            elif isinstance(node, ast.Pass):
                continue
            else:
                raise UnsafeWrapper('unsupported wrapper statement')

    def check(self):
        if (not self.args or not self.kwargs or self.node.args.posonlyargs
                or self.node.args.args or self.node.args.kwonlyargs):
            return False
        env = {self.args: frozenset({'args'}), self.kwargs: frozenset({'kwargs'})}
        try:
            self.block(self.node.body, env)
            return self.forwarded
        except UnsafeWrapper:
            return False
