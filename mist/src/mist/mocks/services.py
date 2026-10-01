"""A bounded proof for clients sent to a repository's prepared-response server.

This is an extra negative check, not a network probe or a positive validator.
An explicit endpoint, a running server in the same entry point, and closed local
request handlers are all required. Unsupported code leaves the old result alone.
"""

import ast
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from mist.mocks.scope import Context
from mist.rules.library_rules import LIBRARY_RULES


SERVERS = {'http.server.HTTPServer', 'http.server.ThreadingHTTPServer'}
LOOPBACK = {'localhost', '127.0.0.1', '::1'}
BIND_HOSTS = LOOPBACK | {'', '0.0.0.0', '::'}
HANDLER_MEMBERS = {name for cls in BaseHTTPRequestHandler.__mro__ for name in vars(cls)}


class Unsupported(Exception):
    pass


@dataclass(frozen=True)
class Ref:
    file: str
    node: ast.AST


@dataclass
class Server:
    address: tuple
    handler: Ref
    location: str
    started: str = ''
    valid: bool = True
    started_order: int = 0


@dataclass
class Thread:
    server: Server


@dataclass
class Entry:
    servers: list = field(default_factory=list)
    visited: dict = field(default_factory=dict)
    unknown: bool = False
    clock: int = 0
    files: set = field(default_factory=set)


class ServiceCheck:
    def __init__(self, scope):
        self.scope = scope
        self.repo = scope.repo
        self.e = scope.e
        self.entries = {}
        self.handlers = {}
        self._definitions = {}
        self.last_reason = ''

    def loc(self, facts, node):
        return self.scope.location(facts, node)

    def symbol(self, facts, node):
        return self.scope.resolve(node, facts, node).symbol

    def external_symbol(self, facts, node):
        symbol = self.symbol(facts, node)
        parts = symbol.split('.')
        if any('.'.join(parts[:cut]) in self.repo.module_index for cut in range(1, len(parts))):
            return ''
        return symbol

    def builtin(self, facts, node, names):
        if not isinstance(node, ast.Name) or node.id not in names:
            return False
        if node.id in facts.imports or any(
                (isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == node.id)
                or (isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == node.id)
                or (isinstance(n, ast.arg) and n.arg == node.id)
                for n in facts.nodes):
            return False
        resolved = self.scope.resolve(node, facts, node)
        return not resolved.symbol and not resolved.location and resolved.node is None

    def definition(self, symbol):
        if symbol in self._definitions:
            return self._definitions[symbol]
        parts = symbol.split('.')
        result = None
        for cut in range(len(parts) - 1, 0, -1):
            path = self.repo.module_index.get('.'.join(parts[:cut]))
            if not path:
                continue
            facts = self.repo.files[path]
            body = facts.tree.body if facts.tree else []
            for name in parts[cut:]:
                matches = [n for n in body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
                if len(matches) != 1:
                    break
                node = matches[0]
                body = getattr(node, 'body', [])
            else:
                result = Ref(path, node)
            break
        self._definitions[symbol] = result
        return result

    def value(self, facts, node, env=None, seen=frozenset()):
        """Resolve only constants, imported definitions, and simple URL strings."""
        env = env or {}
        key = (facts.rel_path, id(node))
        if node is None or key in seen or len(seen) > 20:
            raise Unsupported('value not resolved')
        seen = seen | {key}
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple)):
            return tuple(self.value(facts, n, env, seen) for n in node.elts)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for n in node.values:
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    parts.append(n.value)
                elif isinstance(n, ast.FormattedValue) and n.conversion == -1 and n.format_spec is None:
                    v = self.value(facts, n.value, env, seen)
                    if type(v) not in (str, int):
                        raise Unsupported('nonliteral URL part')
                    parts.append(str(v))
                else:
                    raise Unsupported('formatted URL')
            return ''.join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(facts, node.left, env, seen), self.value(facts, node.right, env, seen)
            if type(left) is type(right) and type(left) in (str, int):
                return left + right
        if isinstance(node, ast.Name):
            if node.id in env:
                return env[node.id]
            # A local/conditional assignment must not fall back to a global.
            owner = self.scope.owner(facts, node)
            if owner and (node.id in self.e.function_arg_names(owner) or any(
                    isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id == node.id
                    for n in ast.walk(owner))):
                raise Unsupported('local value not supplied')
            stores = [n for n in facts.nodes if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == node.id]
            if len(stores) == 1:
                parent = facts.parents.get(stores[0])
                if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent in facts.tree.body:
                    return self.value(facts, parent.value, env, seen)
            ref = facts.imports.get(node.id)
            if ref and ref.symbol:
                path = self.repo.module_index.get(ref.module)
                if path:
                    other = self.repo.files[path]
                    matches = [n for n in other.tree.body if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
                        isinstance(t, ast.Name) and t.id == ref.symbol for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
                    all_stores = [n for n in other.nodes if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == ref.symbol]
                    if len(matches) == len(all_stores) == 1:
                        return self.value(other, matches[0].value, {}, seen)
        definition = self.definition(self.symbol(facts, node))
        if definition:
            return definition
        raise Unsupported('value not a constant or project definition')

    def endpoint(self, facts, call):
        """Only documented explicit URL arguments, not an arbitrary URL nearby."""
        constructor = call
        symbol = self.external_symbol(facts, call.func)
        if symbol not in LIBRARY_RULES.service_clients and symbol not in LIBRARY_RULES.http_calls:
            receiver = call.func
            while isinstance(receiver, ast.Attribute):
                receiver = receiver.value
            if isinstance(receiver, ast.Call):
                constructor = receiver
            elif isinstance(receiver, ast.Name):
                owner = self.scope.owner(facts, call)
                assignments = [n for n in self.scope.preceding(facts, call)
                               if self.scope.owner(facts, n) is owner and isinstance(n, ast.Assign)
                               and any(isinstance(t, ast.Name) and t.id == receiver.id for t in n.targets)]
                if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Call):
                    return None
                assignment = assignments[0]
                constructor = assignment.value
                # No intervening writes, calls, aliases, or escapes of this client.
                for n in facts.nodes:
                    if not isinstance(n, ast.Name) or n.id != receiver.id or self.scope.owner(facts, n) is not owner:
                        continue
                    if assignment.lineno < n.lineno <= call.lineno and n is not receiver:
                        return None
            else:
                return None
            symbol = self.external_symbol(facts, constructor.func)
        client_rule = LIBRARY_RULES.service_clients.get(symbol)
        if client_rule:
            keywords = {k.arg: k.value for k in constructor.keywords}
            # A supplied transport, fallback, or unknown keyword expansion can
            # bypass a client's configured endpoint.
            if constructor.args or any(k not in client_rule['allowed_options'] for k in keywords):
                return None
            urls = [v for k, v in keywords.items() if k in client_rule['endpoint_arguments']]
            if len(urls) != 1:
                return None
            expr = urls[0]
        elif symbol in LIBRARY_RULES.http_calls:
            if any(k.arg not in LIBRARY_RULES.http_options for k in call.keywords):
                return None
            pos = LIBRARY_RULES.http_calls[symbol]
            expr = next((k.value for k in call.keywords if k.arg == 'url'), None)
            if expr is None:
                expr = call.args[pos] if len(call.args) > pos else None
        else:
            return None
        try:
            address = self.value(facts, expr)
            if not isinstance(address, str):
                return None
            url = urlsplit(address)
            if url.scheme != 'http' or url.hostname not in LOOPBACK or url.username or url.password:
                return None
            return address, url.port or 80, self.loc(facts, expr), symbol
        except (Unsupported, ValueError):
            return None

    @staticmethod
    def main_guard(node):
        return (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == '__name__'
                and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
                and len(node.comparators) == 1 and isinstance(node.comparators[0], ast.Constant)
                and node.comparators[0].value == '__main__')

    def entry(self, facts):
        if facts.rel_path in self.entries:
            return self.entries[facts.rel_path]
        result = Entry()
        self.entries[facts.rel_path] = result

        def evaluate(current, node, env, depth):
            if depth > 5:
                raise Unsupported('entry call depth')
            if isinstance(node, ast.Call):
                result.clock += 1
                result.visited.setdefault((current.rel_path, id(node)), []).append(result.clock)
                symbol = self.symbol(current, node.func)
                trusted = self.external_symbol(current, node.func)
                if trusted in SERVERS and len(node.args) == 2 and not node.keywords:
                    address = self.value(current, node.args[0], env)
                    handler = self.value(current, node.args[1], env)
                    if not (isinstance(address, tuple) and len(address) == 2 and address[0] in BIND_HOSTS
                            and type(address[1]) is int and isinstance(handler, Ref) and isinstance(handler.node, ast.ClassDef)):
                        raise Unsupported('server address or handler')
                    server = Server(address, handler, self.loc(current, node))
                    result.servers.append(server)
                    return server
                if trusted == 'threading.Thread' and not node.args and all(k.arg in {'target', 'daemon'} for k in node.keywords):
                    target = next((k.value for k in node.keywords if k.arg == 'target'), None)
                    if isinstance(target, ast.Attribute) and target.attr == 'serve_forever':
                        server = self.value(current, target.value, env)
                        if isinstance(server, Server):
                            return Thread(server)
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'start' and not node.args and not node.keywords:
                    thread = self.value(current, node.func.value, env)
                    if isinstance(thread, Thread):
                        thread.server.started = self.loc(current, node)
                        thread.server.started_order = result.clock
                        return None
                function = self.definition(symbol)
                if function and isinstance(function.node, ast.FunctionDef) and not function.node.decorator_list:
                    parameters = [*function.node.args.posonlyargs, *function.node.args.args]
                    if function.node.args.vararg or function.node.args.kwarg or function.node.args.kwonlyargs or node.keywords or len(parameters) != len(node.args):
                        raise Unsupported('entry arguments')
                    values = [self.value(current, a, env) for a in node.args]
                    result.visited.setdefault((function.file, id(function.node)), []).append(result.clock)
                    run(self.repo.files[function.file], function.node.body, dict(zip([a.arg for a in parameters], values)), depth + 1)
                    return None
                # Harmless entry bookkeeping does not alter the proof. All
                # other calls are ignored only when no tracked object escapes.
                if trusted == 'atexit.register' and len(node.args) == 1 and not node.keywords:
                    target = node.args[0]
                    if isinstance(target, ast.Attribute) and target.attr == 'shutdown' and isinstance(self.value(current, target.value, env), Server):
                        return None
                if isinstance(node.func, ast.Attribute) and node.func.attr in {'serve_forever', 'join'} and not node.args and not node.keywords:
                    obj = self.value(current, node.func.value, env)
                    if isinstance(obj, (Server, Thread)):
                        if isinstance(obj, Server):
                            obj.started = self.loc(current, node)
                            obj.started_order = result.clock
                        return None
                if isinstance(node.func, ast.Attribute):
                    try:
                        receiver = self.value(current, node.func.value, env)
                        if isinstance(receiver, (Server, Thread)):
                            result.unknown = True
                    except Unsupported:
                        pass
                for arg in [*node.args, *(k.value for k in node.keywords)]:
                    for name in ast.walk(arg):
                        if isinstance(name, ast.Name):
                            try:
                                if isinstance(self.value(current, name, env), (Server, Thread, Ref)):
                                    result.unknown = True
                            except Unsupported:
                                pass
                return None
            return self.value(current, node, env)

        def run(current, body, env, depth):
            result.files.add(current.rel_path)
            for stmt in body:
                try:
                    if isinstance(stmt, ast.If) and self.main_guard(stmt.test):
                        run(current, stmt.body, env, depth)
                    elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                        for target in targets:
                            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and isinstance(env.get(target.value.id), (Ref, Server, Thread)):
                                result.unknown = True
                        value = evaluate(current, stmt.value, env, depth)
                        for target in targets:
                            if isinstance(target, ast.Name):
                                env[target.id] = value
                            elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and isinstance(env.get(target.value.id), (Ref, Server, Thread)):
                                result.unknown = True
                    elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                        evaluate(current, stmt.value, env, depth)
                    elif isinstance(stmt, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                        # Never use a conditional startup as definite evidence.
                        for name in ast.walk(stmt):
                            if isinstance(name, ast.Name) and name.id in env and isinstance(env[name.id], (Server, Thread, Ref)):
                                result.unknown = True
                    elif isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                        break
                    elif isinstance(stmt, ast.Delete):
                        result.unknown = True
                    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        env[stmt.name] = Ref(current.rel_path, stmt)
                except Unsupported:
                    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                        for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                            if isinstance(target, ast.Name):
                                env.pop(target.id, None)
            return env

        run(facts, facts.tree.body, {}, 0)
        return result

    def handler(self, ref):
        key = (ref.file, id(ref.node))
        if key not in self.handlers:
            try:
                self.handlers[key] = LocalHandler(self, ref).prove()
            except Unsupported as error:
                self.handlers[key] = None
                self.last_reason = str(error)
        return self.handlers[key]

    def analyze(self, source, facts, call):
        endpoint = self.endpoint(facts, call)
        if not endpoint:
            return None
        address, port, endpoint_location, client = endpoint
        entry = self.entry(facts)
        if entry.unknown:
            return None
        servers = [s for s in entry.servers if s.address[1] == port]
        if len(servers) != 1 or not servers[0].started or not servers[0].valid:
            return None
        server = servers[0]
        if self.modified_service(server.handler, entry.files):
            return None
        owner = self.scope.owner(facts, call)
        cls = facts.parents.get(owner)
        # A callback class must be registered by this same entry point. Merely
        # sharing a file or a port number with a server is insufficient.
        if isinstance(cls, ast.ClassDef):
            callbacks = [s for s in entry.servers if s.handler == Ref(facts.rel_path, cls)]
            if not callbacks or any(not s.started or s.started_order <= server.started_order for s in callbacks):
                return None
        else:
            visits = entry.visited.get((facts.rel_path, id(owner or call)), [])
            if not visits or any(order <= server.started_order for order in visits):
                return None
        proof = self.handler(server.handler)
        if proof is None:
            return None
        return Context('mocked', [{'detail': 'explicit client endpoint reaches a started local prepared-response service',
                                  'client': client, 'endpoint': address, 'endpoint_location': endpoint_location,
                                  'server': server.location, 'server_started': server.started,
                                  'handler': self.loc(self.repo.files[server.handler.file], server.handler.node),
                                  'response_locations': proof}])

    def modified_service(self, handler, files):
        """Reject visible replacement of the class or trusted framework APIs."""
        protected = {'json', 'http.server', 'threading'}
        for path in files | {handler.file}:
            facts = self.repo.files[path]
            for node in facts.nodes:
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    symbol = self.symbol(facts, node.value)
                    if self.definition(symbol) == handler or any(symbol == p or symbol.startswith(p + '.') for p in protected):
                        return True
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and path == handler.file and node.id == handler.node.name:
                    return True
                if isinstance(node, ast.Call):
                    symbol = self.symbol(facts, node.func)
                    if symbol in SERVERS:
                        continue
                    # A dynamic patch, callback registration or other escape
                    # of the handler cannot establish a closed implementation.
                    for arg in [*node.args, *(k.value for k in node.keywords)]:
                        if isinstance(arg, (ast.Name, ast.Attribute)):
                            target = self.symbol(facts, arg)
                            if self.definition(target) == handler or target in protected:
                                return True
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and any(arg.value.startswith(p + '.') for p in protected):
                            return True
        return False


class LocalHandler:
    """All handler branches must stay within JSON data and standard server I/O.

    Unknown calls, forwarding clients, external inheritance, descriptors, and
    dynamic dispatch prevent proof. No response text or PTM name is inspected.
    """
    DATA = 'data'
    SELF = 'handler'
    PURE = {'json.loads', 'json.dumps'}
    BUILTINS = {'int', 'str', 'bool', 'len', 'range', 'list', 'tuple', 'dict'}
    DATA_METHODS = {'get', 'items', 'keys', 'values', 'encode', 'decode'}
    SERVER_METHODS = {'send_response', 'send_response_only', 'send_header', 'end_headers'}

    def __init__(self, check, ref):
        self.check = check
        self.facts = check.repo.files[ref.file]
        self.cls = ref.node
        self.methods = {n.name: n for n in self.cls.body if isinstance(n, ast.FunctionDef)}
        self.active = set()
        self.responses = []
        self.steps = 0

    def prove(self):
        if self.cls.decorator_list or self.cls.keywords or len(self.cls.bases) != 1 or self.check.external_symbol(self.facts, self.cls.bases[0]) != 'http.server.BaseHTTPRequestHandler':
            raise Unsupported('handler inheritance')
        if len(self.methods) != len([n for n in self.cls.body if isinstance(n, ast.FunctionDef)]):
            raise Unsupported('duplicate methods')
        for stmt in self.cls.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.Pass)) and not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)):
                raise Unsupported('handler class state or dynamic members')
        for name in self.methods:
            if name.startswith('__') or (name in HANDLER_MEMBERS and name != 'log_message'):
                raise Unsupported('server machinery replaced')
        # Visible class or framework mutations invalidate the standard behavior.
        for node in self.facts.nodes:
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                root = self.check.e.call_chain(node.value)
                if root in {self.cls.name, 'BaseHTTPRequestHandler'}:
                    raise Unsupported('handler method overwritten')
        routes = [n for name, n in self.methods.items() if name.startswith('do_')]
        if not routes:
            raise Unsupported('no request handlers')
        if 'log_message' in self.methods:
            self.function(self.methods['log_message'], [self.SELF, self.DATA], varargs=True)
        for route in routes:
            before = len(self.responses)
            self.function(route, [self.SELF])
            if len(self.responses) == before:
                raise Unsupported('handler has no local response write')
        return sorted(set(self.responses))

    def tick(self):
        self.steps += 1
        if self.steps > 2000:
            raise Unsupported('handler proof budget')

    def function(self, function, arguments, varargs=False):
        if function in self.active or len(self.active) > 8:
            raise Unsupported('recursive handler')
        decorators = [self.check.external_symbol(self.facts, d) for d in function.decorator_list]
        for node, symbol in zip(function.decorator_list, decorators):
            if not self.check.builtin(self.facts, node, {'staticmethod'}) and symbol not in {'typing.override', 'typing_extensions.override'}:
                raise Unsupported('unknown handler decorator')
        args = function.args
        params = [*args.posonlyargs, *args.args]
        if args.kwarg or args.kwonlyargs or (args.vararg and not varargs) or len(params) != len(arguments):
            raise Unsupported('handler arguments')
        env = dict(zip([p.arg for p in params], arguments))
        if args.vararg:
            env[args.vararg.arg] = self.DATA
        for default in args.defaults:
            self.expr(default, env)
        self.active.add(function)
        try:
            self.block(function.body, env)
        finally:
            self.active.remove(function)
        return self.DATA

    def global_data(self, name, seen=frozenset()):
        if name in seen:
            raise Unsupported('recursive global')
        assignments = [n for n in self.facts.tree.body if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
            isinstance(t, ast.Name) and t.id == name for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        if len(assignments) != 1:
            raise Unsupported('unknown response data')
        value = assignments[0].value
        if not isinstance(value, ast.Constant):
            raise Unsupported('mutable or computed global response data')
        # Only simple numeric counter writes are accepted beyond initialization.
        for n in self.facts.nodes:
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id == name:
                parent = self.facts.parents.get(n)
                if parent is assignments[0]:
                    continue
                if isinstance(parent, ast.Assign) and isinstance(parent.value, ast.Constant) and type(parent.value.value) is type(value.value):
                    continue
                if isinstance(parent, ast.AugAssign) and isinstance(parent.op, ast.Add) and isinstance(parent.value, ast.Constant) and type(value.value) is int and type(parent.value.value) is int:
                    continue
                raise Unsupported('response global changed by unknown code')
        return self.DATA

    def expr(self, node, env):
        self.tick()
        if node is None or isinstance(node, ast.Constant):
            return self.DATA
        if isinstance(node, ast.Name):
            return env[node.id] if node.id in env else self.global_data(node.id)
        if isinstance(node, ast.Attribute):
            base = self.expr(node.value, env)
            if base == self.SELF and node.attr in {'headers', 'path'}:
                return self.DATA
            if base == self.SELF and node.attr in {'rfile', 'wfile'}:
                return node.attr
            raise Unsupported('unknown attribute or descriptor')
        if isinstance(node, ast.Call):
            if any(k.arg is None for k in node.keywords) or any(isinstance(a, ast.Starred) for a in node.args):
                raise Unsupported('expanded handler arguments')
            arguments = [self.expr(a, env) for a in node.args]
            keyword_values = [self.expr(k.value, env) for k in node.keywords]
            symbol = self.check.symbol(self.facts, node.func)
            if self.check.external_symbol(self.facts, node.func) in self.PURE or (self.check.builtin(self.facts, node.func, self.BUILTINS) and node.func.id not in env):
                if all(v == self.DATA for v in arguments + keyword_values):
                    return self.DATA
            if isinstance(node.func, ast.Attribute):
                base = self.expr(node.func.value, env)
                method = node.func.attr
                if base == self.SELF and method in self.methods:
                    target = self.methods[method]
                    static = any(self.check.builtin(self.facts, d, {'staticmethod'}) for d in target.decorator_list)
                    if node.keywords:
                        raise Unsupported('keyword helper dispatch')
                    return self.function(target, arguments if static else [self.SELF, *arguments])
                if any(v != self.DATA for v in arguments + keyword_values):
                    raise Unsupported('handler object escaped')
                if base == self.SELF and method in self.SERVER_METHODS:
                    if method in {'send_response', 'send_response_only'}:
                        if not node.args or not isinstance(node.args[0], ast.Constant) or type(node.args[0].value) is not int or not 200 <= node.args[0].value < 600 or 300 <= node.args[0].value < 400:
                            raise Unsupported('redirect or unknown HTTP status')
                        env[('http', 'status')] = True
                        env.pop(('http', 'headers'), None)
                    elif method == 'send_header':
                        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str) or node.args[0].value.lower() == 'location':
                            raise Unsupported('redirect or unknown HTTP header')
                    elif method == 'end_headers':
                        if not env.get(('http', 'status')):
                            raise Unsupported('unknown response status')
                        env[('http', 'headers')] = True
                    return self.DATA
                if base == self.DATA and method in self.DATA_METHODS:
                    return self.DATA
                if base == 'rfile' and method == 'read':
                    return self.DATA
                if base == 'wfile' and method == 'write' and len(arguments) == 1 and not node.keywords:
                    if not env.get(('http', 'headers')):
                        raise Unsupported('raw HTTP response without verified headers')
                    self.responses.append(self.check.loc(self.facts, node))
                    return self.DATA
            raise Unsupported('handler can invoke unknown or external code')
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            inner = dict(env)
            for generator in node.generators:
                if generator.is_async or self.expr(generator.iter, inner) != self.DATA:
                    raise Unsupported('unknown iterator')
                self.assign(generator.target, self.DATA, inner)
                for condition in generator.ifs:
                    self.expr(condition, inner)
            if isinstance(node, ast.DictComp):
                self.expr(node.key, inner)
                self.expr(node.value, inner)
            else:
                self.expr(node.elt, inner)
            return self.DATA
        if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Subscript, ast.Slice,
                             ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
                             ast.JoinedStr, ast.FormattedValue)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr) and self.expr(child, env) != self.DATA:
                    raise Unsupported('operation on a non-data object')
            return self.DATA
        raise Unsupported('unsupported handler expression')

    def assign(self, target, value, env):
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.List, ast.Tuple)) and value == self.DATA:
            for t in target.elts:
                self.assign(t, value, env)
        else:
            raise Unsupported('handler mutates an object')

    def block(self, body, env):
        for stmt in body:
            self.tick()
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                value = self.expr(stmt.value, env)
                for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                    self.assign(target, value, env)
            elif isinstance(stmt, ast.AugAssign):
                self.expr(stmt.target, env)
                self.expr(stmt.value, env)
                self.assign(stmt.target, self.DATA, env)
            elif isinstance(stmt, (ast.Expr, ast.Return)):
                self.expr(stmt.value, env)
            elif isinstance(stmt, ast.If):
                self.expr(stmt.test, env)
                left, right = dict(env), dict(env)
                self.block(stmt.body, left)
                self.block(stmt.orelse, right)
                merged = {k: left[k] for k in left.keys() & right.keys() if left[k] == right[k]}
                env.clear()
                env.update(merged)
            elif isinstance(stmt, (ast.Pass, ast.Global)):
                continue
            else:
                raise Unsupported('unsupported handler statement')
