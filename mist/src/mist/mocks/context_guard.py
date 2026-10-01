"""Check replacements at client construction and invocation.

For an existing binding, follow local ID values to the sink's project class
and check for replacements when it is constructed or called. Incomplete
context remains unresolved.
"""
import ast
from mist.mocks.client_arguments import ClientFlow
from mist.mocks.scope import Context, Patch


class ContextGuard:
    def __init__(self, scope, source, sink):
        self.c, self.source, self.sink = scope, source, sink
        self.repo = scope.repo
        self.facts = self.repo.files[source['file_path']]
        self.sink_facts = self.repo.files[sink.loader_row['file_path']]
        self.call = scope.e.find_call_at_line(self.sink_facts, int(sink.loader_row['line_number']),
                                              sink.loader_row['visible_call_chain'])
        self.flow = ClientFlow(scope, source, (self.sink_facts, self.call))

    def symbol(self, facts, node):
        return self.flow.symbol(facts, node)

    def owned(self, facts, fn):
        return [n for n in ast.walk(fn) if self.c.owner(facts, n) is fn]

    def root(self, node):
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        return node.id if isinstance(node, ast.Name) else ''

    def addresses(self, facts, expr):
        """Find lookup locations for a name or attribute."""
        chain = self.c.e.call_chain(expr)
        if not chain:
            return set()
        root, *tail = chain.split('.')
        modules = {facts.module, *(name for name, path in self.repo.module_index.items()
                                   if path == facts.rel_path)}
        result = {name + '.' + chain for name in modules}
        imported = facts.imports.get(root)
        # Imported modules retain their attribute lookup. A from-imported class
        # is a copied reference, so patching its former import slot is not enough.
        if imported and not imported.symbol:
            result.add('.'.join([imported.module, *tail]))
        return result

    @staticmethod
    def covers(target, address):
        return bool(target) and (target == address or address.startswith(target + '.'))

    def relevant_module_patch(self, patch, expressions):
        if patch.replacement == 'delegate':
            return False
        return any(self.covers(patch.lookup_target or patch.target, address)
                   for facts, expr in expressions for address in self.addresses(facts, expr))

    def mocker_call(self, facts, call):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            return None
        receiver = call.func.value
        owner = self.c.owner(facts, call)
        if call.func.attr != 'patch' or not isinstance(receiver, ast.Name) or owner is None:
            return None
        args = [*owner.args.posonlyargs, *owner.args.args, *owner.args.kwonlyargs]
        arg = next((a for a in args if a.arg == receiver.id), None)
        if arg is None:
            return None
        typed = arg.annotation is not None and self.c.resolve(arg.annotation, facts, owner).symbol == 'pytest_mock.MockerFixture'
        standard = receiver.id == 'mocker' and owner.name.startswith('test')
        if standard and self.c.fixture_service_check.find_fixture(facts, receiver.id) is not None:
            standard = False
        if not (typed or standard) or not call.args:
            return None
        target = self.c.e.literal_string(call.args[0])
        if not target:
            return None
        options = {k.arg: k.value for k in call.keywords}
        kind = 'mock'
        if len(call.args) > 1 or options.keys() & {'new', 'new_callable', 'side_effect', 'wraps', None}:
            kind = 'unknown'
        return Patch(self.c.canonical_target(target), kind, self.c.location(facts, call),
                     detail='pytest-mock replacement', lookup_target=target)

    def patches(self, point):
        patches = list(self.c.active_here(self.facts, point))
        # pytest-mock patches last until explicitly stopped or the test ends.
        for statement in self.c.preceding(self.facts, point):
            if self.c.owner(self.facts, statement) is not self.owner:
                continue
            value = statement.value if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)) else None
            if isinstance(value, ast.Call) and self.c.e.call_chain(value.func) == 'mocker.stopall':
                patches = [p for p in patches if p.detail != 'pytest-mock replacement']
            parsed = self.mocker_call(self.facts, value)
            if parsed:
                patches.append(parsed)
        return patches

    def with_calls(self, point):
        child = point
        while child in self.facts.parents:
            parent = self.facts.parents[child]
            if isinstance(parent, (ast.With, ast.AsyncWith)) and child in parent.body:
                yield from (item.context_expr for item in parent.items if isinstance(item.context_expr, ast.Call))
            if parent is self.owner:
                break
            child = parent

    def sys_modules(self, point, expressions):
        for patch in self.with_calls(point):
            if self.c.resolve(patch.func, self.facts, patch).symbol != 'unittest.mock.patch.dict' or len(patch.args) < 2:
                continue
            target = self.c.e.literal_string(patch.args[0]) or self.c.resolve(patch.args[0], self.facts, patch).symbol
            if target != 'sys.modules' or not isinstance(patch.args[1], ast.Dict):
                continue
            for key, value in zip(patch.args[1].keys, patch.args[1].values):
                module = self.c.e.literal_string(key)
                if not module or self.c.resolve(value, self.facts, patch).kind not in {'mock', 'unknown'}:
                    continue
                for facts, expr in expressions:
                    value_at_lookup = self.c.resolve(expr, facts, expr)
                    local_imports = self.local_imports(facts, expr, module)
                    if not self.covers(module, value_at_lookup.symbol) and not local_imports:
                        continue
                    # Require an import inside the active block or in a called
                    # initializer. An existing imported client is not replaced.
                    local_import = local_imports or any(isinstance(n, (ast.Import, ast.ImportFrom))
                                                        and self.c.owner(facts, n) is self.c.owner(facts, expr)
                                                        and self.c.owner(facts, n) is not None
                                                        and self.c.location(facts, n) == value_at_lookup.location
                                                        for n in facts.nodes)
                    constructed = self.symbol(self.facts, point.func)
                    block_import = any(isinstance(n, (ast.Import, ast.ImportFrom))
                                       and getattr(patch, 'lineno', 0) < n.lineno < point.lineno
                                       and ((isinstance(n, ast.ImportFrom) and self.covers(n.module or '', constructed))
                                            or isinstance(n, ast.Import) and any(self.covers(a.name, constructed) for a in n.names))
                                       for n in self.owned(self.facts, self.owner))
                    if local_import or block_import:
                        yield {'detail': 'SDK import affected by an active sys.modules replacement',
                               'patch': self.c.location(self.facts, patch), 'target': module,
                               'lookup': self.c.location(facts, expr),
                               'limitation': 'import caching and stored client context are not established'}

    def local_imports(self, facts, expr, module):
        """Keep relevant local imports, including guarded optional SDK imports.

        This retains uncertainty, not an assertion that an import branch ran.
        A later local write invalidates the imported reference for this guard.
        """
        owner = self.c.owner(facts, expr)
        if owner is None:
            return []
        root = self.root(expr)
        nodes = self.owned(facts, owner)
        imports = []
        for node in nodes:
            if not isinstance(node, (ast.Import, ast.ImportFrom)) or node.lineno >= expr.lineno:
                continue
            for alias in node.names:
                local = alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name)
                imported = alias.name if isinstance(node, ast.Import) else (node.module or '') + '.' + alias.name
                if local != root or not self.covers(module, imported):
                    continue
                if any(isinstance(n, ast.Name) and n.id == root and isinstance(n.ctx, (ast.Store, ast.Del))
                       and node.lineno < n.lineno < expr.lineno for n in nodes):
                    continue
                imports.append(node)
        return imports

    def instance_setup(self, point, selected_name, sink_suffix):
        expected = selected_name + sink_suffix
        # Last direct write to a relevant slot wins. Do not use a replacement
        # that has been explicitly restored before this invocation.
        writes = {}
        for stmt in self.c.preceding(self.facts, point):
            if self.c.owner(self.facts, stmt) is not self.owner or not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
                chain = self.c.e.call_chain(target)
                if isinstance(target, ast.Attribute) and self.covers(chain, expected):
                    for old in list(writes):
                        if self.covers(chain, old):
                            del writes[old]
                    writes[chain] = (stmt, target)
        for chain, (stmt, target) in writes.items():
            value = self.c.resolve(stmt.value, self.facts, stmt)
            if value.kind == 'mock':
                yield {'detail': 'selected project instance has a replaced client or call',
                       'replacement': self.c.location(self.facts, stmt), 'target': chain,
                       'invocation': self.c.location(self.facts, point)}
        for patch in self.with_calls(point):
            if self.c.resolve(patch.func, self.facts, patch).symbol != 'unittest.mock.patch.object' or len(patch.args) < 2:
                continue
            attr = self.c.e.literal_string(patch.args[1])
            target = self.c.e.call_chain(patch.args[0]) + '.' + attr if attr else ''
            if self.covers(target, expected):
                yield {'detail': 'selected project instance is called inside an object patch',
                       'patch': self.c.location(self.facts, patch), 'target': target,
                       'invocation': self.c.location(self.facts, point)}

    def analyze(self):
        if self.call is None:
            return None
        key = self.c.e.find_literal_node_id(self.facts, self.c.e.strip_quotes(self.source.get('matched_text', '')),
                                            int(self.source['line_number']), int(self.source.get('column_start', '0')))
        literals = [n for n in self.facts.nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and self.c.e.literal_node(self.facts.rel_path, n.lineno, n.col_offset, n.value) == key]
        if len(literals) != 1:
            return None
        literal = literals[0]
        self.owner = self.c.owner(self.facts, literal)
        if self.owner is None:
            return None
        sink_owner = self.c.owner(self.sink_facts, self.call)
        method = next((m for m in self.repo.functions.values() if m.node is sink_owner), None)
        if method is None or not method.class_symbol:
            return None
        nodes = self.owned(self.facts, self.owner)
        if len(nodes) > 5000:
            return None
        selected = set()
        classes = {}
        probes = []
        for parameter, default in self.c.e.function_defaults(self.owner).items():
            if any(n is literal for n in ast.walk(default)):
                selected.add(parameter)
        def carries(node):
            return node is not None and any(n is literal or isinstance(n, ast.Name) and n.id in selected for n in ast.walk(node))
        # Propagate only within this lexical procedure, in statement order.
        for node in sorted(nodes, key=lambda n: (getattr(n, 'lineno', 0), getattr(n, 'col_offset', 0))):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if carries(node.value):
                        selected.add(target.id)
                    elif target.id in selected:
                        selected.remove(target.id)
                    if isinstance(node.value, ast.Call):
                        classes[target.id] = self.symbol(self.facts, node.value.func)
                    elif isinstance(node.value, ast.Name):
                        classes[target.id] = classes.get(node.value.id, '')
            if not isinstance(node, ast.Call):
                continue
            symbol = self.symbol(self.facts, node.func)
            if symbol == method.class_symbol and carries(node):
                probes.append((node, 'creation', ''))
            if isinstance(node.func, ast.Attribute):
                name = self.root(node.func.value)
                if carries(node):
                    selected.add(name)
                if name in selected and classes.get(name) == method.class_symbol:
                    target = self.flow.method(method.class_symbol, node.func.attr)
                    if target and self.flow.may_reach(target, method.class_symbol):
                        probes.append((node, 'invocation', name))
        if not probes:
            return None
        sink_chain = self.c.e.call_chain(self.call.func)
        self_name = method.node.args.args[0].arg if method.node.args.args else 'self'
        suffix = sink_chain[len(self_name):] if sink_chain.startswith(self_name + '.') else ''
        constructor = self.flow.method(method.class_symbol, '__init__')
        constructors = []
        if constructor:
            facts = self.repo.files[constructor.rel_path]
            for n in self.owned(facts, constructor.node):
                if not isinstance(n, (ast.Assign, ast.AnnAssign)) or not isinstance(n.value, ast.Call):
                    continue
                if any(self.covers(self.c.e.call_chain(t), sink_chain)
                       for t in (n.targets if isinstance(n, ast.Assign) else [n.target])):
                    constructors.append((facts, n.value.func))
        evidence = []
        clear_invocation = False
        construction_replaced = False
        for point, kind, name in probes:
            expressions = constructors if kind == 'creation' else [(self.sink_facts, self.call.func)] if not suffix else []
            current = []
            for patch in self.patches(point):
                if self.relevant_module_patch(patch, expressions):
                    current.append({'detail': 'selected client construction or SDK lookup has an active replacement',
                                    'patch': patch.location, 'target': patch.lookup_target or patch.target,
                                    'replacement_kind': patch.replacement, 'context': self.c.location(self.facts, point)})
            current.extend(self.sys_modules(point, expressions))
            if kind == 'invocation' and suffix:
                current.extend(self.instance_setup(point, name, suffix))
            evidence.extend(current)
            if kind == 'creation' and current:
                construction_replaced = True
            if kind == 'invocation' and not current:
                clear_invocation = True
        # A construction replacement can remain stored in an instance after its
        # patch exits. A later unpatched invocation alone does not disprove it.
        if evidence and (construction_replaced or not clear_invocation):
            return Context('unresolved', evidence + [{'detail': 'additional replacement setup requires complete client/caller context'}])
        return None
