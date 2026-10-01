"""Check whether a selected endpoint serves prepared responses."""

from mist.mocks.local_scope import MockScope as LocalClientScope
from mist.mocks.services import ServiceCheck


class MockScope(LocalClientScope):
    def __init__(self, engine, builder):
        super().__init__(engine, builder)
        self.service_check = ServiceCheck(self)

    def analyze(self, source, sink):
        previous = super().analyze(source, sink)
        if previous.state != 'clear':
            return previous
        facts = self.repo.files[sink.loader_row['file_path']]
        call = self.e.find_call_at_line(facts, int(sink.loader_row['line_number']), sink.loader_row['visible_call_chain'])
        if call is None:
            return previous
        result = self.service_check.analyze(source, facts, call)
        if result is not None:
            result.evidence = [*previous.evidence, *result.evidence]
            return result
        return previous
