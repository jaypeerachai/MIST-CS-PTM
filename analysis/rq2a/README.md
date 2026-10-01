# RQ2a: PTM integration

This analysis counts access interfaces and binding locality in the repository snapshots.

## Run

Use Python 3.10 or later. From this directory:

```bash
python3 -m pip install -r requirements.txt
python3 analyze.py --database /path/to/ptm_reuse.sqlite --output reproduced
```

> [!IMPORTANT]
> The output directory must be new or empty. The database export is described in [database/README.md](../../database/README.md). `interface_mapping.csv` contains the reviewed assignments of 18 interface origins to 4 families. To use another mapping, pass `--mapping path/to/interface_mapping.csv`. Unmapped origins and missing locality flags stop the analysis.

## Results

`results/` contains the summaries and plots for 2,457 bindings in 450 repositories.

| File | Contents |
| --- | --- |
| `interface_families.csv` | Binding and repository counts for each family, including locality |
| `interface_origins.csv` | The same counts for each concrete origin |
| `model_authors.csv` | Counts and interface variety for each PTM author |
| `author_interfaces.csv` | PTM author, family, and origin combinations used in the Sankey plot |
| `repositories.csv` | Interface mixing and locality in each repository |
| `locality.csv` | Local bindings, each boundary type, and their non-local total |
| `summary.json` | Main totals and interface overlap |
| `interfaces.pdf`, `interfaces.svg` | PTM author to interface family to interface origin |
| `locality.pdf`, `locality.svg` | Local and non-local reuse at binding and repository levels |

Each retained snapshot binding counts once. PTM author comes from the canonical ID prefix.  Interface origin comes from the saved import origin. All shares are fractions from 0 to 1. Binding shares use all 2,457 bindings, and repository shares use the 450 repositories with at least one binding. Repository counts can overlap across interfaces and authors. `non_local_binding_share` uses the bindings in that row. `share_within_origin` uses all bindings with that interface origin.

A binding is non-local if it crosses a file or procedure boundary. `locality.csv` reports the four separate combinations, followed by their non-local total. A repository can appear in several rows, but the plot assigns it to non-local if it has at least one non-local binding, and to local only otherwise. These are the saved path flags, not a new calculation from endpoint locations. The Sankey uses binding counts. It groups small origins within their family for readability, while the CSVs keep all 18 origins.
