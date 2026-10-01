# Baseline runs

The authors' files are kept unchanged in `upstream/`. The links below identify the source commits.

| Method | Original code | What the wrapper does |
| --- | --- | --- |
| Jiang / PeaTMOSS | [upstream/peatmoss/](https://github.com/PurdueDualityLab/PeaTMOSS-Artifact/tree/d16dc8b6abcdac1e9bed0eff6f2d5b47243ca405) | Loads the customized Scalpel module, supplies loader signatures, and matches extracted arguments to the annotated PTM ID in the same file |
| Banyongrakkul / TSE | [upstream/tse/](https://github.com/jaypeerachai/TSE_PTM_Changes/tree/a7b02f20d347f950574cb7a8f3b3184c4922f3cf) | Supplies local files and signatures without the original database, retaining literal resolution, argument filters, and path exclusions |
| Drosos / PyCG | [upstream/drosos/](https://github.com/gdrosos/bloat-study-artifact/tree/0fe2fe54fe18a908b8377a473377425b88a21968) and [upstream/pycg/](https://github.com/gdrosos/PyCG/tree/42e54d3ed6e50f7be0cc3aa8b65f25140a86e25b) | Builds the project graph with the pinned PyCG fork, then follows reverse internal calls from the annotated sink |
| Yasmin | [upstream/yasmin/](https://github.com/RISElabQueens/PTMReuseInOSS/tree/16d1603194bfa092dd451fcbf00046f0499b093a) | Adapts the automatic depth-six caller search to the supplied repositories and sinks |

The Yasmin entry script imports a module absent from its repository and expects project-specific CSVs and Scalpel changes. `yasmin.py` supplies that missing setup and preserves the caller-search procedure used in our evaluation. The untouched files are included for reference. This is an adaptation, not direct execution of the released script or its manual analysis. For Drosos, the graph construction uses the authors' fork unchanged. Reverse caller tracing is our adaptation to the common sink-based task. Yasmin searches tracked Python files, including those directories. Neither caller baseline makes an automatic reuse decision.

## Environment

Use Python 3.10 on Linux, with Git available. Install these requirements in a separate environment from MIST:

```bash
python3.10 -m venv /path/to/baseline-env
/path/to/baseline-env/bin/python -m pip install -r requirements.txt
```

Commands below run from this folder using that environment. Do not install the target projects' dependencies or execute their code.

## Reuse classification

Download fixed checkouts with `../fetch_repositories.py`. Then run each method separately:

```bash
python run_classification.py --method peatmoss --dataset benchmark \
  --snapshots /path/to/snapshots --output /path/to/results/peatmoss
python run_classification.py --method tse --dataset benchmark \
  --snapshots /path/to/snapshots --output /path/to/results/tse
```

Use `--dataset unseen_holdout` for the holdout or `all` for both. `--annotations` accepts another workbook in the same format. `--reuse-codebook` accepts another loader-signature workbook and defaults to `../../codebooks/reuse_codebook.xlsx`. The annotations supply the occurrences to evaluate, not the decisions. These two baselines use the annotated PTM IDs and loader signatures, so they do not need a separate vocabulary file.

Each output contains a predictions CSV with case IDs, decisions, analyzer status, and matched call lines. PeaTMOSS also creates its original extraction log. Missing checkouts, wrong commits, edited tracked files, and missing input files stop the run. Analyzer errors remain visible in the status column, with the same prediction rule as the evaluation.

Score a complete dataset from the parent evaluation folder, using its scoring environment:

```bash
python classification/evaluate.py --dataset benchmark --prediction-method peatmoss \
  --predictions /path/to/results/peatmoss/peatmoss_predictions.csv
python classification/evaluate.py --dataset benchmark --prediction-method tse \
  --predictions /path/to/results/tse/tse_predictions.csv
```

## Caller tracing

```bash
python run_reachability.py --method drosos --snapshots /path/to/snapshots \
  --output /path/to/results/drosos
python run_reachability.py --method yasmin --snapshots /path/to/snapshots \
  --output /path/to/results/yasmin
```

Both commands read `../reachability/cases.csv`. `--cases` accepts another file in the same format. The defaults are two repository workers, a 12-hour timeout, and a 12-GiB process-tree RSS limit per repository. Change them with `--workers`, `--timeout`, or `--memory-gib`. Yasmin's depth is six, with no extra per-case timeout. Drosos uses fixpoint graph construction and unbounded reverse traversal. Outputs include `results.csv`, per-repository results and logs, and `runtime.csv`. Drosos also retains each generated graph. Failed and timed-out repositories remain in the result table as misses, not omitted cases.

Score a complete 93-case run from the parent evaluation folder:

```bash
python reachability/evaluate.py --results /path/to/results/drosos/results.csv
python reachability/evaluate.py --results /path/to/results/yasmin/results.csv
```

For a small check, both runners accept `--repository owner/repo` and repeatable `--case-id`. Instead of `--snapshots`, use `--checkout /path/to/repo` with `--repository` for one existing fixed checkout. Always choose a new output directory. A filtered run is not a complete benchmark result.

## Attribution

The original license files are included where present. The pinned TSE and Yasmin repositories contain no top-level license file. Their source remains attributed to the authors and is not covered by any license for our wrappers. Check redistribution permissions before publishing those copies.
