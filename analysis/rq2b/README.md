# RQ2b: PTM changes across releases

This folder reproduces PTM change detection, comparison with the existing method's PTM-ID counts, and the analysis of integration changes with stable counts. It starts from saved MIST bindings and reviewed annotations.

## Run

Use Python 3.10 or later. From this directory:

```bash
python3 -m pip install -r requirements.txt
python3 change_detection/detect.py --database /path/to/ptm_validation_population/ptm_reuse.sqlite --output reproduced_changes --verify
python3 analyze.py --database /path/to/ptm_validation_population/ptm_reuse.sqlite --changes reproduced_changes/ptm_changes.csv --output reproduced
python3 integration.py --database /path/to/ptm_validation_population/ptm_reuse.sqlite --output reproduced
```

> [!IMPORTANT]
> Use the full `ptm_validation_population` database. Start with new or empty output directories; the integration command then adds its files alongside the comparison results without overwriting them. The confirmed-downstream database has a smaller scope. Source archives and graphs are not needed for these commands.

The scripts repeat matching and analysis, but do not rerun MIST, integration screening, or manual review. Omit `--changes` to analyze the database's stored changes directly. Use `--counts path/to/counts.csv` to supply another existing-method count file.

## Inputs

| File in `inputs/` | Purpose |
| --- | --- |
| `binding_structure.jsonl.gz` | Structural anchors and source/sink context for historical bindings |
| `diff_hunks.jsonl.gz` | Python diff locations at fixed release commits |
| `ptm_id_components.xlsx` | Reviewed provider, lineage, variant, generation, version, revision, release status, and suffix for each PTM ID |
| `reviewed_changes.csv` | Confirmed manual change decisions and supporting links |
| `existing_method_counts.csv` | Existing-method counts for the visibility comparison |
| `raw_ptm_counts.csv` | Occurrence counts for selecting stable-count release pairs |
| `integration_screening.csv` | Resolved screening outcomes and subsequent review outcomes |
| `integration_change_annotations.xlsx` | Original annotation workbook, including reviewed decisions, code definitions, and result sheets |
| `evidence.html` | Offline code evidence for the 458 annotated binding pairs |
| `integration_locations.csv` | Reviewed affected files and procedures |

Count files use `release_id,ptm_id,count` and include nonzero counts only. They cover all 6,433 releases in 411 repositories; missing entries mean zero within this scope. They count PTM IDs, not bindings. See the [coding guide](coding_guide.md) for integration annotations.

Open [evidence.html](inputs/evidence.html) in a browser to inspect before/after binding paths, code edits, complete files, and fixed GitHub comparisons. Its case IDs match the workbook's `review_decisions` sheet. No server is needed; annotations remain in the workbook. The analysis reads `review_decisions` and the definitions in `taxonomy`; it recalculates results rather than reading the workbook's result totals.

## Analysis

`change_detection/detect.py` matches same-ID bindings using unique structural evidence and Git diff hunks, then proposes replacements between different IDs. Changes in provider, product lineage, or functional variant mean migration; other differences mean update. Reviewed decisions confirm replacements, resolve ambiguous groups, and override conflicting automatic matches. Unpaired bindings without competing matches become additions or removals; unresolved cases and unreviewed replacements remain separate. With `--verify`, it compares exact change types, endpoints, and continuing matches with the database, restoring its IDs only after all results agree.

`analyze.py` measures visibility in the existing method's counts. Additions and removals need one matching count increase or decrease. Replacements need both sides for full visibility, one for partial visibility, and neither for no visibility. Each count unit is used once, prioritizing replacements with both sides, then one side, then additions and removals. IDs break ties. Visibility shows compatible count evidence, not proof of the same reuse site.

`integration.py` uses saved screening outcomes and reviews for continuing bindings with unchanged repository-wide PTM-ID counts. It reports change frequency among valid reviewed pairs with code edits, not all continuing bindings. Categories overlap; those below 10% of changed release pairs are grouped as `Other`. Breadth counts distinct affected path files and procedures against all locations present in either release, counting shared or continuing locations once per release pair. Module-level edits affect files only; edits outside the measured path do not increase breadth.

## Results

The detector reproduces 3,154 PTM changes and 18,524 continuing binding pairs. It writes change records, endpoint links, continuing matches, replacement proposals, unresolved bindings, and the generated `model_transition.csv`, plus `summary.json`.

`results/` contains change-type totals, visibility labels, release-pair comparisons, and the sunburst chart. The existing method fully shows 309 changes and partly shows two migrations. MIST identifies 706 changed release pairs, compared with 143 for the existing method.

`results/integration_results.xlsx` contains four sheets: `breadth`, `categories_grouped`, `categories`, and `release_pairs`. The summary is in `results/integration_summary.json`. Of 458 valid reviews, 134 contain integration changes (29.3%), spanning 74 release pairs in 49 repositories. Affected file histories total 85/107; procedure histories total 106/199. Shares use 0–1; a blank procedure share means no path procedures.

To redraw the chart:

```bash
python3 plot.py --counts results/change_types.csv --output /path/to/event_visibility
```

To generate only the transition table from the reviewed `components` sheet:

```bash
python3 change_detection/transitions.py --output /path/to/model_transition.csv
```
