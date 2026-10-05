"""Repository facts, sources, sinks, and trace records."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
import ast


@dataclass(frozen=True)
class ImportRef:
    alias: str
    module: str
    symbol: str = ""
    is_project: bool = False
    line_number: int = 0


@dataclass(frozen=True)
class FunctionDefInfo:
    symbol: str
    module: str
    rel_path: str
    name: str
    class_symbol: str = ""
    class_name: str = ""
    node: ast.AST | None = None
    params: tuple[str, ...] = ()
    line_number: int = 0


@dataclass(frozen=True)
class ClassDefInfo:
    symbol: str
    module: str
    rel_path: str
    name: str
    bases: tuple[str, ...] = ()
    node: ast.ClassDef | None = None
    line_number: int = 0


@dataclass(frozen=True)
class Scope:
    module: str
    rel_path: str
    scope_id: str
    function_symbol: str = ""
    class_symbol: str = ""
    function_name: str = ""
    class_name: str = ""


@dataclass
class FileFacts:
    rel_path: str
    full_path: Path
    module: str
    lines: list[str]
    tree: ast.AST | None
    parse_error: str = ""
    nodes: tuple[ast.AST, ...] = ()
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)
    imports: dict[str, ImportRef] = field(default_factory=dict)
    functions: list[FunctionDefInfo] = field(default_factory=list)
    classes: list[ClassDefInfo] = field(default_factory=list)


@dataclass
class RepoFacts:
    repo: Path
    module_index: dict[str, str]
    path_to_module: dict[str, str]
    files: dict[str, FileFacts]
    functions: dict[str, FunctionDefInfo]
    classes: dict[str, ClassDefInfo]
    functions_by_module_name: dict[tuple[str, str], list[str]]
    classes_by_module_name: dict[tuple[str, str], str]


@dataclass
class AnalysisStatus:
    jedi_status: str = "not_run"
    jedi_files_ok: int = 0
    jedi_files_failed: int = 0
    jedi_error: str = ""


@dataclass(frozen=True)
class TraceResult:
    trace_id: str
    model_id_occurrence_id: str
    loader_candidate_id: str
    repo_snapshot: str
    file_path: str
    model_line_number: int
    loader_file_path: str
    loader_line_number: str
    model_location_url: str
    loader_location_url: str
    canonical_model_id: str
    matched_text: str
    model_ast_context: str
    direct_carrier_name: str
    loader_call: str
    loader_origin: str
    trace_status: str
    path_length: str
    used_interprocedural_edges: bool
    used_interfile_edges: bool
    binding_locality: str = "unresolved"


@dataclass(frozen=True)
class TraceStep:
    trace_id: str
    step_index: int
    step_type: str
    file_path: str
    line_number: str
    node_id: str
    evidence: str


@dataclass(frozen=True)
class SourceInfo:
    row: dict[str, str]
    node_id: str
    literal_value: str


@dataclass(frozen=True)
class SinkInfo:
    loader_row: dict[str, str]
    sink_node_id: str
    call_node_id: str
    mocked_reason: str = ""


@dataclass(frozen=True)
class MockPatchIndex:
    """Repo-level test patch hints keyed by fixture name."""

    fixture_targets: dict[str, frozenset[str]]
    autouse_targets: frozenset[str]


@dataclass(frozen=True)
class BindingLoaderRule:
    import_origin: str
    model_loader: str
    terminal_call: str
    chain_suffix: str
    model_args: frozenset[str]


@dataclass(frozen=True)
class IdentityVocabulary:
    """Codebook-derived words used to recognize providers and loader actions."""

    provider_terms: frozenset[str] = frozenset()
    loader_action_terms: frozenset[str] = frozenset()
    loader_identity_terms: frozenset[str] = frozenset()
    endpoint_terms: frozenset[str] = frozenset()
    codebook_path: str = ""
    codebook_status: str = "not_loaded"
    codebook_rows_loaded: int = 0
    call_rows_loaded: int = 0

    def loader_shape_terms(self) -> set[str]:
        return set(self.loader_action_terms | self.loader_identity_terms)

    def provider_or_loader_terms(self) -> set[str]:
        return set(self.provider_terms | self.loader_action_terms | self.loader_identity_terms)

    def summary(self) -> dict[str, Any]:
        return {
            "codebook_path": self.codebook_path,
            "codebook_status": self.codebook_status,
            "codebook_rows_loaded": self.codebook_rows_loaded,
            "call_rows_loaded": self.call_rows_loaded,
            "provider_terms_count": len(self.provider_terms),
            "loader_action_terms_count": len(self.loader_action_terms),
            "loader_identity_terms_count": len(self.loader_identity_terms),
            "endpoint_terms_count": len(self.endpoint_terms),
            "provider_terms_sample": sorted(self.provider_terms)[:40],
            "loader_action_terms_sample": sorted(self.loader_action_terms)[:40],
            "loader_identity_terms_sample": sorted(self.loader_identity_terms)[:40],
        }
