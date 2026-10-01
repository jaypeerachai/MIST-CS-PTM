# GitHub repository collection

This folder includes both GitHub collection and reproduction of the saved study data. Both use the 199 discovery IDs from `../ptm_catalogue`. A search match is a candidate, not a confirmed PTM binding.

## Reproduce the study data

From this folder, using Python 3.10 or later:

```bash
python reproduce.py --output-dir /tmp/mist-repositories
```

No extra packages, GitHub token, or database are needed. A new GitHub search will not reproduce the historical results because repositories and the search index change.

## Collect from GitHub

`collect.py` does the online collection. It uses Python's standard library and Git. Set `GITHUB_TOKEN` in your environment, with permission to search code and read the repositories.

Start with one discovery ID and stop after searching:

```bash
python collect.py --output-dir /tmp/mist-github-trial --limit-models 1 --stop-after search
```

Then continue that trial through the remaining stages:

```bash
python collect.py --output-dir /tmp/mist-github-trial --limit-models 1 --resume
```

For all 199 IDs, use a separate output directory and omit `--limit-models`:

```bash
python collect.py --output-dir /path/to/mist-github-collection
```

> [!WARNING]
> The full collection makes many requests and may take days.

> [!IMPORTANT]
> Requests run one at a time, with rate-limit waits. Use `--resume` with the same options after an interruption. Successful responses are cached, so they are not fetched again. The output directory must be outside the replication package.

| Stage | What it collects from GitHub | Saved input |
|---|---|---|
| Search | Python code matches for quoted IDs, with size splits and pagination | `query_splits`, `search_matches` |
| Metadata | Repository details, then applies the repository filter | `repositories` |
| Releases | All release pages for repositories passing that filter | `releases`, `release_status` |
| Files | Candidate file contents by blob SHA, then checks exact quoted IDs | `literal_checks` |
| Snapshots | Fixed Git commits, then compares each file's blob SHA | `snapshot_checks` |

These tables are written as `.csv.gz` files under the new run's `inputs/`. The final stage writes `repositories.csv`, `files.csv`, and `filter_decisions.csv.gz` using the same selection code as `reproduce.py`. You can stop after any stage with `--stop-after search`, `metadata`, `releases`, `files`, or `snapshots`.

Downloaded candidate files are saved under `source_files/OWNER/REPO/BLOB.py.gz`. The paths and blob hashes in the CSVs link them to the original code. New runs also write `occurrences.csv.gz`, with one row per exact quoted ID, its line and columns, surrounding code, and comment, string, or docstring context. Lines start at 1 and columns at 0. 

The final step creates full repository checkouts under `snapshots/OWNER/REPO/COMMIT` using the fetched objects in `cache/git/`. It verifies candidate file hashes and refuses to overwrite edited checkouts. `snapshots.csv` lists their paths and marks the selected analysis commit for each repository. These folders can be passed to MIST.

`run.json` records the IDs, settings, script hashes, and completed stages. A completed new collection can also be replayed offline:

```bash
python reproduce.py --collection-dir /path/to/mist-github-collection --output-dir /tmp/mist-new-replay
```

> [!WARNING]
> GitHub search is limited to indexed code on the default branch and at most 1,000 results per query. Incomplete responses and size ranges that cannot be split further remain marked in the query log. They are not evidence that no other matches exist. Authentication, network, and repeated API errors stop the run for retry rather than becoming negative results. See GitHub's [code-search documentation](https://docs.github.com/en/rest/search/search#search-code) and [request guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).

## Filtering and Selection

| Step | Repositories |
|---|---:|
| Saved search results, covering 698,696 file versions | 325,819 |
| Repository filter | 14,891 |
| Release filter | 1,338 |
| At least one exact quoted ID found in the downloaded files | 1,229 |
| Commit available and discovered file contents verified | 1,228 |
| Final analysis population, covering 7,105 file versions | 1,219 |

We searched Python files using each ID with and without its namespace, enclosed in single or double quotes. Queries excluded forks. To handle GitHub's result limit, the collector split file-size ranges until each query returned at most 1,000 results. `queries.csv` shows the 796 original queries, with 398 for each quote style. `inputs/query_splits.csv.gz` keeps all 7,212 logged queries, including the smaller queries and their original IDs used by the search matches. The log contains 997,857 matches after removing duplicate items within each query. File versions are deduplicated by repository ID, path, and Git blob SHA.

The repository filter requires collected metadata, a non-fork repository, nonzero size, Python as its main language, at least five stars or five forks, and a push on or after 1 January 2026. It also excludes matches to the name, description, and topic keywords in `filter_settings.json`. Archived and disabled status were not additional exclusion rules.

The release filter removes drafts and prereleases, requires at least two stable releases, and keeps repositories with a median release interval of 7–365 days, between one release per year and one per day, and a latest stable release on or after 1 January 2026. At least 80% of stable tags must meet the original strict or lenient version rules. Other tags are then dropped, leaving 28,021 releases. `filter_rules.py` preserves those rules, including their lenient matches.

The exact-literal and Git checks use their saved outcomes. This offline replay does not download source code or rerun those checks. One repository, `darenr/report_creator`, had no reachable collected commit.

`inputs/analysis_exclusions.csv` records the nine repositories outside the final analysis population. Eight were not selected for analysis and one timed out. Together they account for 140 collected file versions. Reproduction applies these saved exclusions after the collection filters, leaving 1,219 repositories and 7,105 file versions. A new collection has no such exclusions unless this file is supplied.

## Files

- `queries.csv`: one original query per PTM ID, namespace form, and quote style. `total_count` is GitHub's reported count when that query was checked.
- `repositories.csv`: the 1,219 repositories in the final analysis population and their fixed commits.
- `files.csv`: the 7,105 retained file versions, their blob hashes, PTM IDs, and links to the fixed code.
- `filter_decisions.csv.gz`: collection decisions and final selection. `snapshot_available` records availability, while `selected` records final inclusion. A blank later-stage field means the repository did not reach that stage.
- `inputs/`: saved queries, matches, repository metadata, release records, exact-literal checks, and snapshot checks. Large CSVs are gzip-compressed.

Queries were collected on 21–22 May 2026, repository metadata on 22–23 May, and release metadata on 25 May. Exact literals were checked on 4 June and snapshots on 9 June. Search IDs are preserved where a later metadata response returned a different repository ID.

## Note on Reproducibility

> [!WARNING]
>
> 1. GitHub's reported search count can differ from the matches collected through pagination as its index changes. For example, a reported total of 2,000 does not guarantee exactly 2,000 collected matches.
> 2. API results can change over time as repositories are deleted or made private and metadata is updated. Rate limits can also affect collection.
> 3. Rerunning collection today may not produce byte-identical raw data. Use the bundled inputs with `reproduce.py` to reproduce the saved study selection.
