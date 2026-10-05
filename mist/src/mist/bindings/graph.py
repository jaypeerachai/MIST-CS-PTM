"""Graph support for PTM binding analysis."""

from __future__ import annotations

from collections import defaultdict
from mist.bindings.frameworks import GraphEdge as FrameworkGraphEdge
from mist.bindings.frameworks import build_overlay as build_framework_config_overlay
from mist.bindings.frameworks import load_modules as load_framework_modules
from mist.bindings.locations import enrich_registry_summaries
from mist.io import parse_bool
from mist.io import parse_int
from mist.io import strip_quotes
from mist.rules.binding_terms import REQUEST_BODY_ARG_NAMES
from mist.rules.binding_terms import VALUE_PRESERVING_METHODS
from pathlib import Path
from typing import Any
import ast
import json
import networkx as nx
from mist.environment import project as analysis_project
from mist.bindings.constants import (
    GENERIC_HTTP_ORIGINS,
    MODEL_ARG_NAMES,
    MODEL_PAYLOAD_KEYS,
)
from mist.bindings.repository import (
    direct_function_returns,
    extract_classes,
    extract_functions,
    extract_imports,
    function_defaults,
    is_property_function,
    literal_annotation_values,
    parse_file,
    single_model_like_param,
)
from mist.bindings.syntax import (
    attr_node,
    build_mock_patch_index,
    call_chain,
    call_result_node,
    callable_node_key,
    class_looks_like_config,
    class_looks_like_model_registry,
    config_key_from_line,
    config_value_node,
    expr_looks_like_config_arg,
    expr_reads_config_model_field,
    expr_text,
    external_import_origins,
    field_node,
    find_call_at_line,
    find_literal_node_id,
    first_matching_provider_origin,
    identity_terms_from_text,
    is_config_arg_name,
    is_model_arg_name,
    jedi_lookup_column,
    jedi_sys_paths,
    line_at,
    literal_key,
    literal_node,
    literal_string,
    load_binding_loader_rules,
    mapping_item_node,
    mocked_loader_reason,
    module_node,
    payload_key_from_node,
    payload_literal_node,
    payload_node,
    payload_version_node,
    receiver_chain_from_visible_chain,
    return_node,
    sink_node,
    binding_rules_for_chain,
    subscript_key,
    target_names,
    target_text,
    unique,
    var_node,
    widget_node,
)
from mist.bindings.tracing import (
    split_pipe_values,
    url_text_has_model_endpoint,
)
from mist.bindings.types import (
    ClassDefInfo,
    FileFacts,
    FunctionDefInfo,
    IdentityVocabulary,
    RepoFacts,
    Scope,
    SinkInfo,
    SourceInfo,
    BindingLoaderRule,
)


class SourceSinkBuilder:
    """Build the source/sink graph used for final reuse tracing."""

    def __init__(
        self,
        repo_facts: RepoFacts,
        model_rows: list[dict[str, str]],
        loader_rows: list[dict[str, str]],
        identity_vocab: IdentityVocabulary,
        *,
        enable_jedi: bool = False,
        reuse_codebook: Path | None = None,
        framework_config_summaries: Path | None = None,
    ) -> None:
        self.repo_facts = repo_facts
        self.model_rows = model_rows
        self.loader_rows = loader_rows
        self.identity_vocab = identity_vocab
        self.enable_jedi = enable_jedi
        self.reuse_codebook = reuse_codebook
        self.framework_config_summaries = framework_config_summaries
        self.graph = nx.DiGraph()
        self.node_meta: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, SourceInfo] = {}
        self.sinks: dict[str, SinkInfo] = {}
        self.jedi_callable_symbols: dict[tuple[str, int, int], set[str]] = defaultdict(set)
        self.jedi_added_rel_paths: set[str] = set()
        self.jedi_resolution_edges_added = 0
        self.jedi_internal_sinks_added = 0
        self.jedi_error = ""
        self.loader_rules = load_binding_loader_rules(reuse_codebook) if reuse_codebook else []
        self.jedi_probe_windows = self._build_jedi_probe_windows()
        self.object_types: dict[str, set[str]] = defaultdict(set)
        self.payload_aliases: dict[str, set[str]] = defaultdict(set)
        self.payload_provider_keys: dict[str, set[str]] = defaultdict(set)
        self.payload_assignment_versions: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
        self.mapping_entries: dict[str, set[str]] = defaultdict(set)
        self.class_registries: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.module_vars: dict[tuple[str, str], str] = {}
        self.sink_locations: set[tuple[str, int]] = set()
        self.mock_patch_index = build_mock_patch_index(repo_facts)
        self.framework_config_channels = 0
        self.framework_config_edges_added = 0

    def build(self) -> None:
        if self.enable_jedi:
            self._augment_repo_with_jedi_symbols()
        self._register_module_vars()
        self._register_sources()
        self._seed_import_edges()
        self._seed_object_types()
        self._build_value_flow()
        self._propagate_payload_providers()
        self._register_dynamic_registry_kwargs_edges()
        self._propagate_payload_providers()
        self._register_loader_sinks()
        self._register_framework_config_edges()
        # Evidence enrichment must not change graph reachability or sink choice.
        enrich_registry_summaries(
            self.graph,
            self.repo_facts,
            {source.node_id: source.row for source in self.sources.values()},
            {sink.sink_node_id: sink.loader_row for sink in self.sinks.values()},
        )

    def _register_framework_config_edges(self) -> None:
        """Add guarded framework-state and lexical-closure edges to the graph."""
        summary_path = self.framework_config_summaries
        if summary_path is None or not summary_path.is_file():
            return
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        summaries = payload.get("summaries", [])
        if not isinstance(summaries, list):
            raise ValueError(f"Invalid framework summary list in {summary_path}")

        base_edges = [
            FrameworkGraphEdge(
                str(source),
                str(target),
                str(data.get("edge_type", "")),
                str(data.get("evidence", "")),
            )
            for source, target, data in self.graph.edges(data=True)
        ]
        overlay_edges, channels = build_framework_config_overlay(
            load_framework_modules(self.repo_facts.repo), summaries, base_edges
        )
        added = 0
        for edge in overlay_edges:
            if self.graph.has_edge(edge.source, edge.target):
                continue
            self._add_edge(edge.source, edge.target, edge.edge_type, edge.evidence)
            added += 1
        self.framework_config_channels = len(channels)
        self.framework_config_edges_added = added

    def _register_module_vars(self) -> None:
        for facts in self.repo_facts.files.values():
            if facts.tree is None:
                continue
            for node in facts.tree.body:
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                for target in targets:
                    for name in target_names(target):
                        self.module_vars[(facts.module, name)] = var_node(facts.module, name)

    def _register_sources(self) -> None:
        for row in self.model_rows:
            rel_path = row["file_path"]
            facts = self.repo_facts.files.get(rel_path)
            literal = strip_quotes(row.get("matched_text", ""))
            line = parse_int(row.get("line_number"))
            col = parse_int(row.get("column_start"))
            source_node = ""
            if facts and facts.tree is not None:
                source_node = find_literal_node_id(facts, literal, line, col)
            if not source_node:
                source_node = literal_node(rel_path, line, col, literal)
            self._add_node(
                source_node,
                kind="source_literal",
                file_path=rel_path,
                line_number=line,
                label=literal,
            )
            config_key = config_key_from_line(row.get("line_text", ""))
            if config_key:
                config_node = config_value_node(rel_path, line, config_key, literal)
                self._add_node(
                    config_node,
                    kind="config_field_value",
                    file_path=rel_path,
                    line_number=line,
                    label=f"{config_key}={literal}",
                )
                self._add_edge(
                    source_node,
                    config_node,
                    "config_template_field_value",
                    f"model ID appears in config field {config_key}",
                )
            self.sources[row["model_id_occurrence_id"]] = SourceInfo(row, source_node, literal)

    def _seed_import_edges(self) -> None:
        for facts in self.repo_facts.files.values():
            module_scope = Scope(
                module=facts.module,
                rel_path=facts.rel_path,
                scope_id=facts.module,
            )
            for alias, ref in facts.imports.items():
                if not ref.is_project:
                    continue
                local = var_node(module_scope.scope_id, alias)
                if ref.symbol:
                    exported = self.module_vars.get((ref.module, ref.symbol))
                    if exported:
                        self._add_edge(exported, local, "project_import_value", f"from {ref.module} import {ref.symbol}")
                else:
                    module_ref = module_node(ref.module)
                    self._add_node(module_ref, kind="module", label=ref.module)
                    self._add_edge(module_ref, local, "project_import_module", f"import {ref.module} as {alias}")

    def _seed_object_types(self) -> None:
        for facts in self.repo_facts.files.values():
            if facts.tree is None:
                continue
            for node in facts.nodes:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
                    if not isinstance(value, ast.Call):
                        continue
                    scope = self.scope_for_node(facts, node)
                    class_symbols = self.resolve_constructor(facts, scope, value)
                    if not class_symbols:
                        continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        for target_node in self.target_value_nodes(facts, scope, target):
                            self.object_types[target_node].update(class_symbols)
            self._seed_property_return_object_types(facts)

    def _seed_property_return_object_types(self, facts: FileFacts) -> None:
        """Treat @property/@cached_property return constructors as object types."""
        for info in facts.functions:
            if not info.class_symbol or not is_property_function(info.node):
                continue
            scope = Scope(
                module=facts.module,
                rel_path=facts.rel_path,
                scope_id=info.symbol,
                function_symbol=info.symbol,
                class_symbol=info.class_symbol,
                function_name=info.name,
                class_name=info.class_name,
            )
            target_node = field_node(info.class_symbol, info.name)
            self._add_node(
                target_node,
                kind="class_field",
                file_path=facts.rel_path,
                line_number=info.line_number,
                label=f"self.{info.name}",
            )
            for return_stmt in direct_function_returns(facts, info.node):
                if not isinstance(return_stmt.value, ast.Call):
                    continue
                class_symbols = self.resolve_constructor(facts, scope, return_stmt.value)
                if not class_symbols:
                    continue
                self.object_types[target_node].update(class_symbols)
                self._add_edge(
                    return_node(info.symbol),
                    target_node,
                    "property_return_object_type",
                    f"{info.name} returns {expr_text(return_stmt.value)}",
                )

    def _build_value_flow(self) -> None:
        for facts in self.repo_facts.files.values():
            if facts.tree is None:
                continue
            for node in facts.nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._process_function_defaults(facts, node)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    self._process_assignment(facts, node)
                elif isinstance(node, ast.For):
                    self._process_for_loop(facts, node)
                elif isinstance(node, ast.Return):
                    self._process_return(facts, node)
                elif isinstance(node, ast.Call):
                    self._process_call_edges(facts, node)

    def _process_function_defaults(self, facts: FileFacts, node: ast.AST) -> None:
        info = self.function_info_for_node(facts, node)
        if not info:
            return
        defaults = function_defaults(node)
        scope = Scope(
            module=facts.module,
            rel_path=facts.rel_path,
            scope_id=info.symbol,
            function_symbol=info.symbol,
            class_symbol=info.class_symbol,
            function_name=info.name,
            class_name=info.class_name,
        )
        for param, default in defaults.items():
            target = var_node(info.symbol, param)
            for source in self.value_nodes(facts, scope, default):
                self._add_edge(source, target, "function_default", f"default {param} = {expr_text(default)}")
            for payload_source in self.payload_nodes(facts, scope, default):
                self._add_edge(
                    payload_source,
                    payload_node(info.symbol, param),
                    "function_default_payload",
                    f"default {param} has model payload",
                )

    def _process_assignment(self, facts: FileFacts, node: ast.AST) -> None:
        value = node.value
        scope = self.scope_for_node(facts, node)
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_nodes = self.value_nodes(facts, scope, value)
        payload_sources = self.payload_nodes(facts, scope, value)

        for target in targets:
            target_nodes = self.target_value_nodes(facts, scope, target)
            class_registry = self.class_registry_from_dict(facts, scope, value)
            if class_registry:
                for target_node in target_nodes:
                    for provider_key, class_symbols in class_registry.items():
                        self.class_registries[target_node][provider_key].update(class_symbols)
            if isinstance(value, ast.Dict):
                for target_node in target_nodes:
                    self._register_mapping_entries(facts, scope, target_node, value)
            for source in value_nodes:
                for target_node in target_nodes:
                    self._add_edge(source, target_node, "assignment", f"{target_text(target)} = {expr_text(value)}")
            self._copy_model_field_payloads(
                value_nodes,
                target_nodes,
                "assignment_model_field_payload",
                f"{target_text(target)} receives model-bearing fields from {expr_text(value)}",
            )
            for target_payload in self.target_payload_nodes(facts, scope, target):
                for payload_source in payload_sources:
                    self._add_edge(payload_source, target_payload, "payload_assignment", f"{target_text(target)} receives model payload")
            for version_payload in self.target_payload_version_nodes(facts, scope, target, node.lineno):
                for payload_source in payload_sources:
                    self._add_edge(
                        payload_source,
                        version_payload,
                        "payload_assignment_version",
                        f"{target_text(target)} receives model payload at line {node.lineno}",
                    )
            for target_node in target_nodes:
                for payload_source in payload_sources:
                    key = payload_key_from_node(payload_source)
                    if not key or not is_model_arg_name(key):
                        continue
                    self._add_edge(
                        payload_source,
                        payload_node(target_node, key),
                        "scoped_payload_field_assignment",
                        f"{target_text(target)}.{key} receives model payload",
                    )
                    self._add_edge(
                        payload_source,
                        attr_node(target_node, key),
                        "scoped_config_field_assignment",
                        f"{target_text(target)}.{key} receives model value",
                    )

            if isinstance(value, ast.Call):
                for ret in self.call_return_nodes(facts, scope, value):
                    for target_node in target_nodes:
                        self._add_edge(ret, target_node, "call_return_assignment", f"{target_text(target)} = {expr_text(value)}")
            self._process_mapping_model_payload_assignment(facts, scope, target, value, node.lineno)

    def _process_mapping_model_payload_assignment(
        self,
        facts: FileFacts,
        scope: Scope,
        target: ast.AST,
        value: ast.AST,
        line_number: int,
    ) -> None:
        """Track mutations like payload["model"] = self._model."""
        if not isinstance(target, ast.Subscript):
            return
        key = subscript_key(target)
        if key not in MODEL_PAYLOAD_KEYS:
            return
        container_nodes = self.value_nodes(facts, scope, target.value)
        if not container_nodes:
            return
        value_sources = self.value_nodes(facts, scope, value)
        payload_sources = self.payload_nodes(facts, scope, value)
        if not value_sources and not payload_sources:
            return

        version_targets = self.target_payload_version_nodes(facts, scope, target, line_number)
        container_payloads = self.payload_nodes(facts, scope, target.value)
        for container_node in container_nodes:
            item = mapping_item_node(container_node, key)
            field_payload = payload_node(container_node, key)
            self.mapping_entries[container_node].add(item)
            self._add_node(
                item,
                kind="mapping_item",
                file_path=facts.rel_path,
                line_number=line_number,
                label=f"{expr_text(target.value)}[{key!r}]",
            )
            for source in value_sources:
                self._add_edge(source, item, "mapping_subscript_model_item", f"{expr_text(target)} = {expr_text(value)}")
                self._add_edge(source, field_payload, "mapping_subscript_model_payload", f"{expr_text(target)} carries model value")
                for container_payload in container_payloads:
                    self._add_edge(
                        source,
                        container_payload,
                        "mapping_subscript_container_model_payload",
                        f"{expr_text(target.value)} now carries model payload via {expr_text(target)}",
                    )
                for version_target in version_targets:
                    self._add_edge(
                        source,
                        version_target,
                        "mapping_subscript_model_payload_version",
                        f"{expr_text(target)} updates model payload before later sink",
                    )
            for payload_source in payload_sources:
                self._add_edge(
                    payload_source,
                    field_payload,
                    "mapping_subscript_nested_model_payload",
                    f"{expr_text(target)} receives model payload",
                )
                for container_payload in container_payloads:
                    self._add_edge(
                        payload_source,
                        container_payload,
                        "mapping_subscript_nested_container_model_payload",
                        f"{expr_text(target.value)} now carries nested model payload via {expr_text(target)}",
                    )
                for version_target in version_targets:
                    self._add_edge(
                        payload_source,
                        version_target,
                        "mapping_subscript_nested_model_payload_version",
                        f"{expr_text(target)} updates nested model payload before later sink",
                    )

    def _process_for_loop(self, facts: FileFacts, node: ast.For) -> None:
        scope = self.scope_for_node(facts, node)
        for iterable_source in self.value_nodes(facts, scope, node.iter):
            for target_node in self.target_value_nodes(facts, scope, node.target):
                self._add_edge(
                    iterable_source,
                    target_node,
                    "for_loop_value_unpack",
                    f"{target_text(node.target)} receives item value from {expr_text(node.iter)}",
                )
        iterable_payloads = self.payload_nodes(facts, scope, node.iter)
        if not iterable_payloads:
            return
        target_payloads = self.iteration_model_payload_targets(facts, scope, node.target)
        for payload_source in iterable_payloads:
            for target_payload in target_payloads:
                self._add_edge(
                    payload_source,
                    target_payload,
                    "for_loop_model_payload_unpack",
                    f"{target_text(node.target)} receives model-bearing item from {expr_text(node.iter)}",
                )

    def _process_return(self, facts: FileFacts, node: ast.Return) -> None:
        if node.value is None:
            return
        scope = self.scope_for_node(facts, node)
        if not scope.function_symbol:
            return
        ret = return_node(scope.function_symbol)
        for source in self.value_nodes(facts, scope, node.value):
            self._add_edge(source, ret, "return_value", f"return {expr_text(node.value)}")
        for payload_source in self.payload_nodes(facts, scope, node.value):
            function_return_payload = payload_node(scope.function_symbol, "__return__")
            synthetic_return_payload = payload_node(ret, "__return__")
            self._add_edge(payload_source, function_return_payload, "return_payload", "return model payload")
            if synthetic_return_payload != function_return_payload:
                self._add_edge(
                    payload_source,
                    synthetic_return_payload,
                    "return_payload_alias",
                    "return model payload via synthetic return node",
                )

    def _process_call_edges(self, facts: FileFacts, call: ast.Call) -> None:
        scope = self.scope_for_node(facts, call)

        # UI/widget dataflow: widget.addItems(models) and widget.currentText().
        self._process_ui_call(facts, scope, call)
        self._process_collection_mutation(facts, scope, call)

        for function_symbol in self.resolve_call_targets(facts, scope, call):
            info = self.repo_facts.functions.get(function_symbol)
            if not info:
                continue
            positional_params = list(info.params)
            arg_offset = 1 if info.class_symbol and info.params and info.params[0] in {"self", "cls"} else 0
            for index, arg in enumerate(call.args):
                param_index = index + arg_offset
                if param_index >= len(positional_params):
                    continue
                param = positional_params[param_index]
                self._connect_argument_to_param(facts, scope, arg, function_symbol, param, "call_arg_to_param")
            for keyword in call.keywords:
                if keyword.arg:
                    self._connect_argument_to_param(
                        facts,
                        scope,
                        keyword.value,
                        function_symbol,
                        keyword.arg,
                        "call_keyword_to_param",
                    )
                else:
                    if isinstance(call.func, ast.Subscript):
                        continue
                    for payload_source in self.payload_nodes(facts, scope, keyword.value):
                        self._add_edge(
                            payload_source,
                            payload_node(function_symbol, "**kwargs"),
                            "call_kwargs_payload_to_param",
                            f"**{expr_text(keyword.value)} model payload",
                        )
                        key = payload_key_from_node(payload_source)
                        if key and key in info.params and is_model_arg_name(key):
                            self._add_edge(
                                payload_source,
                                var_node(function_symbol, key),
                                "call_kwargs_payload_to_model_param",
                                f"**{expr_text(keyword.value)} supplies {function_symbol}.{key}",
                            )
                            self._add_edge(
                                payload_source,
                                payload_node(function_symbol, key),
                                "call_kwargs_payload_to_named_payload",
                                f"**{expr_text(keyword.value)} supplies model payload {function_symbol}.{key}",
                            )

    def _propagate_payload_providers(self) -> None:
        changed = True
        while changed:
            changed = False
            for source, target in self.graph.edges:
                providers = self.payload_provider_keys.get(source, set())
                if not providers:
                    continue
                before = len(self.payload_provider_keys[target])
                self.payload_provider_keys[target].update(providers)
                if len(self.payload_provider_keys[target]) != before:
                    changed = True

    def _register_dynamic_registry_kwargs_edges(self) -> None:
        for facts in self.repo_facts.files.values():
            if facts.tree is None:
                continue
            for call in facts.nodes:
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Subscript):
                    continue
                scope = self.scope_for_node(facts, call)
                for function_symbol in self.resolve_call_targets(facts, scope, call):
                    info = self.repo_facts.functions.get(function_symbol)
                    if not info:
                        continue
                    for keyword in call.keywords:
                        if keyword.arg is not None:
                            continue
                        for payload_source in self.payload_nodes(facts, scope, keyword.value):
                            if not self.dynamic_registry_target_allows_payload(
                                facts,
                                scope,
                                call.func,
                                function_symbol,
                                payload_source,
                            ):
                                continue
                            self._add_edge(
                                payload_source,
                                payload_node(function_symbol, "**kwargs"),
                                "dynamic_registry_kwargs_payload_to_param",
                                f"**{expr_text(keyword.value)} model payload reaches dynamic registry target {function_symbol}",
                            )
                            key = payload_key_from_node(payload_source)
                            target_params = []
                            if key and key in info.params and is_model_arg_name(key):
                                target_params = [key]
                            else:
                                target_params = single_model_like_param(info.params)
                            for target_param in target_params:
                                self._add_edge(
                                    payload_source,
                                    var_node(function_symbol, target_param),
                                    "dynamic_registry_kwargs_payload_to_model_param",
                                    f"**{expr_text(keyword.value)} supplies {function_symbol}.{target_param}",
                                )
                                self._add_edge(
                                    payload_source,
                                    payload_node(function_symbol, target_param),
                                    "dynamic_registry_kwargs_payload_to_named_payload",
                                    f"**{expr_text(keyword.value)} supplies model payload {function_symbol}.{target_param}",
                                )

    def _process_ui_call(self, facts: FileFacts, scope: Scope, call: ast.Call) -> None:
        if not isinstance(call.func, ast.Attribute):
            return
        attr = call.func.attr
        receiver = call.func.value
        widget = self.widget_node_for_expr(facts, scope, receiver)
        if not widget:
            return
        if attr in {"addItem", "addItems", "setCurrentText", "setText"}:
            for arg in call.args:
                for source in self.value_nodes(facts, scope, arg):
                    self._add_edge(source, widget, "ui_widget_value_flow", f"{expr_text(receiver)}.{attr}({expr_text(arg)})")
        elif attr == "currentText":
            call_node = call_result_node(facts.rel_path, call.lineno, call.col_offset)
            self._add_edge(widget, call_node, "ui_widget_current_text", f"{expr_text(receiver)}.currentText()")

    def _process_collection_mutation(self, facts: FileFacts, scope: Scope, call: ast.Call) -> None:
        if not isinstance(call.func, ast.Attribute):
            return
        if call.func.attr not in {"append", "add", "extend", "update"}:
            return
        receiver_payloads = self.payload_nodes(facts, scope, call.func.value)
        if not receiver_payloads:
            return
        for arg in call.args:
            for payload_source in self.payload_nodes(facts, scope, arg):
                for receiver_payload in receiver_payloads:
                    self._add_edge(
                        payload_source,
                        receiver_payload,
                        f"collection_{call.func.attr}_model_payload",
                        f"{expr_text(call.func.value)}.{call.func.attr}({expr_text(arg)}) keeps model payload",
                    )

    def _connect_argument_to_param(
        self,
        facts: FileFacts,
        scope: Scope,
        arg: ast.AST,
        function_symbol: str,
        param: str,
        edge_type: str,
    ) -> None:
        target = var_node(function_symbol, param)
        arg_value_nodes = self.value_nodes(facts, scope, arg)
        for source in arg_value_nodes:
            self._add_edge(source, target, edge_type, f"{expr_text(arg)} -> {function_symbol}.{param}")
        self._copy_model_field_payloads(
            arg_value_nodes,
            {target},
            f"{edge_type}_model_field_payload",
            f"{expr_text(arg)} model-bearing fields -> {function_symbol}.{param}",
        )
        for payload_source in self.payload_nodes(facts, scope, arg):
            self._add_edge(
                payload_source,
                payload_node(function_symbol, param),
                f"{edge_type}_payload",
                f"{expr_text(arg)} model payload -> {function_symbol}.{param}",
            )

    def _register_loader_sinks(self) -> None:
        for row in self.loader_rows:
            rel_path = row.get("file_path", "")
            facts = self.repo_facts.files.get(rel_path)
            if not facts or facts.tree is None:
                continue
            call = find_call_at_line(facts, parse_int(row.get("line_number")), row.get("visible_call_chain", ""))
            if call is None:
                continue
            scope = self.scope_for_node(facts, call)
            if self.is_generic_http_loader(row) and not self.http_call_has_model_endpoint_evidence(facts, scope, call):
                continue
            call_node = call_result_node(rel_path, call.lineno, call.col_offset)
            sink = sink_node(row.get("loader_candidate_id", ""), rel_path, parse_int(row.get("line_number")))
            self._add_node(sink, kind="loader_sink", file_path=rel_path, line_number=parse_int(row.get("line_number")), label=row.get("visible_call_chain", ""))
            self._add_node(call_node, kind="loader_call", file_path=rel_path, line_number=call.lineno, label=row.get("visible_call_chain", ""))

            sink_sources: set[str] = set()
            for keyword in call.keywords:
                if keyword.arg in MODEL_ARG_NAMES:
                    sink_sources.update(self.value_nodes(facts, scope, keyword.value))
                    sink_sources.update(self.payload_nodes(facts, scope, keyword.value))
                elif is_config_arg_name(keyword.arg):
                    sink_sources.update(self.payload_nodes_for_sink_arg(facts, scope, keyword.value, call.lineno))
                elif keyword.arg in REQUEST_BODY_ARG_NAMES:
                    sink_sources.update(self.payload_nodes_for_sink_arg(facts, scope, keyword.value, call.lineno))
                elif keyword.arg is None:
                    sink_sources.update(self.payload_nodes_for_sink_arg(facts, scope, keyword.value, call.lineno))
            for arg in call.args:
                if not expr_looks_like_config_arg(arg):
                    continue
                sink_sources.update(self.payload_nodes_for_sink_arg(facts, scope, arg, call.lineno))
            if not sink_sources and parse_bool(row.get("has_model_arg")):
                for arg in call.args:
                    sink_sources.update(self.value_nodes(facts, scope, arg))
                    sink_sources.update(self.payload_nodes(facts, scope, arg))
            for source in sink_sources:
                self._add_edge(source, sink, "loader_model_sink", f"{row.get('visible_call_chain', '')} receives model value")
            self.sinks[row.get("loader_candidate_id", "")] = SinkInfo(
                loader_row=row,
                sink_node_id=sink,
                call_node_id=call_node,
                mocked_reason=mocked_loader_reason(facts, call, row, self.identity_vocab, self.mock_patch_index),
            )
            self.sink_locations.add((rel_path, parse_int(row.get("line_number"))))
        if self.enable_jedi:
            self._register_jedi_internal_loader_sinks()
        # binding tracing must not invent confirming loader endpoints. Real reuse is only
        # confirmed against call analysis import-origin-backed loader candidates.
        self._register_adapter_config_registry_edges()

    def _augment_repo_with_jedi_symbols(self) -> None:
        try:
            import jedi  # type: ignore
        except Exception as exc:
            self.jedi_error = f"jedi_unavailable:{type(exc).__name__}:{exc}"
            return

        project = analysis_project(self.repo_facts.repo, jedi_sys_paths(self.repo_facts.repo))
        for facts in list(self.repo_facts.files.values()):
            if facts.tree is None or facts.rel_path not in self.jedi_probe_windows:
                continue
            try:
                script = jedi.Script(path=str(facts.full_path), project=project)
            except Exception:
                continue
            for node in facts.nodes:
                if not isinstance(node, ast.Call):
                    continue
                if not self._jedi_should_probe_call(facts, node):
                    continue
                resolved = self._jedi_resolve_call(script, facts, node)
                if not resolved:
                    continue
                key = callable_node_key(facts, node.func)
                self.jedi_callable_symbols[key].update(resolved)
                for symbol in resolved:
                    self._add_edge(
                        f"callable|{facts.rel_path}|{node.lineno}|{node.col_offset}|{expr_text(node.func)}",
                        f"symbol|{symbol}",
                        "jedi_symbol_resolves_to",
                        f"Jedi resolves {expr_text(node.func)} to {symbol}",
                    )
                    self.jedi_resolution_edges_added += 1

    def _build_jedi_probe_windows(self) -> dict[str, list[tuple[int, int]]]:
        windows: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for row in self.model_rows:
            rel_path = row.get("file_path", "")
            line = parse_int(row.get("line_number"))
            if rel_path and line:
                windows[rel_path].append((max(1, line - 120), line + 160))
        for row in self.loader_rows:
            rel_path = row.get("file_path", "")
            line = parse_int(row.get("line_number"))
            if rel_path and line:
                windows[rel_path].append((max(1, line - 30), line + 30))
        return dict(windows)

    def _jedi_should_probe_call(self, facts: FileFacts, call: ast.Call) -> bool:
        line = getattr(call, "lineno", 0)
        return any(start <= line <= end for start, end in self.jedi_probe_windows.get(facts.rel_path, []))

    def _jedi_resolve_call(self, script: Any, facts: FileFacts, call: ast.Call) -> set[str]:
        if not isinstance(call.func, (ast.Name, ast.Attribute)):
            return set()
        line_number = getattr(call.func, "lineno", 0)
        column = jedi_lookup_column(facts, call.func)
        if not line_number or column < 0:
            return set()
        try:
            definitions = script.infer(line_number, column)
        except Exception:
            return set()
        symbols: set[str] = set()
        for definition in definitions:
            full_name = getattr(definition, "full_name", None)
            module_path = getattr(definition, "module_path", None)
            definition_type = getattr(definition, "type", "")
            if not full_name or not module_path:
                continue
            path = Path(str(module_path))
            try:
                rel_path = path.resolve().relative_to(self.repo_facts.repo).as_posix()
            except ValueError:
                continue
            module = full_name.rsplit(".", 1)[0]
            self._ensure_jedi_file_facts(path, rel_path, module)
            if definition_type == "class" and full_name in self.repo_facts.classes:
                symbols.add(full_name)
            elif definition_type == "function" and full_name in self.repo_facts.functions:
                symbols.add(full_name)
        return symbols

    def _ensure_jedi_file_facts(self, path: Path, rel_path: str, module: str) -> None:
        if rel_path in self.repo_facts.files:
            return
        facts = parse_file(path, rel_path, module)
        facts.imports = extract_imports(facts, self.repo_facts.module_index)
        facts.classes = extract_classes(facts)
        self.repo_facts.files[rel_path] = facts
        self.repo_facts.path_to_module[rel_path] = module
        self.repo_facts.module_index[module] = rel_path
        self.jedi_added_rel_paths.add(rel_path)
        for cls in facts.classes:
            self.repo_facts.classes[cls.symbol] = cls
            self.repo_facts.classes_by_module_name[(cls.module, cls.name)] = cls.symbol
        facts.functions = extract_functions(facts, self.repo_facts.classes_by_module_name)
        for fn in facts.functions:
            self.repo_facts.functions[fn.symbol] = fn
            self.repo_facts.functions_by_module_name.setdefault((fn.module, fn.name), []).append(fn.symbol)

    def _register_jedi_internal_loader_sinks(self) -> None:
        if not self.loader_rules:
            return
        suffix_rules: dict[str, list[BindingLoaderRule]] = defaultdict(list)
        terminal_rules: dict[str, list[BindingLoaderRule]] = defaultdict(list)
        for rule in self.loader_rules:
            if rule.chain_suffix:
                suffix_rules[rule.chain_suffix].append(rule)
            if rule.terminal_call:
                terminal_rules[rule.terminal_call].append(rule)

        for rel_path in sorted(self.jedi_added_rel_paths):
            facts = self.repo_facts.files.get(rel_path)
            if not facts or facts.tree is None:
                continue
            imports = external_import_origins(facts, {rule.import_origin for rule in self.loader_rules})
            if not imports:
                continue
            for node in facts.nodes:
                if not isinstance(node, ast.Call):
                    continue
                visible_chain = call_chain(node.func)
                if not visible_chain:
                    continue
                terminal = visible_chain.rsplit(".", 1)[-1]
                matched_rules = binding_rules_for_chain(visible_chain, suffix_rules) or terminal_rules.get(terminal, [])
                if not matched_rules:
                    continue
                matched_origin = first_matching_provider_origin(facts, node, visible_chain, matched_rules, imports)
                if not matched_origin:
                    continue
                scope = self.scope_for_node(facts, node)
                sink_sources: set[str] = set()
                for keyword in node.keywords:
                    if keyword.arg and is_model_arg_name(keyword.arg):
                        sink_sources.update(self.value_nodes(facts, scope, keyword.value))
                        sink_sources.update(self.payload_nodes(facts, scope, keyword.value))
                if not sink_sources:
                    continue
                self.jedi_internal_sinks_added += 1
                candidate_id = f"loader_candidate_jedi_{self.jedi_internal_sinks_added:05d}"
                sink = sink_node(candidate_id, rel_path, node.lineno)
                call_node = call_result_node(rel_path, node.lineno, node.col_offset)
                matched_suffixes = unique(rule.chain_suffix for rule in matched_rules)
                matched_loaders = unique(rule.model_loader for rule in matched_rules)
                row = {
                    "loader_candidate_id": candidate_id,
                    "file_path": rel_path,
                    "line_number": str(node.lineno),
                    "location_url": "",
                    "visible_call_chain": visible_chain,
                    "terminal_call": terminal,
                    "matched_chain_suffix": "|".join(matched_suffixes),
                    "matched_canonical_loader": "|".join(matched_loaders),
                    "linked_import_origin": matched_origin,
                    "receiver_symbol": receiver_chain_from_visible_chain(visible_chain),
                    "receiver_origin": matched_origin,
                    "origin_resolution_method": "jedi_resolved_internal_provider_origin",
                    "origin_resolution_evidence": "resolved implementation file imports provider and assigns provider client",
                    "has_model_arg": "True",
                    "has_model_payload": "False",
                    "binding_eligible": True,
                    "line_text": line_at(facts.lines, node.lineno),
                }
                self._add_node(sink, kind="loader_sink", file_path=rel_path, line_number=node.lineno, label=visible_chain)
                self._add_node(call_node, kind="loader_call", file_path=rel_path, line_number=node.lineno, label=visible_chain)
                for source in sink_sources:
                    self._add_edge(
                        source,
                        sink,
                        "jedi_internal_loader_model_sink",
                        f"{visible_chain} receives model value in Jedi-resolved implementation",
                    )
                self.sinks[candidate_id] = SinkInfo(
                    loader_row=row,
                    sink_node_id=sink,
                    call_node_id=call_node,
                    mocked_reason=mocked_loader_reason(facts, node, row, self.identity_vocab, self.mock_patch_index),
                )
                self.sink_locations.add((rel_path, node.lineno))

    def _register_adapter_config_registry_edges(self) -> None:
        for facts in self.repo_facts.files.values():
            if facts.tree is None:
                continue
            registry_classes = self.config_linked_model_registry_classes(facts)
            if not registry_classes:
                continue
            sinks = [
                sink
                for sink in self.sinks.values()
                if sink.loader_row.get("file_path", "") == facts.rel_path
                and self.sink_reads_config_model_field(facts, sink)
            ]
            if not sinks:
                continue
            for source in self.sources.values():
                row = source.row
                if row.get("file_path", "") != facts.rel_path:
                    continue
                if row.get("class_context", "") not in registry_classes:
                    continue
                if row.get("ast_context", "") not in {"assignment_value", "class_attribute", "collection_item"}:
                    continue
                for sink in sinks:
                    self._add_edge(
                        source.node_id,
                        sink.sink_node_id,
                        "adapter_config_registry_model_sink",
                        (
                            f"{row.get('class_context', '')} model registry feeds a config model field "
                            f"read by {sink.loader_row.get('visible_call_chain', '')}"
                        ),
                    )

    def config_linked_model_registry_classes(self, facts: FileFacts) -> set[str]:
        registry_names = {
            info.name
            for info in facts.classes
            if class_looks_like_model_registry(info)
        }
        if not registry_names:
            return set()
        linked: set[str] = set()
        for info in facts.classes:
            if not class_looks_like_config(info):
                continue
            for node in info.node.body:
                target = None
                annotation = None
                value = None
                if isinstance(node, ast.AnnAssign):
                    target = node.target
                    annotation = node.annotation
                    value = node.value
                elif isinstance(node, ast.Assign):
                    value = node.value
                    target = node.targets[0] if node.targets else None
                if not is_model_arg_name(expr_text(target)):
                    continue
                reference_text = f"{expr_text(annotation)} {expr_text(value)}"
                for registry_name in registry_names:
                    if registry_name in reference_text:
                        linked.add(registry_name)
        return linked

    def sink_reads_config_model_field(self, facts: FileFacts, sink: SinkInfo) -> bool:
        call = find_call_at_line(
            facts,
            parse_int(sink.loader_row.get("line_number")),
            sink.loader_row.get("visible_call_chain", ""),
        )
        if call is None:
            return False
        for keyword in call.keywords:
            if keyword.arg and is_model_arg_name(keyword.arg) and expr_reads_config_model_field(keyword.value):
                return True
        return False

    def callable_labels(self, facts: FileFacts, scope: Scope, call: ast.Call) -> set[str]:
        labels = {call_chain(call.func)}
        if isinstance(call.func, ast.Name):
            labels.add(call.func.id)
            ref = facts.imports.get(call.func.id)
            if ref:
                labels.add(ref.module)
                if ref.symbol:
                    labels.add(ref.symbol)
                    labels.add(f"{ref.module}.{ref.symbol}")
        elif isinstance(call.func, ast.Attribute):
            labels.add(call.func.attr)
            labels.add(expr_text(call.func))
        for symbol in self.resolve_callable_symbols(facts, scope, call.func):
            labels.add(symbol)
            labels.add(symbol.split(".")[-1])
            if symbol in self.repo_facts.classes:
                labels.add(self.repo_facts.classes[symbol].name)
            if symbol in self.repo_facts.functions:
                labels.add(self.repo_facts.functions[symbol].name)
                if self.repo_facts.functions[symbol].class_name:
                    labels.add(self.repo_facts.functions[symbol].class_name)
        return {label for label in labels if label}

    def same_class_property_return_node(self, class_symbol: str, attr: str) -> str:
        """Return the synthetic return node for a simple same-class property."""
        method = f"{class_symbol}.{attr}"
        info = self.repo_facts.functions.get(method)
        if not info or not is_property_function(info.node):
            return ""
        return return_node(method)

    def value_nodes(self, facts: FileFacts, scope: Scope, expr: ast.AST | None) -> set[str]:
        if expr is None:
            return set()
        if isinstance(expr, ast.Constant):
            if isinstance(expr.value, str):
                node = literal_node(facts.rel_path, expr.lineno, expr.col_offset, expr.value)
                self._add_node(node, kind="literal", file_path=facts.rel_path, line_number=expr.lineno, label=expr.value)
                return {node}
            return set()
        if isinstance(expr, ast.Name):
            nodes = {var_node(scope.scope_id, expr.id)}
            if scope.function_symbol and not self.name_is_local(facts, scope, expr.id):
                if (facts.module, expr.id) in self.module_vars or expr.id in facts.imports:
                    nodes.add(var_node(facts.module, expr.id))
            for node in nodes:
                self._add_node(node, kind="variable", file_path=facts.rel_path, line_number=getattr(expr, "lineno", 0), label=expr.id)
            return nodes
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in {"self", "cls"} and scope.class_symbol:
                node = field_node(scope.class_symbol, expr.attr)
                self._add_node(node, kind="class_field", file_path=facts.rel_path, line_number=expr.lineno, label=f"self.{expr.attr}")
                property_return = self.same_class_property_return_node(scope.class_symbol, expr.attr)
                if property_return:
                    self._add_edge(
                        property_return,
                        node,
                        "same_class_property_value",
                        f"{expr_text(expr)} reads property return {expr.attr}",
                    )
                    self._add_edge(
                        payload_node(property_return, "__return__"),
                        payload_node(scope.class_symbol, f"self.{expr.attr}"),
                        "same_class_property_payload",
                        f"{expr_text(expr)} reads model payload from property {expr.attr}",
                    )
                return {node}
            class_field_nodes = {
                field_node(class_symbol, expr.attr)
                for class_symbol in self.class_symbols_for_expr(facts, scope, expr.value)
            }
            for node in class_field_nodes:
                self._add_node(
                    node,
                    kind="class_field",
                    file_path=facts.rel_path,
                    line_number=expr.lineno,
                    label=expr_text(expr),
                )
            base_nodes = self.value_nodes(facts, scope, expr.value)
            nodes = {attr_node(n, expr.attr) for n in base_nodes}
            for node in nodes:
                self._add_node(node, kind="attribute", file_path=facts.rel_path, line_number=expr.lineno, label=expr_text(expr))
            for field in class_field_nodes:
                for node in nodes:
                    self._add_edge(
                        field,
                        node,
                        "class_attribute_reference",
                        f"{expr_text(expr)} reads class attribute {expr.attr}",
                    )
            nodes.update(class_field_nodes)
            if is_model_arg_name(expr.attr):
                for base_node in base_nodes:
                    node = attr_node(base_node, expr.attr)
                    self._add_edge(
                        payload_node(base_node, expr.attr),
                        node,
                        "scoped_config_attribute_value",
                        f"{expr_text(expr)} reads scoped config field {expr.attr}",
                    )
            return nodes
        if isinstance(expr, ast.Call):
            result_node = call_result_node(facts.rel_path, expr.lineno, expr.col_offset)
            nodes = {result_node}
            self._add_node(result_node, kind="call_result", file_path=facts.rel_path, line_number=expr.lineno, label=expr_text(expr))
            for ret in self.call_return_nodes(facts, scope, expr):
                self._add_edge(ret, result_node, "call_return_value", f"{expr_text(expr)} return")
            if isinstance(expr.func, ast.Attribute):
                attr = expr.func.attr
                if attr in VALUE_PRESERVING_METHODS:
                    for receiver_source in self.value_nodes(facts, scope, expr.func.value):
                        self._add_edge(
                            receiver_source,
                            result_node,
                            "value_preserving_method_call",
                            f"{expr_text(expr.func.value)}.{attr}(...)",
                        )
                if attr == "get" and len(expr.args) >= 2:
                    key = literal_string(expr.args[0])
                    receiver_nodes = self.value_nodes(facts, scope, expr.func.value)
                    for receiver_node in receiver_nodes:
                        if key:
                            item = mapping_item_node(receiver_node, key)
                            if item in self.graph:
                                self._add_edge(
                                    item,
                                    result_node,
                                    "mapping_get_literal_key_value",
                                    f"{expr_text(expr.func.value)}.get({key!r})",
                                )
                        else:
                            for item in self.mapping_entries.get(receiver_node, set()):
                                self._add_edge(
                                    item,
                                    result_node,
                                    "mapping_get_dynamic_key_value",
                                    f"{expr_text(expr.func.value)}.get({expr_text(expr.args[0])}) possible value",
                                )
                    for default_source in self.value_nodes(facts, scope, expr.args[1]):
                        self._add_edge(
                            default_source,
                            result_node,
                            "mapping_get_default_value",
                            f"{expr_text(expr.func.value)}.get(..., {expr_text(expr.args[1])}) default",
                        )
                    for payload_source in self.payload_nodes(facts, scope, expr.args[1]):
                        self._add_edge(
                            payload_source,
                            payload_node(result_node, "__return__"),
                            "mapping_get_default_payload",
                            f"{expr_text(expr.func.value)}.get(...) default model payload",
                        )
            if call_chain(expr.func).endswith("getenv") and len(expr.args) >= 2:
                for default_source in self.value_nodes(facts, scope, expr.args[1]):
                    self._add_edge(
                        default_source,
                        result_node,
                        "env_get_default_value",
                        f"{call_chain(expr.func)}(..., {expr_text(expr.args[1])}) default",
                    )
            # UI currentText() is connected in _process_ui_call; expose its call node here too.
            if isinstance(expr.func, ast.Attribute) and expr.func.attr == "currentText":
                widget = self.widget_node_for_expr(facts, scope, expr.func.value)
                if widget:
                    self._add_edge(widget, result_node, "ui_widget_current_text", f"{expr_text(expr.func.value)}.currentText()")
            if call_chain(expr.func).endswith("json.dumps") and expr.args:
                for payload_source in self.payload_nodes(facts, scope, expr.args[0]):
                    self._add_edge(
                        payload_source,
                        result_node,
                        "json_serialized_model_payload",
                        f"json.dumps({expr_text(expr.args[0])}) preserves model payload",
                    )
            if call_chain(expr.func) == "getattr" and len(expr.args) >= 2:
                for key in self.getattr_field_names(facts, scope, expr.args[1]):
                    if not is_model_arg_name(key):
                        continue
                    for base_node in self.value_nodes(facts, scope, expr.args[0]):
                        self._add_edge(
                            payload_node(base_node, key),
                            result_node,
                            "scoped_getattr_config_field",
                            f"getattr({expr_text(expr.args[0])}, {key!r}) reads scoped config field {key}",
                        )
            return nodes
        if isinstance(expr, ast.JoinedStr):
            nodes: set[str] = set()
            for value in expr.values:
                if isinstance(value, ast.FormattedValue):
                    nodes.update(self.value_nodes(facts, scope, value.value))
                elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                    node = literal_node(facts.rel_path, value.lineno, value.col_offset, value.value)
                    self._add_node(node, kind="literal", file_path=facts.rel_path, line_number=value.lineno, label=value.value)
                    nodes.add(node)
            return nodes
        if isinstance(expr, ast.Dict):
            nodes: set[str] = set()
            for value in expr.values:
                nodes.update(self.value_nodes(facts, scope, value))
            return nodes
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            nodes: set[str] = set()
            for item in expr.elts:
                nodes.update(self.value_nodes(facts, scope, item))
            return nodes
        if isinstance(expr, ast.Subscript):
            nodes = self.payload_nodes(facts, scope, expr)
            if nodes:
                return nodes
            return self.value_nodes(facts, scope, expr.value)
        if isinstance(expr, ast.BoolOp):
            nodes: set[str] = set()
            for value in expr.values:
                nodes.update(self.value_nodes(facts, scope, value))
            return nodes
        if isinstance(expr, ast.IfExp):
            return self.value_nodes(facts, scope, expr.body) | self.value_nodes(facts, scope, expr.orelse)
        if isinstance(expr, ast.UnaryOp):
            return self.value_nodes(facts, scope, expr.operand)
        if isinstance(expr, ast.BinOp):
            return self.value_nodes(facts, scope, expr.left) | self.value_nodes(facts, scope, expr.right)
        return set()

    def _register_mapping_entries(
        self,
        facts: FileFacts,
        scope: Scope,
        target_node: str,
        expr: ast.Dict,
    ) -> None:
        for key_node, value_node in zip(expr.keys, expr.values):
            key = literal_key(key_node)
            if not key:
                continue
            item = mapping_item_node(target_node, key)
            self.mapping_entries[target_node].add(item)
            self._add_node(
                item,
                kind="mapping_item",
                file_path=facts.rel_path,
                line_number=getattr(value_node, "lineno", 0),
                label=f"{target_node}[{key!r}]",
            )
            for source in self.value_nodes(facts, scope, value_node):
                self._add_edge(
                    source,
                    item,
                    "mapping_literal_item",
                    f"{target_node}[{key!r}] = {expr_text(value_node)}",
                )

    def payload_nodes(self, facts: FileFacts, scope: Scope, expr: ast.AST | None) -> set[str]:
        if expr is None:
            return set()
        if isinstance(expr, ast.Dict):
            nodes: set[str] = set()
            provider_key = self.provider_key_from_dict(expr)
            for key, value in zip(expr.keys, expr.values):
                key_value = literal_key(key)
                if key_value in MODEL_PAYLOAD_KEYS:
                    for source in self.value_nodes(facts, scope, value):
                        payload = payload_literal_node(facts.rel_path, value.lineno, getattr(value, "col_offset", 0), key_value)
                        self.note_payload_provider(payload, provider_key)
                        self._add_edge(source, payload, "dict_model_payload", f"dict[{key_value!r}] = {expr_text(value)}")
                        nodes.add(payload)
                else:
                    child_nodes = self.payload_nodes(facts, scope, value)
                    for child_node in child_nodes:
                        self.note_payload_provider(child_node, provider_key)
                    nodes.update(child_nodes)
            return nodes
        if isinstance(expr, ast.Tuple):
            provider_key = self.provider_key_from_tuple(expr)
            nodes: set[str] = set()
            for item in expr.elts:
                child_nodes = self.payload_nodes(facts, scope, item)
                for child_node in child_nodes:
                    self.note_payload_provider(child_node, provider_key)
                nodes.update(child_nodes)
            return nodes
        if isinstance(expr, ast.Name):
            return {payload_node(scope.scope_id, expr.id)}
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in {"self", "cls"} and scope.class_symbol:
                return {payload_node(scope.class_symbol, f"self.{expr.attr}")}
            return {payload_node(node, expr.attr) for node in self.value_nodes(facts, scope, expr.value)}
        if isinstance(expr, ast.Subscript):
            key_value = subscript_key(expr)
            if key_value in MODEL_PAYLOAD_KEYS:
                return self.value_nodes(facts, scope, expr.value)
            return self.payload_nodes(facts, scope, expr.value)
        if isinstance(expr, ast.Call):
            result_node = call_result_node(facts.rel_path, expr.lineno, expr.col_offset)
            nodes: set[str] = set()
            if isinstance(expr.func, ast.Attribute) and expr.func.attr in VALUE_PRESERVING_METHODS:
                for payload_source in self.payload_nodes(facts, scope, expr.func.value):
                    self._add_edge(
                        payload_source,
                        payload_node(result_node, "__return__"),
                        "payload_preserving_method_call",
                        f"{expr_text(expr.func.value)}.{expr.func.attr}(...) keeps model payload",
                    )
                    nodes.add(payload_node(result_node, "__return__"))
            for keyword in expr.keywords:
                if keyword.arg is None:
                    for payload_source in self.payload_nodes(facts, scope, keyword.value):
                        payload = payload_node(result_node, "**kwargs")
                        self._add_edge(
                            payload_source,
                            payload,
                            "call_kwargs_payload",
                            f"{expr_text(expr)} receives **kwargs model payload",
                        )
                        nodes.add(payload)
                    continue
                if is_model_arg_name(keyword.arg):
                    payload = payload_node(result_node, keyword.arg)
                    for source in self.value_nodes(facts, scope, keyword.value):
                        self._add_edge(
                            source,
                            payload,
                            "call_model_keyword_payload",
                            f"{expr_text(expr)}.{keyword.arg} = {expr_text(keyword.value)}",
                        )
                    for payload_source in self.payload_nodes(facts, scope, keyword.value):
                        self._add_edge(
                            payload_source,
                            payload,
                            "call_model_keyword_nested_payload",
                            f"{expr_text(expr)}.{keyword.arg} receives model payload",
                        )
                    nodes.add(payload)
                elif is_config_arg_name(keyword.arg):
                    payload = payload_node(result_node, keyword.arg)
                    for payload_source in self.payload_nodes(facts, scope, keyword.value):
                        self._add_edge(
                            payload_source,
                            payload,
                            "call_config_keyword_payload",
                            f"{expr_text(expr)}.{keyword.arg} receives model-bearing config",
                        )
                        nodes.add(payload)
            if call_chain(expr.func).endswith("json.dumps") and expr.args:
                nodes.update(self.payload_nodes(facts, scope, expr.args[0]))
            for ret in self.call_return_nodes(facts, scope, expr):
                nodes.add(payload_node(ret, "__return__"))
            return nodes
        if isinstance(expr, (ast.BoolOp, ast.List, ast.Set)):
            children = expr.values if isinstance(expr, ast.BoolOp) else expr.elts
            nodes: set[str] = set()
            for child in children:
                nodes.update(self.payload_nodes(facts, scope, child))
            return nodes
        if isinstance(expr, ast.IfExp):
            return self.payload_nodes(facts, scope, expr.body) | self.payload_nodes(facts, scope, expr.orelse)
        return set()

    def provider_key_from_tuple(self, expr: ast.Tuple) -> str:
        if not expr.elts:
            return ""
        provider_key = literal_string(expr.elts[0])
        return provider_key if self.is_known_provider_key(provider_key) else ""

    def provider_key_from_dict(self, expr: ast.Dict) -> str:
        provider_keys = {"provider", "provider_name", "vendor", "vendor_name", "type", "kind"}
        for key, value in zip(expr.keys, expr.values):
            if literal_key(key) not in provider_keys:
                continue
            provider_key = literal_string(value)
            if self.is_known_provider_key(provider_key):
                return provider_key
        return ""

    def is_known_provider_key(self, value: str) -> bool:
        if not value:
            return False
        terms = identity_terms_from_text(value, add_compact=True)
        return bool(terms & set(self.identity_vocab.provider_terms))

    def note_payload_provider(self, node: str, provider_key: str) -> None:
        if node and provider_key:
            self.payload_provider_keys[node].add(provider_key)

    def target_value_nodes(self, facts: FileFacts, scope: Scope, target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            if scope.class_symbol and not scope.function_symbol:
                return {field_node(scope.class_symbol, target.id)}
            return {var_node(scope.scope_id, target.id)}
        if isinstance(target, ast.Attribute):
            if isinstance(target.value, ast.Name) and target.value.id in {"self", "cls"} and scope.class_symbol:
                return {field_node(scope.class_symbol, target.attr)}
            return {attr_node(node, target.attr) for node in self.value_nodes(facts, scope, target.value)}
        if isinstance(target, (ast.Tuple, ast.List)):
            nodes: set[str] = set()
            for item in target.elts:
                nodes.update(self.target_value_nodes(facts, scope, item))
            return nodes
        return set()

    def target_payload_nodes(self, facts: FileFacts, scope: Scope, target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {payload_node(scope.scope_id, target.id)}
        if isinstance(target, ast.Attribute):
            if isinstance(target.value, ast.Name) and target.value.id in {"self", "cls"} and scope.class_symbol:
                return {payload_node(scope.class_symbol, f"self.{target.attr}")}
        if isinstance(target, ast.Subscript) and subscript_key(target) in MODEL_PAYLOAD_KEYS:
            return {
                payload_node(container_node, subscript_key(target))
                for container_node in self.value_nodes(facts, scope, target.value)
            }
        return set()

    def target_payload_version_nodes(
        self,
        facts: FileFacts,
        scope: Scope,
        target: ast.AST,
        line_number: int,
    ) -> set[str]:
        key = self.payload_assignment_key(facts, scope, target)
        if not key:
            return set()
        scope_id, name = key
        node = payload_version_node(scope_id, name, line_number)
        self.payload_assignment_versions[key].append((line_number, node))
        return {node}

    def payload_nodes_for_sink_arg(
        self,
        facts: FileFacts,
        scope: Scope,
        expr: ast.AST,
        sink_line_number: int,
    ) -> set[str]:
        key = self.payload_read_key(facts, scope, expr)
        if key:
            version_node = self.latest_payload_version_before(key, sink_line_number)
            if version_node:
                return {version_node}
        return self.payload_nodes(facts, scope, expr)

    def is_generic_http_loader(self, row: dict[str, str]) -> bool:
        origins = split_pipe_values(row.get("linked_import_origin", "")) or split_pipe_values(
            row.get("receiver_origin", "")
        )
        return bool(origins) and origins <= GENERIC_HTTP_ORIGINS

    def http_call_has_model_endpoint_evidence(self, facts: FileFacts, scope: Scope, call: ast.Call) -> bool:
        for expr in self.http_url_argument_exprs(call):
            for value in self.string_values_for_expr(facts, scope, expr, getattr(call, "lineno", 0), set()):
                if url_text_has_model_endpoint(value, self.identity_vocab):
                    return True
        return url_text_has_model_endpoint(line_at(facts.lines, getattr(call, "lineno", 0)), self.identity_vocab)

    def http_url_argument_exprs(self, call: ast.Call) -> list[ast.AST]:
        exprs = [keyword.value for keyword in call.keywords if keyword.arg == "url"]
        chain = call_chain(call.func)
        terminal = chain.rsplit(".", 1)[-1] if chain else ""
        if terminal in {"request", "stream"} and len(call.args) >= 2:
            exprs.append(call.args[1])
        elif call.args:
            exprs.append(call.args[0])
        return exprs

    def string_values_for_expr(
        self,
        facts: FileFacts,
        scope: Scope,
        expr: ast.AST | None,
        before_line: int,
        seen: set[tuple[str, str]],
    ) -> set[str]:
        if expr is None:
            return set()
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.JoinedStr):
            return self.string_values_for_joined_str(facts, scope, expr, before_line, seen)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left_values = self.string_values_for_expr(facts, scope, expr.left, before_line, seen)
            right_values = self.string_values_for_expr(facts, scope, expr.right, before_line, seen)
            return {left + right for left in left_values for right in right_values}
        if isinstance(expr, ast.Name):
            return self.string_values_for_name(facts, scope, expr.id, before_line, seen)
        return {expr_text(expr)}

    def string_values_for_joined_str(
        self,
        facts: FileFacts,
        scope: Scope,
        expr: ast.JoinedStr,
        before_line: int,
        seen: set[tuple[str, str]],
    ) -> set[str]:
        values = {""}
        for part in expr.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                part_values = {part.value}
            elif isinstance(part, ast.FormattedValue):
                part_values = self.string_values_for_expr(facts, scope, part.value, before_line, seen)
            else:
                part_values = {expr_text(part)}
            values = {prefix + suffix for prefix in values for suffix in part_values}
        return values

    def string_values_for_name(
        self,
        facts: FileFacts,
        scope: Scope,
        name: str,
        before_line: int,
        seen: set[tuple[str, str]],
    ) -> set[str]:
        key = (scope.scope_id, name)
        if key in seen:
            return set()
        seen.add(key)
        values: set[str] = set()
        for node in facts.nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if getattr(node, "lineno", 0) > before_line:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(name in target_names(target) for target in targets):
                continue
            assignment_scope = self.scope_for_node(facts, node)
            if assignment_scope.scope_id not in {scope.scope_id, facts.module}:
                continue
            values.update(self.string_values_for_expr(facts, assignment_scope, node.value, node.lineno, seen))
        return values

    def payload_assignment_key(
        self,
        facts: FileFacts,
        scope: Scope,
        target: ast.AST,
    ) -> tuple[str, str] | None:
        if isinstance(target, ast.Name):
            return (scope.scope_id, target.id)
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in {"self", "cls"}
            and scope.class_symbol
        ):
            return (scope.class_symbol, f"self.{target.attr}")
        if isinstance(target, ast.Subscript) and subscript_key(target) in MODEL_PAYLOAD_KEYS:
            return self.payload_assignment_key(facts, scope, target.value)
        return None

    def payload_read_key(
        self,
        facts: FileFacts,
        scope: Scope,
        expr: ast.AST,
    ) -> tuple[str, str] | None:
        if isinstance(expr, ast.Name):
            return (scope.scope_id, expr.id)
        if (
            isinstance(expr, ast.Attribute)
            and isinstance(expr.value, ast.Name)
            and expr.value.id in {"self", "cls"}
            and scope.class_symbol
        ):
            return (scope.class_symbol, f"self.{expr.attr}")
        return None

    def latest_payload_version_before(
        self,
        key: tuple[str, str],
        sink_line_number: int,
    ) -> str:
        candidates = [
            (line_number, node)
            for line_number, node in self.payload_assignment_versions.get(key, [])
            if line_number <= sink_line_number
        ]
        if not candidates:
            return ""
        return max(candidates, key=lambda item: item[0])[1]

    def iteration_model_payload_targets(self, facts: FileFacts, scope: Scope, target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            if is_model_arg_name(target.id) or is_config_arg_name(target.id):
                return {payload_node(scope.scope_id, target.id)}
            return set()
        if isinstance(target, ast.Attribute):
            name = target.attr
            if is_model_arg_name(name) or is_config_arg_name(name):
                return self.target_payload_nodes(facts, scope, target)
            return set()
        if isinstance(target, (ast.Tuple, ast.List)):
            nodes: set[str] = set()
            for item in target.elts:
                nodes.update(self.iteration_model_payload_targets(facts, scope, item))
            return nodes
        return set()

    def class_registry_from_dict(
        self,
        facts: FileFacts,
        scope: Scope,
        expr: ast.AST | None,
    ) -> dict[str, set[str]]:
        if not isinstance(expr, ast.Dict):
            return {}
        registry: dict[str, set[str]] = defaultdict(set)
        for key, value in zip(expr.keys, expr.values):
            provider_key = literal_key(key)
            if not self.is_known_provider_key(provider_key):
                continue
            class_symbols = self.resolve_class_symbols_for_expr(facts, scope, value)
            if class_symbols:
                registry[provider_key].update(class_symbols)
        return dict(registry)

    def resolve_class_symbols_for_expr(
        self,
        facts: FileFacts,
        scope: Scope,
        expr: ast.AST,
    ) -> set[str]:
        symbols = self.resolve_callable_symbols(facts, scope, expr)
        return {symbol for symbol in symbols if symbol in self.repo_facts.classes}

    def class_symbols_for_expr(self, facts: FileFacts, scope: Scope, expr: ast.AST) -> set[str]:
        """Resolve project class objects used as attribute containers."""
        return {
            symbol
            for symbol in self.resolve_callable_symbols(facts, scope, expr)
            if symbol in self.repo_facts.classes
        }

    def call_return_nodes(self, facts: FileFacts, scope: Scope, call: ast.Call) -> set[str]:
        returns = {return_node(symbol) for symbol in self.resolve_call_targets(facts, scope, call)}
        return {node for node in returns if node}

    def resolve_constructor(self, facts: FileFacts, scope: Scope, call: ast.Call) -> set[str]:
        targets = self.resolve_callable_symbols(facts, scope, call.func)
        classes = set()
        for target in targets:
            if target in self.repo_facts.classes:
                classes.add(target)
        return classes

    def resolve_call_targets(self, facts: FileFacts, scope: Scope, call: ast.Call) -> set[str]:
        symbols = set()
        for symbol in self.resolve_callable_symbols(facts, scope, call.func):
            if symbol in self.repo_facts.functions:
                symbols.add(symbol)
            elif symbol in self.repo_facts.classes:
                init_symbol = f"{symbol}.__init__"
                if init_symbol in self.repo_facts.functions:
                    symbols.add(init_symbol)
        return symbols

    def resolve_callable_symbols(self, facts: FileFacts, scope: Scope, func: ast.AST) -> set[str]:
        jedi_symbols = set(self.jedi_callable_symbols.get(callable_node_key(facts, func), set()))
        if isinstance(func, ast.Name):
            output = set(jedi_symbols)
            output.update(self.repo_facts.functions_by_module_name.get((facts.module, func.id), []))
            cls = self.repo_facts.classes_by_module_name.get((facts.module, func.id))
            if cls:
                output.add(cls)
            ref = facts.imports.get(func.id)
            if ref and ref.is_project:
                if ref.symbol:
                    output.update(self.resolve_project_symbol(ref.module, ref.symbol))
                    output.add(var_node(ref.module, ref.symbol))
                else:
                    output.add(module_node(ref.module))
            return output
        if isinstance(func, ast.Subscript):
            output = set()
            key = subscript_key(func)
            for registry_node in self.value_nodes(facts, scope, func.value):
                registry = self.class_registries.get(registry_node, {})
                if key:
                    output.update(registry.get(key, set()))
                else:
                    for class_symbols in registry.values():
                        output.update(class_symbols)
            return output
        if isinstance(func, ast.Attribute):
            output = set(jedi_symbols)
            if (
                isinstance(func.value, ast.Call)
                and call_chain(func.value.func) == "super"
                and scope.class_symbol
            ):
                class_info = self.repo_facts.classes.get(scope.class_symbol)
                if class_info:
                    for base in class_info.bases:
                        method = f"{base}.{func.attr}"
                        if method in self.repo_facts.functions:
                            output.add(method)
                return output
            if isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"} and scope.class_symbol:
                method = f"{scope.class_symbol}.{func.attr}"
                if method in self.repo_facts.functions:
                    output.add(method)
                return output
            if isinstance(func.value, ast.Name):
                name = func.value.id
                ref = facts.imports.get(name)
                if ref and ref.is_project and not ref.symbol:
                    output.update(self.repo_facts.functions_by_module_name.get((ref.module, func.attr), []))
                    cls = self.repo_facts.classes_by_module_name.get((ref.module, func.attr))
                    if cls:
                        output.add(cls)
                    return output
                object_node = var_node(scope.scope_id, name)
                for class_symbol in self.object_types.get(object_node, set()):
                    method = f"{class_symbol}.{func.attr}"
                    if method in self.repo_facts.functions:
                        output.add(method)
                return output
            receiver_nodes = self.value_nodes(facts, scope, func.value)
            for receiver_node in receiver_nodes:
                for class_symbol in self.object_types.get(receiver_node, set()):
                    method = f"{class_symbol}.{func.attr}"
                    if method in self.repo_facts.functions:
                        output.add(method)
            return output
        return set()

    def resolve_project_symbol(
        self,
        module: str,
        symbol: str,
        seen: set[tuple[str, str]] | None = None,
    ) -> set[str]:
        seen = seen or set()
        key = (module, symbol)
        if key in seen:
            return set()
        seen.add(key)

        output = set(self.repo_facts.functions_by_module_name.get(key, []))
        cls = self.repo_facts.classes_by_module_name.get(key)
        if cls:
            output.add(cls)
        if output:
            return output

        rel_path = self.repo_facts.module_index.get(module, "")
        facts = self.repo_facts.files.get(rel_path)
        if not facts:
            return set()
        ref = facts.imports.get(symbol)
        if ref and ref.is_project and ref.symbol:
            return self.resolve_project_symbol(ref.module, ref.symbol, seen)
        return set()

    def dynamic_registry_target_allows_payload(
        self,
        facts: FileFacts,
        scope: Scope,
        func: ast.AST,
        function_symbol: str,
        payload_source: str,
    ) -> bool:
        if not isinstance(func, ast.Subscript):
            return True
        provider_keys = self.payload_provider_keys.get(payload_source, set())
        if not provider_keys:
            return True
        class_symbol = function_symbol.removesuffix(".__init__")
        if class_symbol == function_symbol:
            return True
        for registry_node in self.value_nodes(facts, scope, func.value):
            registry = self.class_registries.get(registry_node, {})
            for provider_key in provider_keys:
                if class_symbol in registry.get(provider_key, set()):
                    return True
        return False

    def scope_for_node(self, facts: FileFacts, node: ast.AST) -> Scope:
        function_info: FunctionDefInfo | None = None
        class_info: ClassDefInfo | None = None
        cursor: ast.AST | None = node
        while cursor in facts.parents:
            cursor = facts.parents[cursor]
            if function_info is None and isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_info = self.function_info_for_node(facts, cursor)
            if class_info is None and isinstance(cursor, ast.ClassDef):
                class_info = self.class_info_for_node(facts, cursor)
        if function_info:
            return Scope(
                module=facts.module,
                rel_path=facts.rel_path,
                scope_id=function_info.symbol,
                function_symbol=function_info.symbol,
                class_symbol=function_info.class_symbol,
                function_name=function_info.name,
                class_name=function_info.class_name,
            )
        return Scope(
            module=facts.module,
            rel_path=facts.rel_path,
            scope_id=facts.module,
            class_symbol=class_info.symbol if class_info else "",
            class_name=class_info.name if class_info else "",
        )

    def function_info_for_node(self, facts: FileFacts, node: ast.AST) -> FunctionDefInfo | None:
        for info in facts.functions:
            if info.node is node:
                return info
        return None

    def class_info_for_node(self, facts: FileFacts, node: ast.AST) -> ClassDefInfo | None:
        for info in facts.classes:
            if info.node is node:
                return info
        return None

    def widget_node_for_expr(self, facts: FileFacts, scope: Scope, expr: ast.AST) -> str:
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id in {"self", "cls"} and scope.class_symbol:
            return widget_node(scope.class_symbol, expr.attr)
        return ""

    def getattr_field_names(self, facts: FileFacts, scope: Scope, expr: ast.AST) -> set[str]:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.Name) and scope.function_symbol:
            info = self.repo_facts.functions.get(scope.function_symbol)
            if info and info.node is not None:
                return literal_annotation_values(info.node, expr.id)
        return set()

    def name_is_local(self, facts: FileFacts, scope: Scope, name: str) -> bool:
        if not scope.function_symbol:
            return True
        info = self.repo_facts.functions.get(scope.function_symbol)
        if info and name in info.params:
            return True
        function_node = info.node if info else None
        if function_node is None:
            return True
        for node in ast.walk(function_node):
            if node is function_node:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if name in target_names(target):
                        return True
            elif isinstance(node, ast.For):
                if name in target_names(node.target):
                    return True
            elif isinstance(node, ast.With):
                for item in node.items:
                    if item.optional_vars and name in target_names(item.optional_vars):
                        return True
        return False

    def _copy_model_field_payloads(
        self,
        source_bases: set[str],
        target_bases: set[str],
        edge_type: str,
        evidence: str,
    ) -> None:
        for source_base in source_bases:
            for key in MODEL_PAYLOAD_KEYS:
                source_payload = payload_node(source_base, key)
                if source_payload not in self.graph:
                    continue
                for target_base in target_bases:
                    target_payload = payload_node(target_base, key)
                    target_attr = attr_node(target_base, key)
                    self._add_edge(source_payload, target_payload, edge_type, evidence)
                    self._add_edge(source_payload, target_attr, edge_type.replace("payload", "value"), evidence)

    def _add_node(self, node: str, **attrs: Any) -> None:
        if not node:
            return
        if node not in self.node_meta:
            self.node_meta[node] = dict(attrs)
        else:
            self.node_meta[node].update({k: v for k, v in attrs.items() if v not in {"", 0, None}})
        self.graph.add_node(node, **self.node_meta[node])

    def _add_edge(self, source: str, target: str, edge_type: str, evidence: str) -> None:
        if not source or not target:
            return
        self._add_node(source)
        self._add_node(target)
        if source in self.payload_provider_keys:
            self.payload_provider_keys[target].update(self.payload_provider_keys[source])
        self.graph.add_edge(source, target, edge_type=edge_type, evidence=evidence)
