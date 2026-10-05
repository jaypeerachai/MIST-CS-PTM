# PTM reuse database

The database links saved reuse decisions and PTM changes to repository commits and code. It is an input for the RQ2 analysis. Integration-change annotations and breadth measurements are kept separately.

The database contains the saved study results.

The single `ptm_database` export contains three study scopes:

| Scope | Repositories | Records |
| --- | ---: | --- |
| Population | 1,219 | Repository snapshots and validation outcomes |
| RQ2a | 450 | 2,457 confirmed snapshot bindings |
| RQ2b | 411 | 575 release lines, 6,433 releases, and 5,858 adjacent-release pairs |

The historical records contain 23,626 retained bindings and 3,154 PTM changes. Snapshot and historical binding counts describe separate analyses and should not be added as unique reuse sites. Use snapshot bindings for RQ2a and all saved release pairs for RQ2b, including releases without bindings.

## Files

`schema.sql` defines the tables. `show_code.py` reads the saved source code using Python 3.9 or later, with no extra libraries.

The exported `data_dictionary.md` describes each table's fields, keys, links, and missing values. Keep the exported data together:

```text
ptm_reuse.sqlite
summary.json
data_dictionary.md
evidence/
  sources/       # one archive per repository
  graphs/        # available graph exports
  validation/    # saved decisions and paths
```

The database stores records and relative evidence paths. Source archives contain Python files and root license notices from the exact commits. Identical file contents are stored once per repository, under their Git blob hash. The `files` table links them to each commit and path. Symbolic links are stored as link targets and are not followed. These archives are not complete runnable repositories. Third-party code retains its original license.

## Read saved code

Run from this directory, replacing `DATA` with the exported data directory. This reads code without extracting or running it:

```bash
python3 show_code.py --database DATA/ptm_reuse.sqlite --evidence-root DATA \
  --repository digiteinfotech/kairon \
  --commit 84bde0900e94f0664eef23113a2fd94e0614ea37 \
  --file kairon/shared/llm/processor.py --start 33 --end 46
```

## Main tables

| Records | Tables |
| --- | --- |
| Repositories and code versions | `repositories`, `snapshots`, `releases`, `files` |
| Reuse decisions and paths | `analyses`, `occurrences`, `bindings`, `binding_steps`, `binding_locations` |
| Snapshot binding locality | `binding_locality` |
| Changes across releases | `release_pairs`, `binding_matches`, `ptm_changes`, `change_bindings`, `unmatched_bindings` |
| Evidence files | `artifacts` |

`repositories` contains all 1,219 population repositories. `bindings` contains the bindings retained for analysis after applying the saved review decisions. Original tool detections remain in the validation evidence. `binding_records` adds repository, commit, PTM, source, sink, and interface fields. The existing `confirmed_repositories` view selects repositories with at least one retained binding and returns 450 repositories in this dataset. For example:

```sql
SELECT r.repository, r.commit_sha, r.ptm_id, r.source_path, r.source_line,
       r.sink_path, r.sink_line, r.interface_origin
FROM binding_records r
JOIN bindings b USING(binding_id)
JOIN analyses a USING(analysis_id)
WHERE a.analysis_type = 'snapshot';

SELECT category, old_ptm_id, new_ptm_id, existing_method_visibility
FROM ptm_changes;
```

## Reading the records

- A snapshot is a repository at one commit. A release links a tag to that snapshot. `analyses` connects each result to its snapshot and identifies the analysis type: `snapshot` for the initial analysis or `release` for the release history. Only historical analyses have a `release_id`. The same commit may appear in both, so select the analysis type before counting. Repository metadata reflects its collection date, not the release date.
- Drafts and prereleases were excluded during collection filtering. Their original flags remain in the collection data and are omitted from `releases`.
- `release_url` links to the GitHub release page. It is generated from the saved repository URL and tag, with special characters encoded. The snapshot commit identifies the analyzed code even if a tag later moves.
- `occurrences` contains candidates submitted for validation. `confirmed_reuse = 1` means the occurrence has at least one binding retained in this database. A zero means no retained binding, not necessarily a manual non-reuse label. Saved tool outcomes and paths remain in validation evidence. These paths contain only the connections recovered by the tool.
- Snapshot validation files contain `decisions`, `source_contexts`, `calls`, `traces`, and `trace_steps`. Decisions also include occurrences screened out before tracing, which are absent from `occurrences`. `binding_allowed` and `binding_eligible` record whether an occurrence or call qualified for tracing. Neither confirms reuse. Unresolved and excluded outcomes remain separate from non-reuse categories.
- Release validation files contain `decisions`, `bindings`, and `mock_checks`. Their `confirmed_reuse` flag records the saved tool outcome before manual exclusions, while SQLite records the bindings retained for analysis. Each decision's `binding_ids` links to `confirmed_binding_key` in the saved bindings. These bindings retain their trace steps and structural matching information.
- Lines are one-based. Occurrence columns are one-based and inclusive. Saved sink columns are zero-based byte offsets. Missing columns stay `NULL`.
- `binding_steps` contains ordered path edges. Their text explanations remain in the validation evidence files. `binding_locations` contains available node locations matched to that exact commit and path. Not every historical path has these details.
- `binding_locality` contains one row per snapshot binding, with `cross_file` and `cross_procedure` flags. Historical bindings have no row because their locality flags were not saved. Missing locality is not treated as local reuse.
- `binding_matches` contains continuing same-ID bindings. `ptm_changes` retains the saved addition, removal, update, or migration label. `change_bindings` links each change to its endpoints. `unmatched_bindings` preserves unresolved endpoints rather than treating them as unchanged or inventing a change type.
- Raw PTM-ID occurrence counts and the existing method's counts are separate [RQ2b input CSVs](../analysis/rq2b/README.md), not database tables. They cover the full study population, including releases without confirmed reuse.
- Missing values stay `NULL`. Historical records lack full graph files. Ordered binding paths are included, but they are not the full graph. Available graph files are listed in `artifacts` with `kind = 'full_graph'`.
- Each validation evidence file includes `analysis_metadata` with the saved graph details and available library versions. Empty environment information means versions were not recorded in the export. These details do not determine reuse. The database includes completed analyses only.

Use `analysis_type = 'release'` for historical bindings. Keep all saved releases and release pairs when reproducing RQ2b, including those without bindings.
