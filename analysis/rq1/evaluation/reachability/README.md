# Reachability

We compare MIST, Drosos/PyCG, and Yasmin et al. on 93 reuse cases from 33 repositories. Each method starts from the same annotated PTM-use sink. Success means returning the PTM ID source file, not recovering every binding step.

## Files and commands

- `cases.csv`: annotated sources, sinks, and path files. Case IDs match the binding codebooks; `benchmark_case_id` links to classification.
- `results.csv`: returned files, function counts, and outcomes. MIST's function counts are blank because it does not return a separate function set.
- `modules.csv.gz`: module names and files used to interpret graph nodes.

From this folder, using Python 3.10, recalculate recovery and returned-file counts:

```bash
python evaluate.py
```

| Method | Source files recovered | Median returned files |
| --- | ---: | ---: |
| Drosos/PyCG | 20/93 (21.5%) | 0 |
| Yasmin et al. | 76/93 (81.7%) | 19 |
| MIST | 80/93 (86.0%) | 2 |

Failures, timeouts, and unmatched sinks count as misses with zero files for scoring. Partial outputs remain in `results.csv` for reference. These misses explain Drosos/PyCG's zero median.

To recalculate MIST from the benchmark graphs in the [Figshare dataset (private review link)](https://figshare.com/s/7cc2423e7aae7520888d):

```bash
python evaluate.py --graphs /path/to/rq1_validation_evidence/benchmark --output /path/to/new-reachability.csv
```

The reader also accepts newly generated `--full` results and plain or compressed graph CSVs. This recalculates only MIST; baseline results remain unchanged. To rerun the baselines, see [../baselines/README.md](../baselines/README.md), then score the new file with `python evaluate.py --results /path/to/results.csv`.

## Operational metrics

`operational_metrics.csv` records completion, time, and memory for each method and repository (99 records). `operational_environment.json` contains the hardware and run settings. Summarize the saved measurements:

```bash
python evaluate_operational.py
```

Add `--output /path/to/summary.csv` to export the summary. Both scorers use saved data without rerunning tools or needing additional libraries.

| Method | Completed repositories | Wall time | Peak RSS |
| --- | ---: | ---: | ---: |
| Drosos/PyCG | 22/33 | 48 h 19 min 14 s | 12.03 GiB |
| Yasmin et al. | 33/33 | 20 min 39 s | 8.27 GiB |
| MIST | 33/33 | 12 min 52 s | 4.00 GiB |

Methods ran sequentially on the same host with two repository workers, a 12-hour timeout, and a 12-GiB memory limit per repository. Timings include preprocessing and come from one controlled run, not a new run of the packaged tool. Wall time covers the whole run, including failures. Peak RSS is the largest repository memory peak, not the combined memory of both workers. The script also reports time ranges and returned context. Drosos/PyCG's context summary uses its 31 cases with completed graphs and matched sinks; source recovery above includes all 93 cases.
