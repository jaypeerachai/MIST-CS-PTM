"""Check whether a client is replaced or points to a fixture service."""
import ast
from dataclasses import asdict

from mist.mocks.evidence import MockDecision as EvidenceDecision
from mist.mocks.scope import Context
from mist.mocks.context_guard import ContextGuard


class MockDecision(EvidenceDecision):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.completed = {}

    def analyze(self, source, sink):
        key = (source.get('file_path'), source.get('line_number'), source.get('column_start'),
               source.get('matched_text'), sink.loader_row.get('loader_candidate_id'),
               sink.loader_row.get('file_path'), sink.loader_row.get('line_number'))
        if key in self.completed:
            return self.completed[key]
        result = super().analyze(source, sink)
        if result.state == 'clear':
            guard = ContextGuard(self, source, sink).analyze()
            if guard is None:
                guard = self.fixture_host_context(source, sink)
            if guard is not None:
                result = Context('unresolved', [*result.evidence, *guard.evidence])
                self.audit[-1].update(asdict(result))
                self.cache[key] = result
        self.completed[key] = result
        return result

    def fixture_host_context(self, source, sink):
        """Flag xAI clients whose fixture sets a local api_host.

        The service response still needs checking, so this remains unresolved.
        """
        facts = self.repo.files[source['file_path']]
        sink_facts = self.repo.files[sink.loader_row['file_path']]
        call = self.e.find_call_at_line(sink_facts, int(sink.loader_row['line_number']), sink.loader_row['visible_call_chain'])
        if call is None or facts is not sink_facts:
            return None
        owner = self.owner(facts, call)
        if owner is None or not owner.name.startswith('test'):
            return None
        receiver = call.func
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        if not isinstance(receiver, ast.Name) or receiver.id not in self.e.function_arg_names(owner):
            return None
        matches = []
        for visible in self.fixture_service_check.visible(facts):
            for fn in visible.tree.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for dec in fn.decorator_list:
                    expr = dec.func if isinstance(dec, ast.Call) else dec
                    if self.resolve(expr, visible, dec).symbol not in {'pytest.fixture', 'pytest_asyncio.fixture'}:
                        continue
                    name = fn.name
                    if isinstance(dec, ast.Call):
                        name = next((self.e.literal_string(k.value) for k in dec.keywords if k.arg == 'name'), name)
                    if name == receiver.id:
                        matches.append((visible, fn))
        if len(matches) != 1:
            return None
        visible, fn = matches[0]
        if self.fixture_service_check.parameter_override(facts, owner, receiver.id):
            return None
        for output in ast.walk(fn):
            if not isinstance(output, (ast.Return, ast.Yield)) or self.owner(visible, output) is not fn:
                continue
            node = output.value
            if isinstance(node, ast.Name):
                node = self.resolve(node, visible, output).node
            if not isinstance(node, ast.Call):
                continue
            symbol = self.resolve(node.func, visible, node).symbol
            # Explicit public SDK constructors. This also works inside the SDK
            # repository, where external-only symbol lookup deliberately stops.
            if symbol not in {'xai_sdk.Client', 'xai_sdk.AsyncClient'}:
                continue
            endpoint = next((k.value for k in node.keywords if k.arg == 'api_host'), None)
            prefix = (endpoint.value if isinstance(endpoint, ast.Constant) and isinstance(endpoint.value, str)
                      else ''.join(n.value for n in endpoint.values[:1] if isinstance(n, ast.Constant) and isinstance(n.value, str))
                      if isinstance(endpoint, ast.JoinedStr) else '')
            if prefix.startswith(('localhost:', '127.0.0.1:', '[::1]:')):
                return Context('unresolved', [{'detail': 'selected fixture client uses an explicit local service endpoint',
                                               'fixture': self.location(visible, fn), 'constructor': self.location(visible, node),
                                               'endpoint': self.e.expr_text(endpoint), 'call': self.location(facts, call),
                                               'limitation': 'custom service response path is not established'}])
        return None


def binding_context(engine, builder, source, sink):
    checker = getattr(builder, '_mock_checker', None)
    if checker is None:
        checker = MockDecision(engine, builder)
        builder._mock_checker = checker
    return checker.analyze(source, sink)
