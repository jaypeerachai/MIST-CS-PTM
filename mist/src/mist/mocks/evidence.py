"""Combine structural mock checks with fixture and transport evidence.

Incomplete fixture or transport evidence remains unresolved. It does not prove
that a call is real or that it returns a fake response.
"""

import ast
import copy
from dataclasses import asdict
from pathlib import PurePosixPath

import networkx as nx

from mist.mocks.scope import Context
from mist.mocks.fixture_scope import MockScope as StructuralScope
from mist.mocks.fixtures import (
    Client, FixtureServiceCheck,
)
from mist.mocks.processes import Endpoint
from mist.mocks.services import Unsupported
from mist.rules.library_rules import LIBRARY_RULES


def unique(items):
    result = []
    for item in items:
        if item not in result:
            result.append(item)
    return result


def read_only_symbols(builder):
    """Reuse symbol lookup without its graph-building side effects."""
    view = copy.copy(builder)
    view.graph = nx.graphviews.generic_graph_view(builder.graph)
    # Payload lookup can record provider hints even when graph writes are
    # disabled. Keep those query-local hints separate from the shared builder.
    view.payload_provider_keys = copy.deepcopy(builder.payload_provider_keys)
    view._add_node = lambda *args, **kwargs: None
    view._add_edge = lambda *args, **kwargs: None
    return view


class FixtureEvidence(FixtureServiceCheck):
    """Retain partial endpoint proof even if a later fixture step is unsupported."""

    def __init__(self, scope):
        super().__init__(scope)
        self.partial = []
        self.partial_by_fixture = {}

    def fixture(self, facts, test, name):
        key = (facts.rel_path, id(test), name)
        if key in self.fixture_cache:
            self.partial.extend(self.partial_by_fixture.get(key, ()))
        start = len(self.partial)
        try:
            return super().fixture(facts, test, name)
        finally:
            if key not in self.partial_by_fixture:
                self.partial_by_fixture[key] = list(self.partial[start:])

    def fixture_expr(self, facts, fn, node, env, test_facts, test):
        values = super().fixture_expr(facts, fn, node, env, test_facts, test)
        for value in values:
            if isinstance(value, Client):
                self.partial.append({
                    'detail': 'selected client receives an explicit fixture-service endpoint',
                    'constructor': value.location, 'client': value.origin,
                    'process_launch': value.endpoint.launch,
                    'service_url_yield': value.endpoint.yielded,
                })
        return values

    def analyze(self, source, facts, call):
        self.partial = []
        result = super().analyze(source, facts, call)
        if result is None and self.partial:
            return Context('unresolved', [*unique(self.partial), {
                'detail': 'fixture service setup is only partly established',
                'limitation': self.last_reason,
                'call': self.loc(facts, call),
            }])
        return result


class MockDecision(StructuralScope):
    def __init__(self, engine, builder):
        super().__init__(engine, read_only_symbols(builder))
        self.fixture_service_check = FixtureEvidence(self)
        self.audit = []
        self.cache = {}

    def analyze(self, source, sink):
        key = (source.get('file_path'), source.get('line_number'),
               source.get('column_start'), source.get('matched_text'),
               sink.loader_row.get('loader_candidate_id'),
               sink.loader_row.get('file_path'), sink.loader_row.get('line_number'))
        if key in self.cache:
            return self.cache[key]
        result = super().analyze(source, sink)
        if result.state == 'clear':
            constructed = self.inline_constructor_context(sink)
            if constructed is not None:
                result = constructed
        if result.state == 'clear':
            guard = self.unresolved_setup(source, sink)
            if guard:
                result = Context('unresolved', unique([*result.evidence, *guard]))
        self.cache[key] = result
        self.audit.append({
            'source_file': source.get('file_path'), 'source_line': source.get('line_number'),
            'source_column': source.get('column_start'),
            'occurrence': source.get('model_id_occurrence_id'),
            'sink_file': sink.loader_row.get('file_path'),
            'sink_line': sink.loader_row.get('line_number'),
            'sink': sink.loader_row.get('loader_candidate_id'),
            **asdict(result),
        })
        return result

    def inline_constructor_context(self, sink):
        """Follow a patched constructor through an inline client method call."""
        facts = self.repo.files[sink.loader_row['file_path']]
        call = self.e.find_call_at_line(facts, int(sink.loader_row['line_number']),
                                        sink.loader_row['visible_call_chain'])
        if call is None:
            return None
        receiver = call.func
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        if not isinstance(receiver, ast.Call):
            return None
        constructor = self.resolve(receiver.func, facts, receiver)
        if constructor.kind != 'symbol' or not constructor.symbol:
            return None
        for patch in self.active_here(facts, receiver):
            if patch.target != constructor.symbol + '.__new__' or patch.replacement == 'delegate':
                continue
            state = 'mocked' if patch.replacement == 'mock' and not patch.uncertain_scope else 'unresolved'
            return Context(state, [{
                'detail': 'inline client constructor has an active replacement',
                'constructor': self.location(facts, receiver), 'call': self.location(facts, call),
                'target': patch.target, 'patch': patch.location,
                'replacement': patch.replacement_location, 'replacement_kind': patch.replacement,
            }])
        return None

    def unresolved_setup(self, source, sink):
        facts = self.repo.files[sink.loader_row['file_path']]
        call = self.e.find_call_at_line(facts, int(sink.loader_row['line_number']),
                                        sink.loader_row['visible_call_chain'])
        if call is None:
            return []
        evidence = []
        origin = (sink.loader_row.get('receiver_origin') or
                  sink.loader_row.get('linked_import_origin') or
                  sink.loader_row.get('matched_rule_import_origin') or '').split('.')[0]
        # A patch below an opaque SDK call is not resolved by matching the SDK's
        # own method name. Retain the explicit active transport patch as a gap.
        for patch in self.active_here(facts, call):
            if LIBRARY_RULES.transport_may_affect(patch.target, origin) and patch.replacement != 'delegate':
                evidence.append({
                    'detail': 'active HTTP transport replacement; SDK request path not established',
                    'patch': patch.location, 'target': patch.target,
                    'replacement': patch.replacement_location, 'call': self.location(facts, call),
                })
        source_facts = self.repo.files[source['file_path']]
        test = self.e.enclosing_function_for_line(source_facts, int(source['line_number']))
        filename = PurePosixPath(source_facts.rel_path).name
        if test is None or not test.name.startswith('test') or not (
                filename.startswith('test_') or filename.endswith('_test.py')):
            return evidence
        # Only visible, requested/autouse fixtures belong to this test.
        service = self.fixture_service_check
        try:
            pending = set(self.e.function_arg_names(test))
            for visible in service.visible(source_facts):
                for fn in visible.tree.body:
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    meta = service.fixture_meta(visible, fn)
                    if meta and isinstance(meta[1].get('autouse'), ast.Constant) and meta[1]['autouse'].value is True:
                        pending.add(meta[0])
            seen = set()
            while pending - seen:
                name = sorted(pending - seen)[0]
                seen.add(name)
                found = service.find_fixture(source_facts, name)
                if found is None or service.parameter_override(source_facts, test, name):
                    continue
                fixture_facts, fn = found
                pending.update(self.e.function_arg_names(fn))
                for node in ast.walk(fn):
                    if self.owner(fixture_facts, node) is not fn or not isinstance(node, ast.Call):
                        continue
                    # Environment routing is a concern only when the assigned
                    # endpoint is itself linked to an established local service.
                    endpoint_variables = LIBRARY_RULES.endpoint_variables(origin)
                    if (endpoint_variables and not self.explicit_endpoint(facts, call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr == 'setenv' and len(node.args) == 2
                            and isinstance(node.args[0], ast.Constant)
                            and node.args[0].value in endpoint_variables
                            and self.is_fixture_monkeypatch(service, source_facts, fixture_facts, fn, node.func.value)):
                        try:
                            endpoints = service.fixture_expr(fixture_facts, fn, node.args[1], {}, source_facts, test)
                        except Unsupported:
                            continue
                        for endpoint in endpoints:
                            if isinstance(endpoint, Endpoint):
                                evidence.append({
                                    'detail': 'active fixture sets an SDK endpoint to a local service; environment routing not established',
                                    'environment': node.args[0].value,
                                    'assignment': self.location(fixture_facts, node),
                                    'process_launch': endpoint.launch,
                                    'service_url_yield': endpoint.yielded,
                                    'call': self.location(facts, call),
                                })
        except Unsupported:
            # No name-only fallback. An ambiguous fixture name alone does not
            # establish either a mock or a connection to this call.
            pass
        return evidence

    def explicit_endpoint(self, facts, call):
        """An explicit SDK URL takes priority over its environment default."""
        receiver = call.func
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        if isinstance(receiver, ast.Name):
            owner = self.owner(facts, call)
            assignments = [stmt for stmt in self.preceding(facts, call)
                           if self.owner(facts, stmt) is owner and isinstance(stmt, ast.Assign)
                           and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                           and stmt.targets[0].id == receiver.id]
            if len(assignments) != 1:
                return False
            receiver = assignments[0].value
        if not isinstance(receiver, ast.Call):
            return False
        symbol = self.fixture_service_check.external_symbol(facts, receiver.func)
        return LIBRARY_RULES.has_explicit_endpoint(symbol, (k.arg for k in receiver.keywords))

    def is_fixture_monkeypatch(self, service, test_facts, facts, fn, expr):
        if service.external_symbol(facts, expr) == 'pytest.MonkeyPatch':
            return True
        return (isinstance(expr, ast.Name) and expr.id == 'monkeypatch'
                and expr.id in self.e.function_arg_names(fn)
                and service.find_fixture(test_facts, expr.id) is None)


def binding_context(engine, builder, source, sink):
    checker = getattr(builder, '_mock_checker', None)
    if checker is None:
        checker = MockDecision(engine, builder)
        builder._mock_checker = checker
    return checker.analyze(source, sink)
