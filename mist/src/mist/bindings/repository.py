"""Repository support for PTM binding analysis."""

from __future__ import annotations

from collections import defaultdict
from mist.analysis.parse_cache import ParsedPythonFile
from mist.analysis.parse_cache import parse_python_file
from mist.analysis.parse_cache import parse_python_files
from mist.io import iter_python_files
from pathlib import Path
from typing import Sequence
import ast
from mist.bindings.constants import (
    SOURCE_ROOT_DIRS,
)
from mist.bindings.syntax import (
    call_chain,
    is_model_arg_name,
)
from mist.bindings.types import (
    ClassDefInfo,
    FileFacts,
    FunctionDefInfo,
    ImportRef,
    RepoFacts,
)


def parse_repo(repo: Path, *, syntax_cache: Path | None = None) -> RepoFacts:
    path_to_module, module_index = build_module_index(repo)
    aliases_by_rel: dict[str, list[str]] = defaultdict(list)
    for module, rel_path in module_index.items():
        aliases_by_rel[rel_path].append(module)
    files: dict[str, FileFacts] = {}
    functions: dict[str, FunctionDefInfo] = {}
    classes: dict[str, ClassDefInfo] = {}
    functions_by_module_name: dict[tuple[str, str], list[str]] = defaultdict(list)
    classes_by_module_name: dict[tuple[str, str], str] = {}

    for parsed in parse_python_files(
        repo,
        syntax_cache=syntax_cache,
        check_file_name=True,
        sort_paths=True,
        collect_nodes=True,
        collect_parents=True,
    ):
        rel_path = parsed.rel_path
        module = path_to_module.get(rel_path, module_name_from_path(Path(rel_path)))
        facts = file_facts_from_parsed(parsed, module)
        files[rel_path] = facts

    for facts in files.values():
        if facts.tree is None:
            continue
        facts.imports = extract_imports(facts, module_index)
        facts.classes = extract_classes(facts)
        for cls in facts.classes:
            classes[cls.symbol] = cls
            classes_by_module_name[(cls.module, cls.name)] = cls.symbol
            for alias in aliases_by_rel.get(facts.rel_path, []):
                classes_by_module_name[(alias, cls.name)] = cls.symbol
        facts.functions = extract_functions(facts, classes_by_module_name)
        for fn in facts.functions:
            functions[fn.symbol] = fn
            functions_by_module_name[(fn.module, fn.name)].append(fn.symbol)
            for alias in aliases_by_rel.get(facts.rel_path, []):
                functions_by_module_name[(alias, fn.name)].append(fn.symbol)

    return RepoFacts(
        repo=repo,
        module_index=module_index,
        path_to_module=path_to_module,
        files=files,
        functions=functions,
        classes=classes,
        functions_by_module_name=dict(functions_by_module_name),
        classes_by_module_name=classes_by_module_name,
    )


def build_module_index(repo: Path) -> tuple[dict[str, str], dict[str, str]]:
    path_to_module: dict[str, str] = {}
    module_index: dict[str, str] = {}
    for path in iter_python_files(repo, check_file_name=True, sort_paths=True):
        rel = path.relative_to(repo).as_posix()
        module = module_name_from_path(Path(rel))
        path_to_module[rel] = module
        module_index[module] = rel
        if path.name == "__init__.py":
            package = ".".join(module.split(".")[:-1])
            if package:
                module_index[package] = rel
    add_unique_suffix_module_aliases(module_index)
    return path_to_module, module_index


def add_unique_suffix_module_aliases(module_index: dict[str, str]) -> None:
    """Add safe aliases such as `llm` for uniquely named project modules."""
    suffix_refs: dict[str, set[str]] = defaultdict(set)
    canonical_items = list(module_index.items())
    for module, rel_path in canonical_items:
        parts = module.split(".")
        for index in range(1, len(parts)):
            alias = ".".join(parts[index:])
            if alias:
                suffix_refs[alias].add(rel_path)
    for alias, rel_paths in suffix_refs.items():
        if alias not in module_index and len(rel_paths) == 1:
            module_index[alias] = next(iter(rel_paths))


def module_name_from_path(rel_path: Path) -> str:
    parts = list(rel_path.with_suffix("").parts)
    if len(parts) > 1 and parts[0] in SOURCE_ROOT_DIRS:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def parse_file(path: Path, rel_path: str, module: str) -> FileFacts:
    parsed = parse_python_file(path, rel_path=rel_path, collect_nodes=True, collect_parents=True)
    return file_facts_from_parsed(parsed, module)


def file_facts_from_parsed(parsed: ParsedPythonFile, module: str) -> FileFacts:
    return FileFacts(
        rel_path=parsed.rel_path,
        full_path=parsed.path,
        module=module,
        lines=parsed.lines,
        tree=parsed.tree,
        parse_error=parsed.parse_error,
        nodes=parsed.nodes,
        parents=parsed.parents,
    )


def extract_imports(facts: FileFacts, module_index: dict[str, str]) -> dict[str, ImportRef]:
    imports: dict[str, ImportRef] = {}
    if facts.tree is None:
        return imports
    for node in facts.nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                local = alias.asname or name.split(".")[0]
                module = name
                imports[local] = ImportRef(local, module, is_project=module in module_index, line_number=node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_relative_module(facts.module, node.module or "", node.level)
            for alias in node.names:
                local = alias.asname or alias.name
                imports[local] = ImportRef(
                    local,
                    module,
                    symbol=alias.name,
                    is_project=module in module_index,
                    line_number=node.lineno,
                )
    return imports


def resolve_relative_module(current_module: str, imported: str, level: int) -> str:
    if level <= 0:
        return imported
    parts = current_module.split(".")
    base = parts[: max(0, len(parts) - level)]
    if imported:
        base.extend(imported.split("."))
    return ".".join(part for part in base if part)


def extract_classes(facts: FileFacts) -> list[ClassDefInfo]:
    classes: list[ClassDefInfo] = []
    if facts.tree is None:
        return classes
    for node in facts.nodes:
        if isinstance(node, ast.ClassDef):
            qualname = class_qualname(facts, node)
            symbol = f"{facts.module}.{qualname}"
            classes.append(
                ClassDefInfo(
                    symbol=symbol,
                    module=facts.module,
                    rel_path=facts.rel_path,
                    name=node.name,
                    bases=tuple(local_base_symbols(facts, node)),
                    node=node,
                    line_number=node.lineno,
                )
            )
    return classes


def extract_functions(
    facts: FileFacts,
    classes_by_module_name: dict[tuple[str, str], str],
) -> list[FunctionDefInfo]:
    functions: list[FunctionDefInfo] = []
    if facts.tree is None:
        return functions
    for node in facts.nodes:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        class_symbol = ""
        class_name = ""
        parent = facts.parents.get(node)
        if isinstance(parent, ast.ClassDef):
            class_name = parent.name
            class_symbol = classes_by_module_name.get((facts.module, parent.name), "")
        qualname = function_qualname(facts, node)
        symbol = f"{facts.module}.{qualname}"
        functions.append(
            FunctionDefInfo(
                symbol=symbol,
                module=facts.module,
                rel_path=facts.rel_path,
                name=node.name,
                class_symbol=class_symbol,
                class_name=class_name,
                node=node,
                params=tuple(function_params(node)),
                line_number=node.lineno,
            )
        )
    return functions


def class_qualname(facts: FileFacts, node: ast.ClassDef) -> str:
    names = [node.name]
    parent = facts.parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.ClassDef):
            names.append(parent.name)
        parent = facts.parents.get(parent)
    return ".".join(reversed(names))


def local_base_symbols(facts: FileFacts, node: ast.ClassDef) -> list[str]:
    bases = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            ref = facts.imports.get(base.id)
            if ref and ref.is_project and ref.symbol:
                bases.append(f"{ref.module}.{ref.symbol}")
            else:
                bases.append(f"{facts.module}.{base.id}")
        elif isinstance(base, ast.Attribute):
            chain = call_chain(base)
            if "." not in chain:
                continue
            root, _, rest = chain.partition(".")
            ref = facts.imports.get(root)
            if ref and ref.is_project:
                if ref.symbol:
                    bases.append(f"{ref.module}.{ref.symbol}.{rest}")
                else:
                    bases.append(f"{ref.module}.{rest}")
            else:
                bases.append(chain)
    return bases


def function_qualname(facts: FileFacts, node: ast.AST) -> str:
    names = [getattr(node, "name", "")]
    parent = facts.parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(parent.name)
        parent = facts.parents.get(parent)
    return ".".join(reversed([name for name in names if name]))


def function_params(node: ast.AST) -> list[str]:
    args = getattr(node, "args", None)
    if args is None:
        return []
    params = [arg.arg for arg in args.posonlyargs + args.args]
    if args.vararg:
        params.append(args.vararg.arg)
    params.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg:
        params.append(args.kwarg.arg)
    return params


def single_model_like_param(params: Sequence[str]) -> list[str]:
    """Return the only model-like parameter when a function has one clear target."""
    candidates = [
        param
        for param in params
        if param not in {"self", "cls"} and is_model_arg_name(param)
    ]
    return candidates if len(candidates) == 1 else []


def function_defaults(node: ast.AST) -> dict[str, ast.AST]:
    args = getattr(node, "args", None)
    if args is None:
        return {}
    params = [arg.arg for arg in args.posonlyargs + args.args]
    defaults: dict[str, ast.AST] = {}
    for param, default in zip(params[-len(args.defaults):], args.defaults):
        defaults[param] = default
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            defaults[arg.arg] = default
    return defaults


def literal_annotation_values(node: ast.AST, param_name: str) -> set[str]:
    args = getattr(node, "args", None)
    if args is None:
        return set()
    for arg in args.posonlyargs + args.args + args.kwonlyargs:
        if arg.arg != param_name:
            continue
        return literal_values_from_annotation(arg.annotation)
    return set()


def literal_values_from_annotation(annotation: ast.AST | None) -> set[str]:
    if annotation is None:
        return set()
    if isinstance(annotation, ast.Subscript) and call_chain(annotation.value).endswith("Literal"):
        slice_node = annotation.slice
        values = slice_node.elts if isinstance(slice_node, ast.Tuple) else [slice_node]
        return {
            value.value
            for value in values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        }
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return literal_values_from_annotation(annotation.left) | literal_values_from_annotation(annotation.right)
    return set()


def is_property_function(node: ast.AST | None) -> bool:
    """Return true for simple property-like methods."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if call_chain(target) in {"property", "cached_property", "functools.cached_property"}:
            return True
    return False


def direct_function_returns(facts: FileFacts, node: ast.AST | None) -> list[ast.Return]:
    """Return statements owned by this function, excluding nested scopes."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    returns: list[ast.Return] = []
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Return):
            continue
        cursor: ast.AST | None = candidate
        direct = False
        while cursor in facts.parents:
            cursor = facts.parents[cursor]
            if cursor is node:
                direct = True
                break
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                break
        if direct:
            returns.append(candidate)
    return returns
