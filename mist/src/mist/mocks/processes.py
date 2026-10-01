"""Static evidence for a fixture-owned Python service process.

No repository code is imported or executed. Values must follow an explicit URL,
subprocess argument list, argparse options, and Uvicorn application. Unknown
process setup, endpoint rewriting, and application hooks prevent this proof.
"""

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from mist.mocks.services import (
    LOOPBACK, Ref, Unsupported,
)


@dataclass(frozen=True)
class Port:
    location: str


@dataclass(frozen=True)
class Text:
    parts: tuple


@dataclass(frozen=True)
class Endpoint:
    script: str
    host: str
    port: object
    base_path: str
    launch: str
    yielded: str
    options: tuple


def main_block(stmt):
    return (isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare)
            and isinstance(stmt.test.left, ast.Name) and stmt.test.left.id == '__name__'
            and len(stmt.test.ops) == 1 and isinstance(stmt.test.ops[0], ast.Eq)
            and len(stmt.test.comparators) == 1
            and isinstance(stmt.test.comparators[0], ast.Constant)
            and stmt.test.comparators[0].value == '__main__' and not stmt.orelse)


class ProcessCheck:
    def __init__(self, check):
        self.check = check
        self.scope = check.scope
        self.repo = check.repo
        self.cache = {}
        self.protected = set()

    def definition_intact(self, ref):
        facts, fn = self.repo.files[ref.file], ref.node
        if not isinstance(fn, ast.FunctionDef) or fn not in facts.tree.body:
            return False
        self.protected.add(facts.module + '.' + fn.name)
        definitions = [n for n in facts.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == fn.name]
        return len(definitions) == 1 and not any(
            isinstance(n, ast.Name) and n.id == fn.name and isinstance(n.ctx, (ast.Store, ast.Del))
            and self.scope.owner(facts, n) is None for n in facts.nodes)

    def symbol(self, facts, node):
        return self.check.external_symbol(facts, node)

    def resolve(self, facts, node, env):
        if isinstance(node, ast.Constant) and type(node.value) in (str, int):
            return node.value
        if isinstance(node, ast.Name) and node.id in env:
            return env[node.id]
        if isinstance(node, ast.JoinedStr):
            parts = []
            for child in node.values:
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    parts.append(child.value)
                elif isinstance(child, ast.FormattedValue) and child.conversion == -1 and child.format_spec is None:
                    value = self.resolve(facts, child.value, env)
                    if not isinstance(value, (str, int, Port, Text)):
                        raise Unsupported('URL part is not a scalar')
                    if isinstance(value, Text):
                        parts.extend(value.parts)
                    else:
                        parts.append(value)
                else:
                    raise Unsupported('unsupported URL formatting')
            return Text(tuple(parts))
        if isinstance(node, ast.Call) and self.check.builtin(facts, node.func, {'str'}) and len(node.args) == 1 and not node.keywords:
            value = self.resolve(facts, node.args[0], env)
            if isinstance(value, (str, int, Port)):
                return value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = self.resolve(facts, node.left, env), self.resolve(facts, node.right, env)
            if isinstance(left, PurePosixPath) and isinstance(right, str):
                return left / right
        if isinstance(node, ast.Attribute) and node.attr == 'parent':
            value = self.resolve(facts, node.value, env)
            if isinstance(value, PurePosixPath):
                return value.parent
        if isinstance(node, ast.Call) and self.symbol(facts, node.func) == 'pathlib.Path':
            if len(node.args) == 1 and not node.keywords and isinstance(node.args[0], ast.Name) and node.args[0].id == '__file__':
                if any(isinstance(n, ast.Name) and n.id == '__file__' and isinstance(n.ctx, (ast.Store, ast.Del)) for n in facts.nodes):
                    raise Unsupported('module path replaced')
                return PurePosixPath(facts.rel_path)
        if isinstance(node, ast.Call) and not node.args and not node.keywords:
            ref = self.check.definition(self.check.symbol(facts, node.func))
            if ref and self.port_helper(ref):
                return Port(self.check.loc(facts, node))
        raise Unsupported('process value not established')

    def port_helper(self, ref):
        """Only a standard socket bound to an ephemeral port, then closed."""
        facts, fn = self.repo.files[ref.file], ref.node
        if not self.definition_intact(ref) or fn.decorator_list or self.check.e.function_arg_names(fn):
            return False
        body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]
        if len(body) != 5:
            return False
        create, bind, capture, close, ret = body
        if not (isinstance(create, ast.Assign) and len(create.targets) == 1 and isinstance(create.targets[0], ast.Name)
                and isinstance(create.value, ast.Call) and self.symbol(facts, create.value.func) == 'socket.socket'
                and not create.value.keywords and len(create.value.args) == 2
                and [self.symbol(facts, a) for a in create.value.args] == ['socket.AF_INET', 'socket.SOCK_STREAM']):
            return False
        name = create.targets[0].id
        method = lambda s, m: (isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                              and self.check.e.call_chain(s.value.func) == name + '.' + m)
        if not method(bind, 'bind') or len(bind.value.args) != 1 or bind.value.keywords:
            return False
        address = bind.value.args[0]
        if not isinstance(address, ast.Tuple) or len(address.elts) != 2:
            return False
        try:
            host = self.check.value(facts, address.elts[0])
        except Unsupported:
            return False
        if host not in LOOPBACK or not isinstance(address.elts[1], ast.Constant) or address.elts[1].value != 0:
            return False
        if not (isinstance(capture, ast.Assign) and len(capture.targets) == 1 and isinstance(capture.targets[0], ast.Name)
                and isinstance(capture.value, ast.Subscript) and isinstance(capture.value.value, ast.Call)
                and self.check.e.call_chain(capture.value.value.func) == name + '.getsockname'
                and not capture.value.value.args and not capture.value.value.keywords
                and isinstance(capture.value.slice, ast.Constant) and capture.value.slice.value == 1):
            return False
        return (method(close, 'close') and not close.value.args and not close.value.keywords
                and isinstance(ret, ast.Return) and isinstance(ret.value, ast.Name)
                and ret.value.id == capture.targets[0].id)

    def launch(self, ref):
        key = (ref.file, id(ref.node))
        if key in self.cache:
            value = self.cache[key]
            if isinstance(value, str):
                raise Unsupported(value)
            return value
        try:
            value = self._launch(ref)
        except Unsupported as error:
            self.cache[key] = str(error)
            raise
        self.cache[key] = value
        return value

    def _launch(self, ref):
        facts, fn = self.repo.files[ref.file], ref.node
        if (not self.definition_intact(ref) or self.check.e.function_arg_names(fn)
                or len(fn.decorator_list) != 1 or self.symbol(facts, fn.decorator_list[0]) != 'contextlib.contextmanager'):
            raise Unsupported('launcher is not a plain no-argument context manager')
        body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]
        env = {}
        for stmt in body[:-1]:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                raise Unsupported('unknown setup before subprocess')
            if stmt.targets[0].id in env:
                raise Unsupported('launcher value reassigned')
            env[stmt.targets[0].id] = self.resolve(facts, stmt.value, env)
        process = body[-1] if body else None
        if not isinstance(process, ast.With) or len(process.items) != 1:
            raise Unsupported('process lifetime not established')
        item = process.items[0]
        call = item.context_expr
        if (not isinstance(call, ast.Call) or self.symbol(facts, call.func) != 'subprocess.Popen'
                or len(call.args) != 1 or call.keywords or not isinstance(item.optional_vars, ast.Name)):
            raise Unsupported('unsupported process launch')
        command = call.args[0]
        if not isinstance(command, (ast.List, ast.Tuple)) or len(command.elts) < 2 or len(command.elts) % 2:
            raise Unsupported('command is not an explicit script and option list')
        if self.symbol(facts, command.elts[0]) != 'sys.executable':
            raise Unsupported('not the Python executable')
        script = self.resolve(facts, command.elts[1], env)
        if not isinstance(script, PurePosixPath) or script.is_absolute() or '..' in script.parts or str(script) not in self.repo.files:
            raise Unsupported('script path not established in repository')
        options = {}
        for flag, value in zip(command.elts[2::2], command.elts[3::2]):
            if not isinstance(flag, ast.Constant) or not isinstance(flag.value, str) or not flag.value.startswith('--') or flag.value in options:
                raise Unsupported('ambiguous CLI option')
            options[flag.value] = self.resolve(facts, value, env)
        yielded = []
        self.process_body(facts, process.body, env, item.optional_vars.id, yielded)
        if len(yielded) != 1:
            raise Unsupported('service URL does not have one active yield')
        url, where = yielded[0]
        host, port, path = self.url(url)
        return Endpoint(str(script), host, port, path, self.check.loc(facts, call), where, tuple(options.items()))

    @staticmethod
    def url(value):
        if isinstance(value, str):
            from urllib.parse import urlsplit
            try:
                parsed = urlsplit(value)
                if (parsed.scheme == 'http' and parsed.hostname in LOOPBACK and parsed.port
                        and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment):
                    return parsed.hostname, parsed.port, parsed.path.rstrip('/')
            except ValueError:
                raise Unsupported('malformed service URL') from None
        if isinstance(value, Text):
            parts = list(value.parts)
            if len(parts) in (2, 3) and isinstance(parts[0], str) and isinstance(parts[1], (Port, int)):
                host = next((h for h in LOOPBACK if parts[0] == f'http://{h}:'), None)
                suffix = parts[2] if len(parts) == 3 else ''
                if host and isinstance(suffix, str) and (not suffix or suffix.startswith('/')) and not any(c in suffix for c in '?#@'):
                    return host, parts[1], suffix.rstrip('/')
        raise Unsupported('not a correlated local HTTP URL')

    def process_body(self, facts, body, env, proc, yielded):
        for stmt in body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                if stmt.targets[0].id in env:
                    raise Unsupported('process value changed')
                env[stmt.targets[0].id] = self.resolve(facts, stmt.value, env)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield):
                yielded.append((self.resolve(facts, stmt.value.value, env), self.check.loc(facts, stmt)))
            elif isinstance(stmt, ast.Try) and not stmt.handlers and not stmt.orelse:
                self.process_body(facts, stmt.body, env, proc, yielded)
                for final in stmt.finalbody:
                    if not (isinstance(final, ast.Expr) and isinstance(final.value, ast.Call)
                            and self.check.e.call_chain(final.value.func) in {proc + '.kill', proc + '.terminate', proc + '.wait'}
                            and not final.value.args and not final.value.keywords):
                        raise Unsupported('unknown process teardown')
            elif isinstance(stmt, ast.For):
                self.health_poll(facts, stmt, env, proc)
            else:
                raise Unsupported('unknown process lifecycle statement')

    def health_poll(self, facts, loop, env, proc):
        """A bounded GET-only readiness poll; terminal failure cannot reach yield."""
        if not isinstance(loop.iter, ast.Call) or not self.check.builtin(facts, loop.iter.func, {'range'}):
            raise Unsupported('unknown readiness loop')
        if not loop.iter.args or any(not isinstance(a, ast.Constant) or type(a.value) is not int for a in loop.iter.args) or loop.iter.keywords:
            raise Unsupported('dynamic readiness loop')
        if not loop.orelse or not isinstance(loop.orelse[-1], ast.Raise):
            raise Unsupported('readiness failure can continue to yield')
        response_names = set()
        for stmt in loop.body:
            for node in ast.walk(stmt):
                if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom, ast.With, ast.AsyncWith, ast.Delete, ast.AugAssign)):
                    raise Unsupported('unsupported readiness effect')
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in env:
                    raise Unsupported('readiness changes endpoint values')
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                    raise Unsupported('readiness mutates an object')
                if isinstance(node, ast.Call):
                    symbol = self.symbol(facts, node.func)
                    if symbol == 'requests.get' and len(node.args) == 1 and not node.keywords:
                        address = self.resolve(facts, node.args[0], env)
                        # f'{base_url}/health' needs a data-only substitution.
                        if isinstance(address, Text) and len(address.parts) == 2 and isinstance(address.parts[0], Text):
                            address = Text((*address.parts[0].parts, address.parts[1]))
                        self.url(address)
                        parent = facts.parents.get(node)
                        if not isinstance(parent, ast.Assign) or len(parent.targets) != 1 or not isinstance(parent.targets[0], ast.Name):
                            raise Unsupported('readiness response escapes')
                        response_names.add(parent.targets[0].id)
                    elif symbol == 'time.sleep' and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and not node.keywords:
                        pass
                    else:
                        raise Unsupported('unknown readiness call: ' + self.check.e.call_chain(node.func) + ' [' + symbol + ']')
                if isinstance(node, ast.Attribute) and not isinstance(facts.parents.get(node), ast.Call):
                    chain = self.check.e.call_chain(node)
                    if chain not in {n + '.ok' for n in response_names} and self.symbol(facts, node) != 'requests.ConnectionError':
                        raise Unsupported('unknown readiness attribute')
        for stmt in loop.orelse[:-1]:
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                    and self.check.e.call_chain(stmt.value.func) in {proc + '.kill', proc + '.terminate', proc + '.wait'}
                    and not stmt.value.args and not stmt.value.keywords):
                raise Unsupported('unknown readiness failure action')
        error = loop.orelse[-1].exc
        if not (isinstance(error, ast.Call) and self.check.builtin(facts, error.func, {'RuntimeError', 'TimeoutError'})
                and all(isinstance(a, ast.Constant) for a in error.args) and not error.keywords):
            raise Unsupported('unknown readiness failure exception')

    def application(self, endpoint):
        facts = self.repo.files[endpoint.script]
        if facts.tree is None:
            raise Unsupported('service script did not parse')
        blocks = [n for n in facts.tree.body if main_block(n)]
        if len(blocks) != 1:
            raise Unsupported('ambiguous server entry point')
        body = blocks[0].body
        parser_name = args_name = None
        cli = dict(endpoint.options)
        parsed = {}
        used = set()
        app = None
        for stmt in body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and isinstance(stmt.value, ast.Call):
                name, call = stmt.targets[0].id, stmt.value
                if self.symbol(facts, call.func) == 'argparse.ArgumentParser' and not call.args and not call.keywords and parser_name is None:
                    parser_name = name
                elif self.check.e.call_chain(call.func) == str(parser_name) + '.parse_args' and not call.args and not call.keywords and args_name is None:
                    args_name = name
                else:
                    raise Unsupported('unknown server startup assignment')
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if self.check.e.call_chain(call.func) == str(parser_name) + '.add_argument' and args_name is None:
                    if len(call.args) != 1 or not isinstance(call.args[0], ast.Constant) or not isinstance(call.args[0].value, str):
                        raise Unsupported('ambiguous CLI argument')
                    flag = call.args[0].value
                    kw = {k.arg: k.value for k in call.keywords}
                    if flag not in cli or flag in used or set(kw) != {'type'} or not self.check.builtin(facts, kw['type'], {'str', 'int'}):
                        raise Unsupported('CLI argument not supplied or transformed')
                    value = cli[flag]
                    if kw['type'].id == 'int' and not isinstance(value, (int, Port)):
                        raise Unsupported('port not numeric')
                    if kw['type'].id == 'str' and not isinstance(value, str):
                        raise Unsupported('host not a string')
                    parsed[flag[2:].replace('-', '_')] = value
                    used.add(flag)
                elif self.symbol(facts, call.func) == 'uvicorn.run' and args_name and app is None:
                    kw = {k.arg: k.value for k in call.keywords}
                    if len(call.args) != 1 or not isinstance(call.args[0], ast.Name) or set(kw) != {'host', 'port'}:
                        raise Unsupported('unsupported ASGI server options')
                    values = {}
                    for name, expr in kw.items():
                        if not isinstance(expr, ast.Attribute) or not isinstance(expr.value, ast.Name) or expr.value.id != args_name:
                            raise Unsupported('server address is not the CLI address')
                        values[name] = parsed.get(expr.attr)
                    if values != {'host': endpoint.host, 'port': endpoint.port} or used != set(cli):
                        raise Unsupported('client and process addresses differ')
                    app = call.args[0].id
                else:
                    raise Unsupported('unknown server startup call')
            else:
                raise Unsupported('unknown server startup statement')
        if app is None:
            raise Unsupported('no matching ASGI server start')
        return facts, app
