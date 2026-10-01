"""Check supplied clients that return local responses."""

from mist.mocks.argument_scope import MockScope as ArgumentScope
from mist.mocks.local_clients import LocalClientFlow
from mist.mocks.scope import Context


class MockScope(ArgumentScope):
    def analyze(self, source, sink):
        previous = super().analyze(source, sink)
        if previous.state != 'clear':
            return previous
        facts = self.repo.files[sink.loader_row['file_path']]
        call = self.e.find_call_at_line(facts, int(sink.loader_row['line_number']), sink.loader_row['visible_call_chain'])
        if call is None:
            return previous
        result = LocalClientFlow(self, source, (facts, call)).analyze()
        return Context(result.state, [*previous.evidence, *result.evidence]) if result is not None else previous
