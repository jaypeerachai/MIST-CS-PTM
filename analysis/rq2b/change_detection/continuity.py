"""Conservative same-model binding continuity across adjacent releases."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from common import run_git
from common import structural_fingerprint


CONTINUITY_SCHEMA = "binding_continuity"


@dataclass(frozen=True)
class DiffHunk:
    hunk_id: str
    old_path: str
    new_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SameIdContinuityResult:
    matches: tuple[dict[str, object], ...]
    site_turnover_groups: tuple[dict[str, object], ...]
    residual_bindings: tuple[dict[str, object], ...]


def match_same_id_bindings(
    pair: Mapping[str, object],
    old_bindings: Sequence[Mapping[str, object]],
    new_bindings: Sequence[Mapping[str, object]],
    *,
    diff_hunks: Sequence[DiffHunk] = (),
) -> SameIdContinuityResult:
    """Match only identical-model bindings using unique structural evidence."""
    pair_key = _pair_key(pair)
    old = _binding_map(old_bindings, "old")
    new = _binding_map(new_bindings, "new")
    remaining_old = set(old)
    remaining_new = set(new)
    matches: list[dict[str, object]] = []

    stages: tuple[
        tuple[str, str, Callable[[Mapping[str, object]], tuple[object, ...] | None]],
        ...,
    ] = (
        ("exact_binding_anchor", "unchanged_no_model_change", _binding_anchor_key),
        ("exact_source_anchor", "refactored_no_model_change", _source_anchor_key),
        ("exact_loader_anchor", "refactored_no_model_change", _loader_anchor_key),
        (
            "same_semantic_source_and_loader_role",
            "refactored_no_model_change",
            _combined_semantic_key,
        ),
        ("same_semantic_source_role", "refactored_no_model_change", _source_semantic_key),
        ("same_semantic_loader_role", "refactored_no_model_change", _loader_semantic_key),
    )
    for relation, decision, key_function in stages:
        for old_key, new_key in _unique_groups(
            old,
            new,
            remaining_old,
            remaining_new,
            key_function,
        ):
            matches.append(
                _match_row(pair_key, old[old_key], new[new_key], relation, decision)
            )
            remaining_old.remove(old_key)
            remaining_new.remove(new_key)

    for hunk in sorted(diff_hunks, key=lambda row: row.hunk_id):
        old_sources = {
            str(old[key].get("candidate_key", ""))
            for key in remaining_old
            if _binding_in_hunk(old[key], hunk, "old")
        }
        new_sources = {
            str(new[key].get("candidate_key", ""))
            for key in remaining_new
            if _binding_in_hunk(new[key], hunk, "new")
        }
        old_sources.discard("")
        new_sources.discard("")
        if len(old_sources) != 1 or len(new_sources) != 1:
            continue
        old_candidates = [
            key
            for key in remaining_old
            if str(old[key].get("candidate_key", "")) in old_sources
        ]
        new_candidates = [
            key
            for key in remaining_new
            if str(new[key].get("candidate_key", "")) in new_sources
        ]
        if len(old_candidates) != 1 or len(new_candidates) != 1:
            continue
        old_key = old_candidates[0]
        new_key = new_candidates[0]
        if old[old_key].get("canonical_model_id") != new[new_key].get(
            "canonical_model_id"
        ):
            continue
        relocated = old[old_key].get("source_logical_file_path_id") != new[
            new_key
        ].get("source_logical_file_path_id")
        decision = (
            "relocated_no_model_change" if relocated else "refactored_no_model_change"
        )
        matches.append(
            _match_row(
                pair_key,
                old[old_key],
                new[new_key],
                "unique_diff_hunk",
                decision,
                additional_evidence=(f"diff_hunk:{hunk.hunk_id}",),
            )
        )
        remaining_old.remove(old_key)
        remaining_new.remove(new_key)

    turnover_groups: list[dict[str, object]] = []
    turnover_by_binding: dict[str, str] = {}
    old_by_model = _remaining_by_model(old, remaining_old)
    new_by_model = _remaining_by_model(new, remaining_new)
    for model_id in sorted(set(old_by_model) & set(new_by_model)):
        old_keys = sorted(old_by_model[model_id])
        new_keys = sorted(new_by_model[model_id])
        group_id = structural_fingerprint(
            {
                "schema": CONTINUITY_SCHEMA,
                "pair_key": pair_key,
                "category": "site_turnover_no_model_change",
                "canonical_model_id": model_id,
                "old_binding_keys": old_keys,
                "new_binding_keys": new_keys,
            }
        )
        turnover_groups.append(
            {
                "schema": CONTINUITY_SCHEMA,
                "record_type": "same_id_site_turnover_group",
                "site_turnover_group_id": group_id,
                "pair_key": pair_key,
                "canonical_model_id": model_id,
                "category": "site_turnover_no_model_change",
                "pairing_status": "unresolved_no_explicit_correspondence",
                "old_binding_keys": old_keys,
                "new_binding_keys": new_keys,
                "old_binding_count": len(old_keys),
                "new_binding_count": len(new_keys),
                "reason": (
                    "confirmed bindings disappear and appear for the same model ID, but no "
                    "unique structural or diff-hunk relationship proves relocation/refactoring"
                ),
            }
        )
        for key in (*old_keys, *new_keys):
            turnover_by_binding[key] = group_id

    residuals = [
        _residual_row(pair_key, "old", old[key], turnover_by_binding.get(key, ""))
        for key in sorted(remaining_old)
    ] + [
        _residual_row(pair_key, "new", new[key], turnover_by_binding.get(key, ""))
        for key in sorted(remaining_new)
    ]
    return SameIdContinuityResult(
        matches=tuple(sorted(matches, key=lambda row: str(row["continuity_key"]))),
        site_turnover_groups=tuple(
            sorted(turnover_groups, key=lambda row: str(row["site_turnover_group_id"]))
        ),
        residual_bindings=tuple(
            sorted(
                residuals,
                key=lambda row: (str(row["side"]), str(row["confirmed_binding_key"])),
            )
        ),
    )


def read_diff_hunks(repo_dir: Path, old_sha: str, new_sha: str) -> tuple[DiffHunk, ...]:
    """Read zero-context Python diff hunks for explicit source correspondence."""
    payload = run_git(
        repo_dir,
        [
            "diff",
            "--unified=0",
            "--find-renames",
            "--no-color",
            "--no-ext-diff",
            old_sha,
            new_sha,
            "--",
            "*.py",
        ],
    ).decode("utf-8", errors="replace")
    old_path = ""
    new_path = ""
    hunks: list[DiffHunk] = []
    for line in payload.splitlines():
        path_match = re.match(r"diff --git a/(.+) b/(.+)$", line)
        if path_match:
            old_path, new_path = path_match.groups()
            continue
        hunk_match = re.match(
            r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
            line,
        )
        if not hunk_match or not old_path or not new_path:
            continue
        old_start, old_count, new_start, new_count = hunk_match.groups()
        hunks.append(
            DiffHunk(
                hunk_id=f"{old_path}:{old_start}->{new_path}:{new_start}",
                old_path=old_path,
                new_path=new_path,
                old_start=int(old_start),
                old_count=int(old_count) if old_count is not None else 1,
                new_start=int(new_start),
                new_count=int(new_count) if new_count is not None else 1,
            )
        )
    return tuple(sorted(hunks, key=lambda row: row.hunk_id))


def _binding_map(
    rows: Sequence[Mapping[str, object]], side: str
) -> dict[str, Mapping[str, object]]:
    output: dict[str, Mapping[str, object]] = {}
    for row in rows:
        key = str(row.get("confirmed_binding_key", ""))
        if not key:
            raise ValueError(f"{side} binding is missing confirmed_binding_key")
        if key in output:
            raise ValueError(f"duplicate {side} confirmed binding key: {key}")
        output[key] = row
    return output


def _unique_groups(
    old: Mapping[str, Mapping[str, object]],
    new: Mapping[str, Mapping[str, object]],
    remaining_old: set[str],
    remaining_new: set[str],
    key_function: Callable[[Mapping[str, object]], tuple[object, ...] | None],
) -> tuple[tuple[str, str], ...]:
    old_groups: dict[tuple[object, ...], list[str]] = defaultdict(list)
    new_groups: dict[tuple[object, ...], list[str]] = defaultdict(list)
    for key in sorted(remaining_old):
        group = key_function(old[key])
        if group is not None:
            old_groups[group].append(key)
    for key in sorted(remaining_new):
        group = key_function(new[key])
        if group is not None:
            new_groups[group].append(key)
    pairs = []
    for group in sorted(set(old_groups) & set(new_groups), key=repr):
        if len(old_groups[group]) == 1 and len(new_groups[group]) == 1:
            pairs.append((old_groups[group][0], new_groups[group][0]))
    return tuple(pairs)


def _model_key(row: Mapping[str, object], value: object) -> tuple[object, ...] | None:
    model_id = str(row.get("canonical_model_id", ""))
    text = str(value or "")
    return (model_id, text) if model_id and text else None


def _binding_anchor_key(row: Mapping[str, object]) -> tuple[object, ...] | None:
    return _model_key(row, row.get("site_binding_anchor_key"))


def _source_anchor_key(row: Mapping[str, object]) -> tuple[object, ...] | None:
    return _model_key(row, row.get("source_anchor_key"))


def _loader_anchor_key(row: Mapping[str, object]) -> tuple[object, ...] | None:
    return _model_key(row, row.get("loader_anchor_key"))


def _source_semantic_key(row: Mapping[str, object]) -> tuple[object, ...] | None:
    values = (
        row.get("source_logical_file_path_id"),
        row.get("source_class_context"),
        row.get("source_function_context"),
        row.get("source_ast_context"),
        row.get("source_carrier_kind"),
        row.get("source_carrier_name"),
        row.get("source_carrier_container"),
    )
    return _semantic_key(row, values)


def _loader_semantic_key(row: Mapping[str, object]) -> tuple[object, ...] | None:
    values = (
        row.get("loader_logical_file_path_id"),
        row.get("loader_class_context"),
        row.get("loader_function_context"),
        row.get("model_role"),
    )
    return _semantic_key(row, values)


def _combined_semantic_key(row: Mapping[str, object]) -> tuple[object, ...] | None:
    source = _source_semantic_key(row)
    loader = _loader_semantic_key(row)
    return (*source, *loader[1:]) if source is not None and loader is not None else None


def _semantic_key(
    row: Mapping[str, object], values: tuple[object, ...]
) -> tuple[object, ...] | None:
    model_id = str(row.get("canonical_model_id", ""))
    normalized = tuple(str(value or "") for value in values)
    return (model_id, *normalized) if model_id and any(normalized) else None


def _match_row(
    pair_key: str,
    old: Mapping[str, object],
    new: Mapping[str, object],
    relation: str,
    decision: str,
    *,
    additional_evidence: tuple[str, ...] = (),
) -> dict[str, object]:
    old_key = str(old["confirmed_binding_key"])
    new_key = str(new["confirmed_binding_key"])
    evidence = [relation, *additional_evidence]
    if old.get("source_resolved_file_path") != new.get("source_resolved_file_path"):
        if old.get("source_logical_file_path_id") == new.get(
            "source_logical_file_path_id"
        ):
            evidence.append("git_file_rename_continuity")
        else:
            evidence.append("source_path_changed")
    if old.get("source_line_number") != new.get("source_line_number"):
        evidence.append("source_line_moved")
    continuity_key = structural_fingerprint(
        {
            "schema": CONTINUITY_SCHEMA,
            "pair_key": pair_key,
            "old_binding_key": old_key,
            "new_binding_key": new_key,
            "relation": relation,
        }
    )
    return {
        "schema": CONTINUITY_SCHEMA,
        "record_type": "same_id_binding_continuity",
        "continuity_key": continuity_key,
        "pair_key": pair_key,
        "canonical_model_id": old.get("canonical_model_id", ""),
        "decision": decision,
        "relation": relation,
        "confidence": "high",
        "evidence": sorted(set(evidence)),
        "old_confirmed_binding_key": old_key,
        "new_confirmed_binding_key": new_key,
        "old_candidate_key": old.get("candidate_key", ""),
        "new_candidate_key": new.get("candidate_key", ""),
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
            for field in ("loader_anchor_key", "resolved_callee", "receiver_origin", "model_role")
        ),
    }


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


def _remaining_by_model(
    rows: Mapping[str, Mapping[str, object]], remaining: set[str]
) -> dict[str, list[str]]:
    output: dict[str, list[str]] = defaultdict(list)
    for key in sorted(remaining):
        output[str(rows[key].get("canonical_model_id", ""))].append(key)
    return output


def _residual_row(
    pair_key: str,
    side: str,
    row: Mapping[str, object],
    turnover_group_id: str,
) -> dict[str, object]:
    return {
        "schema": CONTINUITY_SCHEMA,
        "record_type": "same_id_residual_binding",
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
        "residual_reason": (
            "same_id_site_turnover_member"
            if turnover_group_id
            else f"unmatched_{side}_confirmed_binding"
        ),
    }


def _pair_key(pair: Mapping[str, object]) -> str:
    return f"{pair.get('old_release_row_id', '')}->{pair.get('new_release_row_id', '')}"
