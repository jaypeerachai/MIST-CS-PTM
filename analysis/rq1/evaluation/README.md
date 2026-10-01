# Evaluation (RQ1)

RQ1 has two checks: classifying real PTM reuse and recovering its source file from an annotated PTM-use sink.

| Folder | Cases | Repositories | Purpose |
| --- | ---: | ---: | --- |
| `classification/benchmark.xlsx` | 328 | 86 | Development benchmark, including 93 reuse cases |
| `classification/unseen_holdout.xlsx` | 485 | 100 | Evaluation on additional repositories |
| `reachability/` | 93 | 33 | The positive benchmark cases, with the same sink supplied to each method |

The workbooks contain reviewed annotations, predictions for each method, and fixed repository commits. See [classification/README.md](classification/README.md) for the classification commands and results.

## Score the results

From this folder, using Python 3.10:

```bash
python -m pip install -r requirements.txt
python classification/evaluate.py
python reachability/evaluate.py
python reachability/evaluate_operational.py
```

The shared script calculates classification scores directly from the workbooks. `--dataset benchmark` or `--dataset unseen_holdout` selects one dataset. Use `--predictions /path/to/results/mist_predictions.csv` to score a new MIST run.

## Rerun MIST

First install MIST in its pinned environment as described in `../../../mist/README.md`. The scripts read repository names and commits from the workbooks. Download fixed checkouts into a separate directory:

```bash
python fetch_repositories.py --output /path/to/snapshots
```

Then analyze them and score the results:

```bash
python run_mist.py --model-ids /path/to/ptm_ids.csv \
  --snapshots /path/to/snapshots --output /path/to/results
python classification/evaluate.py --predictions /path/to/results/mist_predictions.csv
```

Both commands accept `--dataset` and `--repository`. Start with one repository if desired. Checkouts use `owner__repo/full_commit`; result folders use `owner__repo_commitprefix`, for example `d3fq0n1__maestro-orchestrator_753c694acc5b`.

New runs write `mist_predictions.csv` in your output directory. This is an output for scoring, not an additional input shipped with the package.

Supply your vocabulary through `--model-ids`. It must be a CSV with a `model_id` column and is used for every repository selected in that command. Existing results must match its hash before they can be reused. The scripts match occurrences by file, line, column, and PTM ID, not just by string or generated occurrence number.

The included predictions used 199 IDs for the benchmark and the first 50 holdout repositories (cases 001–254), and 427 for the remaining 50 (cases 255–485). Use the corresponding vocabulary when reproducing those runs, selecting individual repositories with `--repository` if needed. A different vocabulary may change the results or omit annotated occurrences. Scoring the included predictions does not require a vocabulary file.

## Recalculate reachability

Run the benchmark with `--full`, then provide those graphs to the reachability script:

```bash
python run_mist.py --dataset benchmark --full --model-ids /path/to/ptm_ids.csv \
  --snapshots /path/to/snapshots --output /path/to/full-results
python reachability/evaluate.py --graphs /path/to/full-results \
  --output /path/to/reachability-results.csv
```

The graph calculation gives MIST the same annotated sinks used for the baseline comparisons. It is separate from classification, where MIST finds its own sinks. Recalculation changes only MIST's returned file sets; baseline sets remain saved inputs.

## Rerun the baselines

See [baselines/README.md](baselines/README.md) for the original source files, pinned environment, and input wrappers. PeaTMOSS and TSE classify the annotated occurrences. Drosos/PyCG and Yasmin start from the annotated sinks and return caller context. Keep their environment separate from MIST's.

Score new classification predictions with `--prediction-method peatmoss` or `--prediction-method tse` alongside `--predictions`. The default remains MIST. New caller results can be passed to `reachability/evaluate.py --results`. Neither command changes the supplied workbooks or results.

## Scope

This folder includes classification and reachability data, scoring, holdout bootstrap comparisons, fixed-checkout collection, MIST and baseline runners, and operational metrics from the controlled reachability run. The original baseline files are separate from our input and task adapters.
