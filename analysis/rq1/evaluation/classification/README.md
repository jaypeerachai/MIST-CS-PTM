# Reuse classification

This folder compares PTM reuse classification by MIST, Jiang et al. (PeaTMOSS), and Banyongrakkul et al. (TSE). `benchmark.xlsx` contains 328 occurrences from 86 repositories (93 reuse). `unseen_holdout.xlsx` contains 485 occurrences from 100 repositories (81 reuse), with one sampled file per repository.

Both workbooks contain `annotations`, `predictions`, and `repositories` sheets. Case IDs link labels to predictions; `real_reuse` is positive. Repository records include fixed commits and sampled files.

## Run

From this folder, using Python 3.10:

```bash
python -m pip install -r ../requirements.txt
python evaluate.py --dataset benchmark
python evaluate.py --dataset unseen_holdout
```

`python evaluate.py` scores both datasets from their workbook predictions without rerunning tools. Add `--bootstrap` to the holdout command for paired bootstrap intervals. See the [evaluation README](../README.md) to rerun MIST or the baselines.

## Results

P is precision, R is recall, and F1 is their harmonic mean.

| Dataset | Method | Occurrence P / R / F1 | Repository P / R / F1 |
| --- | --- | --- | --- |
| Benchmark | MIST | 1.000 / 0.839 / 0.912 | 1.000 / 0.818 / 0.900 |
| Benchmark | Jiang et al. | 0.378 / 0.151 / 0.215 | 0.500 / 0.061 / 0.108 |
| Benchmark | Banyongrakkul et al. | 0.424 / 0.151 / 0.222 | 0.667 / 0.061 / 0.111 |
| Unseen holdout | MIST | 0.928 / 0.790 / 0.853 | 0.900 / 0.643 / 0.750 |
| Unseen holdout | Jiang et al. | 0.125 / 0.012 / 0.022 | 0.250 / 0.036 / 0.063 |
| Unseen holdout | Banyongrakkul et al. | 1.000 / 0.012 / 0.024 | 1.000 / 0.036 / 0.069 |

MIST finds 64 of 81 holdout reuse occurrences and 48 of 62 non-local benchmark bindings; each baseline finds one in either set. TSE's holdout precision rests on one positive prediction. Repository scores cover only the annotated files.