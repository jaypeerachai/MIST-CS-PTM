# PTM catalogue and author selection

This folder includes online catalogue collection and offline reproduction of the five selected authors and 199 PTM IDs used to search GitHub. Reproduction uses saved OpenRouter observations, so no website access or API key is needed. These discovery IDs are separate from the later 427-ID vocabulary used for validation.

## Run

Use Python 3.10 or later. No extra packages are needed:

```bash
python reproduce.py
```

The script compares the reproduced results with the supplied results. To save another copy of the results, use `python reproduce.py --output /path/to/new/folder`.

Expected result: Google, Anthropic, xAI, OpenAI, and Qwen account for 92.7182% of mean weekly closed-weight token use. Their discovery-ID counts are 43, 30, 20, 86, and 20, respectively.

## Files and selection

| File | Contents |
|---|---|
| `inputs/weekly_models.csv` | All 16,338 extracted model-week rows, including open-weight models and rows without usage. |
| `inputs/captures.csv` | Weekly targets, actual capture dates, archive URLs, and time gaps. |
| `inputs/classifications.csv` | The saved open/closed labels and the links used for classification. |
| `inputs/live_models.csv` | The 729 catalogue entries collected with Show Deprecated on 6 May 2026. |
| `author_rankings.csv` | All ranked authors, their shares, observed weeks, and selection. |
| `discovery_ids.csv` | The 199 selected IDs and whether each appeared historically, in the live catalogue, or both. |

We sum the token use of models labelled `closed` for each author and week, then divide by that week's total closed-weight token use. We average these shares over all 26 weekly slots, giving an absent author zero share for that slot. We select the five highest-ranked authors with positive usage in every slot. Xiaomi ranks fifth overall but appears in only seven slots, so Qwen is selected. For those authors, we combine historical IDs with recorded usage and IDs from the live catalogue, including deprecated entries.

The classification sheet checks live OpenRouter detail pages for a model-weights or Hugging Face link. A usable page with such a link is labelled `open_weight`, and one without it is labelled `closed`. Failed checks remain unknown unless a Hugging Face fallback finds public weights. These are saved collection labels, not historical proof of weight availability or licensing. Blank usage values are missing observations, not zero usage. Unknown labels are excluded.

## Collection dates

Weekly targets run from 11 November 2025 to 5 May 2026 at 06:00 UTC. The original script searched for nearby captures within three days. The discovery list uses the live catalogue collected on 6 May 2026.

## Collect again (optional)

`openrouter_scraper.py` is the study's collection helper, renamed without code changes. `collect.py` now starts by searching the Wayback Machine for the 26 weekly dates. It queries the capture index from four days before the first target to four days after the last, then selects the nearest successful HTML capture within three days of each target. If a page cannot be read or has no usage data, it tries other captures within the same three-day window. The manifest records the capture actually used and its distance from the target date.

It then collects the current catalogue with Show Deprecated and classifies the resulting IDs. All output goes to a new folder, including page evidence.

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python collect.py --output /path/to/new/collection
python reproduce.py --data /path/to/new/collection/inputs --output /path/to/new/results
```

To inspect only the capture search, without opening a browser or scraping pages:

```bash
python collect.py --output /path/to/capture-check --discover-only
```

This writes `capture_search.csv` with the returned archive records and `inputs/captures.csv` with the weekly selections. Full collection also writes `capture_attempts.csv` if it attempts archive extraction. Missing or unusable weeks stop full collection, rather than becoming zero usage or being replaced by an older week. Use another new folder when starting the full collection.

To revisit the exact URLs used in the study instead of searching again:

```bash
python collect.py --output /path/to/saved-capture-rerun --capture-mode saved
```

This mode uses the URLs in `inputs/captures.csv` rather than searching again.

> [!WARNING]
> Collection requires internet access and can take hours. The bundled CSVs preserve the extracted rows and classification links without the large HTML archive. Saved end-of-scroll HTML alone does not contain every row of OpenRouter's scrolling list.


## Note on Reproducibility
> [!WARNING]
> Wayback capture availability and page replay can vary between runs. Rerunning collection, even with the same archive URLs, may not return exactly the same model entries or usage values. The live catalogue also changes over time. Use the bundled CSV inputs with `reproduce.py` to reproduce the reported selection.
