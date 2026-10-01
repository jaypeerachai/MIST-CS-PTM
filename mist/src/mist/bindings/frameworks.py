#!/usr/bin/env python3
"""Infer guarded binding edges for framework-managed model configuration.

The reusable core models finite selectors, keyed framework persistence, typed
state reads, and Python lexical closures. Framework-specific API semantics are
supplied as data; repository names, model IDs, and local variable names are not
part of the implementation.
"""

from __future__ import annotations

import ast
import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    edge_type: str
    evidence: str
    overlay: bool = False


@dataclass(frozen=True)
class ModuleFacts:
    relative_path: str
    module_name: str
    tree: ast.Module
    imports: dict[str, str]


@dataclass(frozen=True)
class ConfigChannel:
    summary_name: str
    state_namespace: str
    component_identity: str
    key_identity: str
    choice_node: str
    selection_node: str
    state_node: str
    schema_file: str
    selector_line: int
    form_line: int
    persistence_line: int


def module_name_for(relative_path: Path) -> str:
    parts = list(relative_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def component_identity_for(relative_path: str) -> str:
    """Return a repository-relative integration/package boundary."""
    parts = Path(relative_path).parts
    if not parts:
        return "<repo-root>"
    if "custom_components" in parts:
        index = parts.index("custom_components")
        if len(parts) > index + 1:
            return "/".join(parts[: index + 2])
    if len(parts) > 1:
        return parts[0]
    return "<repo-root>"


def resolve_import_from(module_name: str, imported_module: str | None, level: int) -> str:
    if level == 0:
        return imported_module or ""
    package = module_name.split(".")[:-1]
    keep = max(0, len(package) - (level - 1))
    base = package[:keep]
    if imported_module:
        base.extend(imported_module.split("."))
    return ".".join(base)


def collect_imports(tree: ast.Module, module_name: str) -> dict[str, str]:
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                imports[local] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = resolve_import_from(module_name, node.module, node.level)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                imports[local] = f"{base}.{alias.name}" if base else alias.name
    return imports


def load_modules(repo_root: Path) -> list[ModuleFacts]:
    modules: list[ModuleFacts] = []
    for source_path in sorted(repo_root.rglob("*.py")):
        if any(part in {".git", ".venv", "venv", "site-packages"} for part in source_path.parts):
            continue
        relative = source_path.relative_to(repo_root)
        try:
            text = source_path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=relative.as_posix())
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        module_name = module_name_for(relative)
        modules.append(
            ModuleFacts(
                relative.as_posix(),
                module_name,
                tree,
                collect_imports(tree, module_name),
            )
        )
    return modules


def dotted_parts(expr: ast.AST) -> list[str] | None:
    if isinstance(expr, ast.Name):
        return [expr.id]
    if isinstance(expr, ast.Attribute):
        prefix = dotted_parts(expr.value)
        if prefix is None:
            return None
        return [*prefix, expr.attr]
    return None


def resolve_fqn(expr: ast.AST, module: ModuleFacts) -> str | None:
    if isinstance(expr, ast.Constant):
        return f"literal:{expr.value!r}"
    parts = dotted_parts(expr)
    if not parts:
        return None
    head = module.imports.get(parts[0])
    if head:
        return ".".join([head, *parts[1:]])
    if len(parts) == 1:
        return f"{module.module_name}.{parts[0]}" if module.module_name else parts[0]
    return ".".join(parts)


def call_keyword(call: ast.Call, names: Iterable[str]) -> ast.AST | None:
    wanted = set(names)
    for keyword in call.keywords:
        if keyword.arg in wanted:
            return keyword.value
    return None


def call_method_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def iter_calls(node: ast.AST) -> Iterator[ast.Call]:
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            yield child


def expression_contains_name(expr: ast.AST, names: set[str]) -> bool:
    return any(isinstance(node, ast.Name) and node.id in names for node in ast.walk(expr))


def function_arguments(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    result = {arg.arg for arg in args}
    if function.args.vararg:
        result.add(function.args.vararg.arg)
    if function.args.kwarg:
        result.add(function.args.kwarg.arg)
    return result


def ordered_function_arguments(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    result = [arg.arg for arg in args if arg.arg not in {"self", "cls"}]
    if function.args.vararg:
        result.append(function.args.vararg.arg)
    if function.args.kwarg:
        result.append(function.args.kwarg.arg)
    return result


def is_self_method_call(call: ast.Call) -> bool:
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
    )


def mapping_covers_submitted_key(
    expr: ast.AST,
    submission_parameter: str,
    key_identity: str,
    module: ModuleFacts,
) -> bool:
    """Whether a save expression preserves the submitted mapping or this key."""
    if isinstance(expr, ast.Name):
        return expr.id == submission_parameter
    if isinstance(expr, ast.Dict):
        for key, value in zip(expr.keys, expr.values):
            if key is None:
                if mapping_covers_submitted_key(
                    value, submission_parameter, key_identity, module
                ):
                    return True
                continue
            if (
                resolve_fqn(key, module) == key_identity
                and expression_contains_name(value, {submission_parameter})
            ):
                return True
        return False
    if isinstance(expr, ast.Call):
        return any(
            mapping_covers_submitted_key(arg, submission_parameter, key_identity, module)
            for arg in expr.args
        )
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        return mapping_covers_submitted_key(
            expr.left, submission_parameter, key_identity, module
        ) or mapping_covers_submitted_key(
            expr.right, submission_parameter, key_identity, module
        )
    return False


def assigned_expressions(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.AST]:
    assignments: dict[str, ast.AST] = {}

    class AssignmentVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
            self.generic_visit(node.value)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if isinstance(node.target, ast.Name) and node.value is not None:
                assignments[node.target.id] = node.value
            if node.value is not None:
                self.generic_visit(node.value)

    AssignmentVisitor().visit(function)
    return assignments


def dereference_local(expr: ast.AST, assignments: dict[str, ast.AST]) -> ast.AST:
    seen: set[str] = set()
    current = expr
    while isinstance(current, ast.Name) and current.id in assignments and current.id not in seen:
        seen.add(current.id)
        current = assignments[current.id]
    return current


def class_qualifies(node: ast.ClassDef, module: ModuleFacts, summary: dict[str, Any]) -> bool:
    allowed = set(summary["flow_base_classes"])
    return any(resolve_fqn(base, module) in allowed for base in node.bases)


def graph_nodes(edges: list[GraphEdge]) -> set[str]:
    return {node for edge in edges for node in (edge.source, edge.target)}


def find_graph_variable(
    nodes: set[str], module: ModuleFacts, name: str, scope: str | None = None
) -> str | None:
    candidates: list[str] = []
    if scope:
        candidates.append(f"var|{scope}|{name}")
    candidates.append(f"var|{module.module_name}|{name}")
    for candidate in candidates:
        if candidate in nodes:
            return candidate
    suffix = f"|{name}"
    module_marker = f"|{module.module_name}"
    fallback = sorted(node for node in nodes if node.startswith("var|") and node.endswith(suffix) and module_marker in node)
    return fallback[0] if len(fallback) == 1 else None


def schema_pairs(
    schema_expr: ast.AST, module: ModuleFacts, summary: dict[str, Any]
) -> list[tuple[str, ast.AST, int]]:
    if not isinstance(schema_expr, ast.Call):
        return []
    if resolve_fqn(schema_expr.func, module) not in set(summary["schema_calls"]):
        return []
    if not schema_expr.args or not isinstance(schema_expr.args[0], ast.Dict):
        return []
    pairs: list[tuple[str, ast.AST, int]] = []
    for key_expr, value_expr in zip(schema_expr.args[0].keys, schema_expr.args[0].values):
        if key_expr is None or not isinstance(key_expr, ast.Call) or not isinstance(value_expr, ast.Call):
            continue
        if resolve_fqn(key_expr.func, module) not in set(summary["key_calls"]):
            continue
        if resolve_fqn(value_expr.func, module) not in set(summary["selector_calls"]):
            continue
        if not key_expr.args or not value_expr.args:
            continue
        key_identity = resolve_fqn(key_expr.args[0], module)
        if key_identity:
            pairs.append((key_identity, value_expr.args[0], getattr(value_expr, "lineno", 0)))
    return pairs


def infer_config_channels(
    modules: list[ModuleFacts], summaries: list[dict[str, Any]], base_nodes: set[str]
) -> list[ConfigChannel]:
    channels: list[ConfigChannel] = []
    for module in modules:
        for class_node in (node for node in module.tree.body if isinstance(node, ast.ClassDef)):
            for summary in summaries:
                if not class_qualifies(class_node, module, summary):
                    continue
                for function in (
                    child
                    for child in class_node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                ):
                    assignments = assigned_expressions(function)
                    parameters = ordered_function_arguments(function)
                    position = int(summary.get("submission_parameter_position", 0))
                    if position < 0 or position >= len(parameters):
                        continue
                    submission_parameter = parameters[position]
                    displayed_schemas: list[tuple[ast.AST, int]] = []
                    persistence_calls: list[tuple[ast.AST, int]] = []
                    for call in iter_calls(function):
                        method = call_method_name(call)
                        if method in set(summary["form_methods"]) and is_self_method_call(call):
                            schema_value = call_keyword(call, summary["form_schema_keywords"])
                            if schema_value is not None:
                                displayed_schemas.append(
                                    (dereference_local(schema_value, assignments), call.lineno)
                                )
                        if method in set(summary["persistence_methods"]) and is_self_method_call(call):
                            data_value = call_keyword(call, summary["persistence_data_keywords"])
                            if data_value is not None:
                                persistence_calls.append((data_value, call.lineno))
                    if not displayed_schemas or not persistence_calls:
                        continue
                    scope = ".".join(
                        part for part in (module.module_name, class_node.name, function.name) if part
                    )
                    for schema_expr, schema_line in displayed_schemas:
                        for key_identity, choice_expr, selector_line in schema_pairs(
                            schema_expr, module, summary
                        ):
                            persistence_lines = [
                                line
                                for data_value, line in persistence_calls
                                if mapping_covers_submitted_key(
                                    data_value,
                                    submission_parameter,
                                    key_identity,
                                    module,
                                )
                            ]
                            if not persistence_lines:
                                continue
                            if not isinstance(choice_expr, ast.Name):
                                continue
                            choice_node = find_graph_variable(
                                base_nodes, module, choice_expr.id, scope=scope
                            )
                            if choice_node is None:
                                continue
                            stable_key = hashlib.sha256(key_identity.encode()).hexdigest()[:12]
                            component_identity = component_identity_for(module.relative_path)
                            stable_component = hashlib.sha256(
                                component_identity.encode()
                            ).hexdigest()[:10]
                            selection_node = (
                                f"framework_choice|{summary['name']}|{stable_component}|{stable_key}|"
                                f"{module.relative_path}:{selector_line}"
                            )
                            state_node = (
                                f"framework_state|{summary['state_namespace']}|"
                                f"{stable_component}|{stable_key}"
                            )
                            channels.append(
                                ConfigChannel(
                                    summary["name"],
                                    summary["state_namespace"],
                                    component_identity,
                                    key_identity,
                                    choice_node,
                                    selection_node,
                                    state_node,
                                    module.relative_path,
                                    selector_line,
                                    schema_line,
                                    min(persistence_lines),
                                )
                            )
    unique: dict[tuple[str, str, str, str], ConfigChannel] = {}
    for channel in channels:
        unique[
            (
                channel.summary_name,
                channel.component_identity,
                channel.key_identity,
                channel.choice_node,
            )
        ] = channel
    return list(unique.values())


def call_result_node(nodes: set[str], relative_path: str, call: ast.Call) -> str | None:
    prefix = f"call_result|{relative_path}|{call.lineno}|{call.col_offset}"
    if prefix in nodes:
        return prefix
    candidates = sorted(node for node in nodes if node.startswith(prefix + "|"))
    if len(candidates) == 1:
        return candidates[0]
    line_prefix = f"call_result|{relative_path}|{call.lineno}|"
    line_candidates = sorted(node for node in nodes if node.startswith(line_prefix))
    return line_candidates[0] if len(line_candidates) == 1 else None


def annotation_identity(annotation: ast.AST | None, module: ModuleFacts) -> str | None:
    if annotation is None:
        return None
    if isinstance(annotation, ast.Subscript):
        return resolve_fqn(annotation.value, module)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return annotation_identity(annotation.left, module) or annotation_identity(
            annotation.right, module
        )
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return annotation_identity(ast.parse(annotation.value, mode="eval").body, module)
        except SyntaxError:
            return None
    return resolve_fqn(annotation, module)


def parameter_types(
    function: ast.FunctionDef | ast.AsyncFunctionDef, module: ModuleFacts
) -> dict[str, str]:
    result: dict[str, str] = {}
    args = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    for argument in args:
        identity = annotation_identity(argument.annotation, module)
        if identity:
            result[argument.arg] = identity
    return result


def class_attribute_types(class_node: ast.ClassDef, module: ModuleFacts) -> dict[str, str]:
    result: dict[str, str] = {}
    for member in class_node.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        types = parameter_types(member, module)
        for node in ast.walk(member):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                value_type = types.get(node.value.id)
                if not value_type:
                    continue
                for target in node.targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        result[target.attr] = value_type
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Attribute)
                and isinstance(node.target.value, ast.Name)
                and node.target.value.id == "self"
            ):
                value_type = annotation_identity(node.annotation, module)
                if value_type:
                    result[node.target.attr] = value_type
        if any(
            isinstance(decorator, ast.Name) and decorator.id == "property"
            for decorator in member.decorator_list
        ):
            return_type = annotation_identity(member.returns, module)
            if return_type:
                result[member.name] = return_type
    return result


def state_owner_type(
    owner: ast.AST,
    parameters: dict[str, str],
    class_attributes: dict[str, str],
) -> str | None:
    if isinstance(owner, ast.Name):
        return parameters.get(owner.id)
    if (
        isinstance(owner, ast.Attribute)
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "self"
    ):
        return class_attributes.get(owner.attr)
    return None


def calls_in_function(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Call]:
    calls: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Call(self, node: ast.Call) -> None:
            calls.append(node)
            self.generic_visit(node)

    Visitor().visit(function)
    return calls


def infer_state_reads(
    modules: list[ModuleFacts], summary: dict[str, Any], base_nodes: set[str]
) -> list[tuple[str, str, str, int]]:
    reads: list[tuple[str, str, str, int]] = []
    allowed_attrs = set(summary["state_container_attributes"])
    allowed_methods = set(summary["state_read_methods"])
    allowed_owner_types = set(summary.get("state_owner_types", []))
    for module in modules:
        component = component_identity_for(module.relative_path)
        function_contexts: list[
            tuple[ast.FunctionDef | ast.AsyncFunctionDef, dict[str, str]]
        ] = []
        for node in module.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_contexts.append((node, {}))
            elif isinstance(node, ast.ClassDef):
                attributes = class_attribute_types(node, module)
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        function_contexts.append((member, attributes))
        for function, attributes in function_contexts:
            parameters = parameter_types(function, module)
            for call in calls_in_function(function):
                if call_method_name(call) not in allowed_methods or not call.args:
                    continue
                if (
                    not isinstance(call.func, ast.Attribute)
                    or not isinstance(call.func.value, ast.Attribute)
                ):
                    continue
                state_container = call.func.value
                if state_container.attr not in allowed_attrs:
                    continue
                owner_type = state_owner_type(
                    state_container.value, parameters, attributes
                )
                if allowed_owner_types and owner_type not in allowed_owner_types:
                    continue
                key_identity = resolve_fqn(call.args[0], module)
                target = call_result_node(base_nodes, module.relative_path, call)
                if key_identity and target:
                    reads.append((component, key_identity, target, call.lineno))
    return reads


class ScopeBindingVisitor(ast.NodeVisitor):
    """Collect bindings and loads in one function, excluding child scopes."""

    def __init__(self, root: ast.AST) -> None:
        self.root = root
        self.bound: set[str] = set()
        self.loaded: set[str] = set()
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node is self.root:
            self.bound.update(function_arguments(node))
            for statement in node.body:
                self.visit(statement)
        else:
            self.bound.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)
        elif isinstance(node.ctx, ast.Load):
            self.loaded.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.bound.update(alias.asname or alias.name for alias in node.names)

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocals.update(node.names)


def scope_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[set[str], set[str], set[str]]:
    visitor = ScopeBindingVisitor(node)
    visitor.visit(node)
    bound = visitor.bound - visitor.globals - visitor.nonlocals
    return bound, visitor.loaded, visitor.globals


def infer_closure_edges(modules: list[ModuleFacts], base_nodes: set[str]) -> list[GraphEdge]:
    result: list[GraphEdge] = []

    def inspect_function(
        module: ModuleFacts,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        parent_scope: str,
    ) -> None:
        outer_scope = ".".join(part for part in (parent_scope, function.name) if part)
        outer_bound, _, _ = scope_names(function)
        for child in function.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_bound, child_loaded, child_globals = scope_names(child)
                child_scope = f"{outer_scope}.{child.name}"
                for name in sorted(
                    (child_loaded - child_bound - child_globals) & outer_bound
                ):
                    source = f"var|{outer_scope}|{name}"
                    target = f"var|{child_scope}|{name}"
                    if source in base_nodes and target in base_nodes:
                        result.append(
                            GraphEdge(
                                source,
                                target,
                                "outer_scope_value_to_free_variable",
                                f"{module.relative_path}:{child.lineno}: nested function {child.name} "
                                f"captures {name} from {function.name}",
                                True,
                            )
                        )
                inspect_function(module, child, outer_scope)
            elif isinstance(child, ast.ClassDef):
                for member in child.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        inspect_function(module, member, f"{outer_scope}.{child.name}")

    for module in modules:
        for node in module.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inspect_function(module, node, module.module_name)
            elif isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        inspect_function(module, member, f"{module.module_name}.{node.name}")
    return result


def build_overlay(
    modules: list[ModuleFacts], summaries: list[dict[str, Any]], base_edges: list[GraphEdge]
) -> tuple[list[GraphEdge], list[ConfigChannel]]:
    nodes = graph_nodes(base_edges)
    channels = infer_config_channels(modules, summaries, nodes)
    overlay: list[GraphEdge] = []
    for channel in channels:
        overlay.append(
            GraphEdge(
                channel.choice_node,
                channel.selection_node,
                "framework_selector_choice_set",
                f"{channel.schema_file}:{channel.selector_line}: recognized selector for "
                f"resolved key {channel.key_identity}; displayed at line {channel.form_line}",
                True,
            )
        )
        overlay.append(
            GraphEdge(
                channel.selection_node,
                channel.state_node,
                "framework_form_value_persisted",
                f"{channel.schema_file}:{channel.persistence_line}: displayed choice is persisted "
                f"by {channel.summary_name}",
                True,
            )
        )
    matched_reads: list[tuple[ConfigChannel, str, int]] = []
    for summary in summaries:
        reads = infer_state_reads(modules, summary, nodes)
        matching_channels = [channel for channel in channels if channel.summary_name == summary["name"]]
        for channel in matching_channels:
            for component_identity, key_identity, target, line in reads:
                if (
                    key_identity != channel.key_identity
                    or component_identity != channel.component_identity
                ):
                    continue
                matched_reads.append((channel, target, line))

    # Keep every path introduced by a framework summary provenance-scoped.
    # A raw state-read or closure edge would merge a selectable registry value
    # with other values (for example, a default passed to ``dict.get``) at the
    # same program node. Shadowing only the downstream slice preserves the
    # framework value's identity until it reaches an existing eligible sink.
    path_edges = [*base_edges, *infer_closure_edges(modules, nodes)]
    outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
    incoming: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in path_edges:
        outgoing[edge.source].append(edge)
        incoming[edge.target].append(edge)
    sink_nodes = {node for node in nodes if node.startswith("sink|")}

    can_reach_sink = set(sink_nodes)
    queue = deque(sink_nodes)
    while queue:
        target = queue.popleft()
        for edge in incoming.get(target, []):
            if edge.source in can_reach_sink:
                continue
            can_reach_sink.add(edge.source)
            queue.append(edge.source)

    for channel, read_target, line in matched_reads:
        if read_target not in can_reach_sink:
            continue
        token = hashlib.sha256(
            f"{channel.state_node}\0{read_target}".encode("utf-8")
        ).hexdigest()[:16]

        def shadow(node: str) -> str:
            return f"framework_scoped|{token}|{node}"

        overlay.append(
            GraphEdge(
                channel.state_node,
                shadow(read_target),
                "framework_state_read",
                f"resolved key {channel.key_identity} is read from "
                f"{channel.state_namespace} at line {line}",
                True,
            )
        )
        reachable = {read_target}
        queue = deque([read_target])
        while queue:
            source = queue.popleft()
            for edge in outgoing.get(source, []):
                if edge.target not in can_reach_sink:
                    continue
                target = edge.target if edge.target in sink_nodes else shadow(edge.target)
                overlay.append(
                    GraphEdge(
                        shadow(edge.source),
                        target,
                        edge.edge_type,
                        edge.evidence,
                        True,
                    )
                )
                if edge.target not in sink_nodes and edge.target not in reachable:
                    reachable.add(edge.target)
                    queue.append(edge.target)
    unique: dict[tuple[str, str, str], GraphEdge] = {}
    for edge in overlay:
        unique[(edge.source, edge.target, edge.edge_type)] = edge
    return list(unique.values()), channels


def adjacency(edges: list[GraphEdge]) -> dict[str, list[GraphEdge]]:
    result: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in edges:
        result[edge.source].append(edge)
    for values in result.values():
        values.sort(key=lambda edge: (edge.target, edge.edge_type))
    return result


def shortest_sink_path(edges: list[GraphEdge], source: str) -> list[GraphEdge] | None:
    outgoing = adjacency(edges)
    queue: deque[str] = deque([source])
    predecessor: dict[str, GraphEdge | None] = {source: None}
    sink: str | None = source if source.startswith("sink|") else None
    while queue and sink is None:
        node = queue.popleft()
        for edge in outgoing.get(node, []):
            if edge.target in predecessor:
                continue
            predecessor[edge.target] = edge
            if edge.target.startswith("sink|"):
                sink = edge.target
                break
            queue.append(edge.target)
    if sink is None:
        return None
    path: list[GraphEdge] = []
    cursor = sink
    while cursor != source:
        edge = predecessor[cursor]
        assert edge is not None
        path.append(edge)
        cursor = edge.source
    return list(reversed(path))
