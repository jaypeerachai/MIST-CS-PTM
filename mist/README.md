# 🔍 MIST

MIST traces PTM IDs to the calls that use them in Python repositories. This package contains MIST, the 427 PTM IDs, and its detection rules. It reads source code without running the target application or calling a model service.

## 📦 Install

Use Python 3.10.12 on Linux in a separate environment.

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps .
```

> [!IMPORTANT]
> Keep the pinned versions, including `httpx==0.28.1` and `requests==2.34.0`. MIST uses Jedi to resolve imports, so installed libraries can affect its results. Jedi uses the same Python environment as MIST. Do not install the analyzed project's requirements into this environment.

## 🔎 Analyze a repository

Check out the desired commit first, then run:

```bash
mist --repo /path/to/repository --commit FULL_COMMIT_SHA \
     --repository owner/name --output /path/to/new-results
```

> [!IMPORTANT]
> The checkout must be clean and match the full commit SHA. Extra Python files, including ignored generated files, are rejected. The output directory must be new and outside the checkout. MIST does not change the checkout.

`python -m mist` runs the same command.

MIST uses the bundled 427 PTM IDs by default. To use another vocabulary, add `--model-ids /path/to/ptm_ids.csv`. The CSV must have a `model_id` column:

```csv
model_id
openai/gpt-4o
google/gemini-2.0-flash
```

This option works in both output modes. `summary.json` records the selected file, number of unique IDs, and file hash. Its `inputs_sha256.ptm_ids.csv` entry refers to the selected vocabulary, including when a custom file is supplied.

Outputs:

- `decisions.csv`: one decision per PTM ID occurrence, with `confirmed_reuse` and `status`.
- `traces.csv`: source, selected sink, locality, and trace status.
- `trace_steps.csv`: ordered steps and locations on the recovered paths.
- `mock_checks.json`: detailed evidence used to check mocked calls.
- `summary.json`: counts, commit, environment, and input hashes.

`summary.json` also keeps `jedi_diagnostics` once for the run, including the diagnostic status, file counts, and any error. This does not indicate whether Jedi resolved an individual binding.

> [!TIP]
> Add `--full` to retain discovered ids (`model_id_occurrences.csv`), import records (`import_origin_occurrences.csv`), both graphs, candidate calls, and source context. Temporary parsing files are removed after the run.

`call_graph_edges.csv` records import-origin propagation and candidate-call matching. `binding_graph.csv` records the connections used to trace PTM IDs to calls.

In `candidate_calls.csv`, `binding_eligible=True` means the call can be used as a sink for tracing. It does not confirm reuse. MIST still needs a path from the id and must check for mocked calls. `False` means there is not enough evidence to accept the call as a sink.

## 🔀 Sink modes

Choose how many reachable calls to check with `--sink-mode`:

| Mode | Behaviour |
| --- | --- |
| `shortest` (default) | Check only the first shortest route. |
| `confirmed-all` | If that route confirms reuse, check the other reachable sinks and retain their confirmed bindings. Otherwise stop. |
| `fallback` | If the first route does not confirm reuse, try other sinks until one passes. |
| `all` | Check every reachable eligible sink independently and retain all confirmed bindings. |

For example:

```bash
mist --repo /path/to/repository --commit FULL_COMMIT_SHA \
     --repository owner/name --output /path/to/new-results --sink-mode all --full
```

Each mode writes `bindings.csv` for confirmed source–sink pairs, `binding_steps.csv` for their ordered paths, and `sink_checks.csv` for every checked pair, including mocked or unresolved results. Join these files by `trace_id`. `traces.csv` keeps one selected route per occurrence, preferring a confirmed route and otherwise an unresolved one. `summary.json` records the mode and the checked and confirmed binding counts. Switching modes does not change the graph.

> [!NOTE]
> These modes retain one representative path per source–sink pair, not every possible path or runtime branch. `--full` controls graph export independently of the sink mode. An unchecked sink has no review decision in `sink_checks.csv`.

See [graph_reference.xlsx](graph_reference.xlsx) for the graph's node and edge types.

`confirmed_reuse=True` means that MIST confirmed an eligible path under the selected mode. Other statuses are not confirmed reuse. An unresolved trace is not proof that a PTM is unused. This command analyzes one snapshot, not changes between releases.

FP statuses use `fp_<category>` from the [non-reuse codebook](../analysis/rq1/codebooks/fp_codebook.xlsx). Unresolved statuses remain separate.

## 🛠️ Design and implementation

MIST combines three components:

- **Python AST (abstract syntax tree):** parses source code to identify PTM ID literals, imports, assignments, functions, and calls, retaining their code locations.
- **Jedi:** helps resolve symbol definitions and import origins when syntax alone is insufficient, including through aliases and project imports.
- **NetworkX:** stores the directed graph connecting code elements and analysis states. MIST searches this graph for paths from PTM ID occurrences to eligible calls.

MIST's tracing rules build the connections, including assignments, argument passing, and returns across files and procedures. It checks call eligibility and mock evidence before retaining confirmed PTM bindings. The analysis does not execute the target application.

## 🗂️ Code layout

The code is organized by purpose:

```text
src/mist/
  cli.py, pipeline.py   command and analysis workflow
  analysis/             parsing, PTM IDs, calls, source context
  bindings/             repository symbols, value-flow graph, tracing, locations
  mocks/                scoped patches, fixtures, clients, service evidence
  rules/                rule loading and context terms
  data/                 427 IDs and required detection rules
```
