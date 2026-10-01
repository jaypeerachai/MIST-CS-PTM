"""Check services and clients supplied through fixtures."""

from mist.mocks.service_scope import MockScope as ServiceScope
from mist.mocks.fixtures import FixtureServiceCheck


class MockScope(ServiceScope):
    def __init__(self, engine, builder):
        super().__init__(engine, builder)
        self.fixture_service_check = FixtureServiceCheck(self)

    def analyze(self, source, sink):
        previous = super().analyze(source, sink)
        if previous.state != 'clear':
            return previous
        facts = self.repo.files[sink.loader_row['file_path']]
        call = self.e.find_call_at_line(facts, int(sink.loader_row['line_number']), sink.loader_row['visible_call_chain'])
        if call is None:
            return previous
        result = self.fixture_service_check.analyze(source, facts, call)
        if result is not None:
            result.evidence = [*previous.evidence, *result.evidence]
            return result
        return previous
