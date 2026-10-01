"""Follow mocked callables through project wrappers."""

import ast

from mist.mocks.assignments import MockScope as AssignmentScope
from mist.mocks.callables import CallableFlow


class MockScope(AssignmentScope):
    def __init__(self, engine, builder):
        super().__init__(engine, builder)
        self._wrapper_cache = {}

    def wrapper_result(self, facts, point, target, object_id="", expression=None):
        key = (facts.rel_path, id(point), target, object_id, id(expression))
        if key not in self._wrapper_cache:
            self._wrapper_cache[key] = CallableFlow(self, target, object_id).at(facts, point, expression)
        return self._wrapper_cache[key]

    def active_here(self, facts, point, include_fixtures=True):
        output = []
        local_writes = self.writes_before(facts, point)
        for patch in super().active_here(facts, point, include_fixtures):
            if (patch.detail == "direct assignment" and patch.replacement == "delegate"
                    and not patch.uncertain_scope and self._analysis_sink
                    and self.match(patch, *self._analysis_sink) == "possible"):
                # Restoring a module attribute does not modify a previously
                # copied callable. The named-call check below follows that value.
                continue
            # Leave ordinary patch APIs and established assignment decisions alone.
            if patch.detail not in {"direct assignment", "possible assignment in called project helper"} or (
                    patch.replacement != "unknown" and not patch.uncertain_scope):
                output.append(patch)
                continue
            write = local_writes.get((patch.target, patch.target_object))
            if write is None or patch.location != self.location(write.facts, write.statement):
                # Fixture evidence has already been interpreted in its own scope.
                # An untouched target in the test body cannot cancel that evidence.
                output.append(patch)
                continue
            kind, evidence, limitation = self.wrapper_result(facts, point, patch.target, patch.target_object)
            if kind == "delegate":
                # A wrapper that preserves the previous callable introduces no
                # additional mock exclusion. Other active patches still apply.
                continue
            if kind == "stub":
                patch.replacement = "stub"
                patch.uncertain_scope = False
                patch.detail = "wrapper calls a saved mock or stub"
                patch.replacement_location = " -> ".join(evidence)
            elif limitation:
                patch.detail += "; wrapper analysis: " + limitation
            output.append(patch)
        return output

    def callable_kind(self, expr, facts, point, target, invoked_at=None, seen=frozenset()):
        resolved = self.resolve(expr, facts, point)
        if resolved.kind == 'symbol' and resolved.symbol in self.repo.functions:
            # A project import is not proof that the imported body forwards.
            return 'unknown', resolved.location
        result = super().callable_kind(expr, facts, point, target, invoked_at, seen)
        if result[0] == "unknown" and target and isinstance(expr, ast.Name) and point is invoked_at:
            candidates = [(target, ""), *self.writes_before(facts, point).keys()]
            for name, object_id in dict.fromkeys(candidates):
                kind, evidence, _ = self.wrapper_result(facts, point, name, object_id, expression=expr)
                if kind != "unknown":
                    return kind, " -> ".join(evidence)
        return result
