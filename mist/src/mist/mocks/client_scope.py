"""Follow client construction and method calls when checking for mocks."""

from mist.mocks.wrappers import MockScope as WrapperScope
from mist.mocks.scope import Context
from mist.mocks.client_values import ClientFlow


class MockScope(WrapperScope):
    def __init__(self, engine, builder):
        super().__init__(engine, builder)
        self._client_decorators = {}

    def analyze(self, source, sink):
        previous = super().analyze(source, sink)
        # Client tracking can resolve a missing constructor-to-method link. It
        # cannot dismiss an independent uncertain patch or wrapper decision.
        context_gap = (previous.state == "unresolved"
                       and any(e.get("detail") == "source calling context is not resolved"
                               for e in previous.evidence)
                       and not any(e.get("state") == "unresolved" for e in previous.evidence))
        if previous.state != "clear" and not context_gap:
            return previous
        facts = self.repo.files[sink.loader_row["file_path"]]
        call = self.e.find_call_at_line(facts, int(sink.loader_row["line_number"]),
                                       sink.loader_row["visible_call_chain"])
        if call is None:
            return previous
        result = ClientFlow(self, source, (facts, call)).analyze()
        if result is None:
            return previous
        if result.state == "clear":
            # Object provenance alone cannot dismiss an independent patch rule.
            return previous
        return Context(result.state, [*previous.evidence, *result.evidence])
