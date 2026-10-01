"""Conservative different-model binding pairing after same-ID continuity."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from common import structural_fingerprint
from continuity import DiffHunk


REPLACEMENT_PAIRING_SCHEMA = "binding_replacement"


@dataclass(frozen=True)
class ReplacementPairingResult:
    """Accepted pairs, unresolved relation groups, and remaining bindings."""

    pairs: tuple[dict[str, object], ...]
    unresolved_groups: tuple[dict[str, object], ...]
    turnover_group_resolutions: tuple[dict[str, object], ...]
    residual_bindings: tuple[dict[str, object], ...]


def pair_different_id_bindings(
    pair: Mapping[str, object],
    old_bindings: Sequence[Mapping[str, object]],
    new_bindings: Sequence[Mapping[str, object]],
    *,
    diff_hunks: Sequence[DiffHunk] = (),
) -> ReplacementPairingResult:
    """Pair different IDs only when a model-independent relation is one-to-one."""
    pair_key = _pair_key(pair)
    old = _binding_map(old_bindings, "old")
    new = _binding_map(new_bindings, "new")
    remaining_old = set(old)
    remaining_new = set(new)
    pairs: list[dict[str, object]] = []

    stages: tuple[
        tuple[str, Callable[[Mapping[str, object]], str]], ...
    ] = (
        ("exact_binding_anchor", lambda row: str(row.get("site_binding_anchor_key", ""))),
        (
            "exact_source_anchor_and_semantic_loader_role",
            _source_and_loader_role_key,
        ),
        ("exact_source_anchor", lambda row: str(row.get("source_anchor_key", ""))),
        ("exact_loader_anchor", lambda row: str(row.get("loader_anchor_key", ""))),
    )
    for relation, key_function in stages:
        for old_key, new_key in _unique_different_id_groups(
            old, new, remaining_old, remaining_new, key_function
        ):
            pairs.append(_pair_row(pair_key, old[old_key], new[new_key], relation))
            remaining_old.remove(old_key)
            remaining_new.remove(new_key)

    for hunk in sorted(diff_hunks, key=lambda row: row.hunk_id):
        old_keys = [
            key for key in sorted(remaining_old) if _binding_in_hunk(old[key], hunk, "old")
        ]
        new_keys = [
            key for key in sorted(remaining_new) if _binding_in_hunk(new[key], hunk, "new")
        ]
        if len(old_keys) != 1 or len(new_keys) != 1:
            continue
        old_key, new_key = old_keys[0], new_keys[0]
        if _same_model(old[old_key], new[new_key]):
            continue
        pairs.append(
            _pair_row(
                pair_key,
                old[old_key],
                new[new_key],
                "unique_diff_hunk",
                additional_evidence=(f"diff_hunk:{hunk.hunk_id}",),
            )
        )
        remaining_old.remove(old_key)
        remaining_new.remove(new_key)

    unresolved_groups, group_ids_by_binding = _unresolved_components(
        pair_key,
        old,
        new,
        remaining_old,
        remaining_new,
        diff_hunks,
    )
    turnover_resolutions, unresolved_turnover_groups = _resolve_turnover_groups(
        pair_key,
        old,
        new,
        remaining_old,
        remaining_new,
    )
    residuals = [
        _residual_row(
            pair_key,
            "old",
            old[key],
            group_ids_by_binding.get(("old", key), ()),
            unresolved_turnover_groups,
        )
        for key in sorted(remaining_old)
    ] + [
        _residual_row(
            pair_key,
            "new",
            new[key],
            group_ids_by_binding.get(("new", key), ()),
            unresolved_turnover_groups,
        )
        for key in sorted(remaining_new)
    ]
    return ReplacementPairingResult(
        pairs=tuple(sorted(pairs, key=lambda row: str(row["replacement_pair_key"]))),
        unresolved_groups=tuple(
            sorted(unresolved_groups, key=lambda row: str(row["unresolved_group_id"]))
        ),
        turnover_group_resolutions=tuple(
            sorted(
                turnover_resolutions,
                key=lambda row: str(row["site_turnover_group_id"]),
            )
        ),
        residual_bindings=tuple(
            sorted(
                residuals,
                key=lambda row: (
                    str(row["side"]), str(row["confirmed_binding_key"])
                ),
            )
        ),
    )


def _binding_map(
    rows: Sequence[Mapping[str, object]], side: str
) -> dict[str, Mapping[str, object]]:
    output: dict[str, Mapping[str, object]] = {}
    for row in rows:
        key = str(row.get("confirmed_binding_key", ""))
        model_id = str(row.get("canonical_model_id", ""))
        if not key or not model_id:
            raise ValueError(f"{side} binding is missing its key or canonical model ID")
        if key in output:
            raise ValueError(f"duplicate {side} confirmed binding key: {key}")
        output[key] = row
    return output


def _unique_different_id_groups(
    old: Mapping[str, Mapping[str, object]],
    new: Mapping[str, Mapping[str, object]],
    remaining_old: set[str],
    remaining_new: set[str],
    key_function: Callable[[Mapping[str, object]], str],
) -> tuple[tuple[str, str], ...]:
    old_groups: dict[str, list[str]] = defaultdict(list)
    new_groups: dict[str, list[str]] = defaultdict(list)
    for key in sorted(remaining_old):
        group = key_function(old[key])
        if group:
            old_groups[group].append(key)
    for key in sorted(remaining_new):
        group = key_function(new[key])
        if group:
            new_groups[group].append(key)
    result = []
    for group in sorted(set(old_groups) & set(new_groups)):
        if len(old_groups[group]) != 1 or len(new_groups[group]) != 1:
            continue
        old_key, new_key = old_groups[group][0], new_groups[group][0]
        if not _same_model(old[old_key], new[new_key]):
            result.append((old_key, new_key))
    return tuple(result)


def _pair_row(
    pair_key: str,
    old: Mapping[str, object],
    new: Mapping[str, object],
    relation: str,
    *,
    additional_evidence: tuple[str, ...] = (),
) -> dict[str, object]:
    old_key = str(old["confirmed_binding_key"])
    new_key = str(new["confirmed_binding_key"])
    old_model = str(old["canonical_model_id"])
    new_model = str(new["canonical_model_id"])
    replacement_key = structural_fingerprint(
        {
            "schema": REPLACEMENT_PAIRING_SCHEMA,
            "pair_key": pair_key,
            "old_binding_key": old_key,
            "new_binding_key": new_key,
            "relation": relation,
        }
    )
    evidence = {relation, *additional_evidence}
    if old.get("source_resolved_file_path") != new.get("source_resolved_file_path"):
        if old.get("source_logical_file_path_id") == new.get(
            "source_logical_file_path_id"
        ):
            evidence.add("git_file_rename_continuity")
        else:
            evidence.add("source_path_changed")
    if old.get("source_line_number") != new.get("source_line_number"):
        evidence.add("source_line_moved")
    return {
        "schema": REPLACEMENT_PAIRING_SCHEMA,
        "record_type": "different_id_replacement_pair",
        "replacement_pair_key": replacement_key,
        "pair_key": pair_key,
        "pairing_status": "paired_pending_transition_classification",
        "relation": relation,
        "confidence": "high",
        "evidence": sorted(evidence),
        "transition_taxonomy_consulted": False,
        "old_confirmed_binding_key": old_key,
        "new_confirmed_binding_key": new_key,
        "old_candidate_key": old.get("candidate_key", ""),
        "new_candidate_key": new.get("candidate_key", ""),
        "old_model_id": old_model,
        "new_model_id": new_model,
        "old_source_path": old.get("source_resolved_file_path", ""),
        "new_source_path": new.get("source_resolved_file_path", ""),
        "old_source_line": old.get("source_line_number", ""),
        "new_source_line": new.get("source_line_number", ""),
        "old_loader_path": old.get("loader_file_path", ""),
        "new_loader_path": new.get("loader_file_path", ""),
        "old_loader_line": old.get("loader_line", ""),
        "new_loader_line": new.get("loader_line", ""),
        "old_resolved_callee": old.get("resolved_callee", ""),
        "new_resolved_callee": new.get("resolved_callee", ""),
        "old_receiver_origin": old.get("receiver_origin", ""),
        "new_receiver_origin": new.get("receiver_origin", ""),
        "integration_route_changed": any(
            old.get(field) != new.get(field)
            for field in (
                "loader_anchor_key", "resolved_callee", "receiver_origin", "model_role"
            )
        ),
    }


def _unresolved_components(
    pair_key: str,
    old: Mapping[str, Mapping[str, object]],
    new: Mapping[str, Mapping[str, object]],
    remaining_old: set[str],
    remaining_new: set[str],
    diff_hunks: Sequence[DiffHunk],
) -> tuple[list[dict[str, object]], dict[tuple[str, str], tuple[str, ...]]]:
    edge_relations: dict[tuple[str, str], set[str]] = defaultdict(set)

    def add_shared(relation: str, field: str) -> None:
        add_computed(relation, lambda row: str(row.get(field, "")))

    def add_computed(
        relation: str, key_function: Callable[[Mapping[str, object]], str]
    ) -> None:
        old_groups: dict[str, list[str]] = defaultdict(list)
        new_groups: dict[str, list[str]] = defaultdict(list)
        for key in sorted(remaining_old):
            value = key_function(old[key])
            if value:
                old_groups[value].append(key)
        for key in sorted(remaining_new):
            value = key_function(new[key])
            if value:
                new_groups[value].append(key)
        for value in sorted(set(old_groups) & set(new_groups)):
            for old_key in old_groups[value]:
                for new_key in new_groups[value]:
                    if not _same_model(old[old_key], new[new_key]):
                        edge_relations[(old_key, new_key)].add(relation)

    add_shared("exact_binding_anchor", "site_binding_anchor_key")
    add_computed(
        "exact_source_anchor_and_semantic_loader_role",
        _source_and_loader_role_key,
    )
    add_shared("exact_source_anchor", "source_anchor_key")
    add_shared("exact_loader_anchor", "loader_anchor_key")
    for hunk in sorted(diff_hunks, key=lambda row: row.hunk_id):
        old_keys = [
            key for key in sorted(remaining_old) if _binding_in_hunk(old[key], hunk, "old")
        ]
        new_keys = [
            key for key in sorted(remaining_new) if _binding_in_hunk(new[key], hunk, "new")
        ]
        for old_key in old_keys:
            for new_key in new_keys:
                if not _same_model(old[old_key], new[new_key]):
                    edge_relations[(old_key, new_key)].add(f"diff_hunk:{hunk.hunk_id}")

    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for old_key, new_key in edge_relations:
        old_node = ("old", old_key)
        new_node = ("new", new_key)
        adjacency[old_node].add(new_node)
        adjacency[new_node].add(old_node)

    groups: list[dict[str, object]] = []
    ids_by_binding: dict[tuple[str, str], list[str]] = defaultdict(list)
    visited: set[tuple[str, str]] = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        stack = [start]
        component: set[tuple[str, str]] = set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(sorted(adjacency[node] - component))
        visited.update(component)
        old_keys = sorted(key for side, key in component if side == "old")
        new_keys = sorted(key for side, key in component if side == "new")
        if not old_keys or not new_keys:
            continue
        component_edges = {
            (old_key, new_key): relations
            for (old_key, new_key), relations in edge_relations.items()
            if old_key in old_keys and new_key in new_keys
        }
        group_id = structural_fingerprint(
            {
                "schema": REPLACEMENT_PAIRING_SCHEMA,
                "pair_key": pair_key,
                "old_binding_keys": old_keys,
                "new_binding_keys": new_keys,
                "candidate_edges": [
                    [old_key, new_key, sorted(relations)]
                    for (old_key, new_key), relations in sorted(component_edges.items())
                ],
            }
        )
        for node in component:
            ids_by_binding[node].append(group_id)
        groups.append(
            {
                "schema": REPLACEMENT_PAIRING_SCHEMA,
                "record_type": "unresolved_replacement_group",
                "unresolved_group_id": group_id,
                "pair_key": pair_key,
                "pairing_status": "unresolved_competing_explicit_relations",
                "old_binding_keys": old_keys,
                "new_binding_keys": new_keys,
                "old_model_ids": sorted(
                    {str(old[key]["canonical_model_id"]) for key in old_keys}
                ),
                "new_model_ids": sorted(
                    {str(new[key]["canonical_model_id"]) for key in new_keys}
                ),
                "candidate_edges": [
                    {
                        "old_binding_key": old_key,
                        "new_binding_key": new_key,
                        "evidence": sorted(relations),
                    }
                    for (old_key, new_key), relations in sorted(component_edges.items())
                ],
                "reason": (
                    "multiple residual binding relationships remain plausible; no arbitrary "
                    "one-to-one replacement is selected"
                ),
            }
        )
    return groups, {
        key: tuple(sorted(values)) for key, values in ids_by_binding.items()
    }


def _residual_row(
    pair_key: str,
    side: str,
    row: Mapping[str, object],
    unresolved_group_ids: Sequence[str],
    unresolved_turnover_groups: set[str],
) -> dict[str, object]:
    turnover_group_id = str(row.get("site_turnover_group_id", ""))
    if unresolved_group_ids:
        disposition = "unresolved_replacement_member"
    elif turnover_group_id in unresolved_turnover_groups:
        disposition = "same_id_turnover_member"
    else:
        disposition = f"definite_unpaired_{'remove' if side == 'old' else 'add'}_candidate"
    return {
        "schema": REPLACEMENT_PAIRING_SCHEMA,
        "record_type": "post_replacement_residual_binding",
        "pair_key": pair_key,
        "side": side,
        "confirmed_binding_key": row.get("confirmed_binding_key", ""),
        "candidate_key": row.get("candidate_key", ""),
        "canonical_model_id": row.get("canonical_model_id", ""),
        "release_row_id": row.get("release_row_id", ""),
        "source_resolved_file_path": row.get("source_resolved_file_path", ""),
        "source_line_number": row.get("source_line_number", ""),
        "loader_file_path": row.get("loader_file_path", ""),
        "loader_line": row.get("loader_line", ""),
        "site_turnover_group_id": turnover_group_id,
        "site_turnover_group_still_unresolved": (
            turnover_group_id in unresolved_turnover_groups
        ),
        "unresolved_replacement_group_ids": list(unresolved_group_ids),
        "disposition": disposition,
        "eligible_for_definite_add_remove": (
            not unresolved_group_ids
            and turnover_group_id not in unresolved_turnover_groups
        ),
    }


def _resolve_turnover_groups(
    pair_key: str,
    old: Mapping[str, Mapping[str, object]],
    new: Mapping[str, Mapping[str, object]],
    remaining_old: set[str],
    remaining_new: set[str],
) -> tuple[list[dict[str, object]], set[str]]:
    """Recheck unpaired same-ID groups after accepted replacements."""
    input_members: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"old": [], "new": []}
    )
    for side, rows in (("old", old), ("new", new)):
        for key, row in rows.items():
            group_id = str(row.get("site_turnover_group_id", ""))
            if group_id:
                input_members[group_id][side].append(key)

    resolutions: list[dict[str, object]] = []
    unresolved: set[str] = set()
    for group_id in sorted(input_members):
        old_input = sorted(input_members[group_id]["old"])
        new_input = sorted(input_members[group_id]["new"])
        old_remaining = sorted(set(old_input) & remaining_old)
        new_remaining = sorted(set(new_input) & remaining_new)
        if old_remaining and new_remaining:
            status = "unresolved_members_both_sides"
            unresolved.add(group_id)
        elif old_remaining:
            status = "resolved_to_definite_remove_residuals"
        elif new_remaining:
            status = "resolved_to_definite_add_residuals"
        else:
            status = "superseded_by_accepted_replacements"
        resolutions.append(
            {
                "schema": REPLACEMENT_PAIRING_SCHEMA,
                "record_type": "same_id_turnover_resolution",
                "pair_key": pair_key,
                "site_turnover_group_id": group_id,
                "resolution_status": status,
                "input_old_binding_keys": old_input,
                "input_new_binding_keys": new_input,
                "remaining_old_binding_keys": old_remaining,
                "remaining_new_binding_keys": new_remaining,
                "consumed_old_binding_keys": sorted(set(old_input) - remaining_old),
                "consumed_new_binding_keys": sorted(set(new_input) - remaining_new),
                "reason": (
                    "Same-ID groups are rechecked after accepted model-independent "
                    "different-ID replacement relations; no count-based correspondence is inferred"
                ),
            }
        )
    return resolutions, unresolved


def _binding_in_hunk(
    row: Mapping[str, object], hunk: DiffHunk, side: str
) -> bool:
    if side == "old":
        return row.get("source_resolved_file_path") == hunk.old_path and _line_in_range(
            int(row.get("source_line_number", 0)), hunk.old_start, hunk.old_count
        )
    return row.get("source_resolved_file_path") == hunk.new_path and _line_in_range(
        int(row.get("source_line_number", 0)), hunk.new_start, hunk.new_count
    )


def _line_in_range(line: int, start: int, count: int) -> bool:
    return count > 0 and start <= line < start + count


def _same_model(old: Mapping[str, object], new: Mapping[str, object]) -> bool:
    return old.get("canonical_model_id") == new.get("canonical_model_id")


def _source_and_loader_role_key(row: Mapping[str, object]) -> str:
    source_anchor = str(row.get("source_anchor_key", ""))
    loader_role = tuple(
        str(row.get(field, ""))
        for field in (
            "loader_logical_file_path_id",
            "loader_class_context",
            "loader_function_context",
            "model_role",
        )
    )
    if not source_anchor or not any(loader_role):
        return ""
    return structural_fingerprint(
        {
            "relation": "source_anchor_and_semantic_loader_role",
            "source_anchor_key": source_anchor,
            "loader_role": loader_role,
        }
    )


def _pair_key(pair: Mapping[str, object]) -> str:
    return f"{pair.get('old_release_row_id', '')}->{pair.get('new_release_row_id', '')}"
