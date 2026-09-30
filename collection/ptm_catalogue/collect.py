#!/usr/bin/env python3
"""Find weekly Wayback captures and collect a new PTM catalogue separately."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from reproduce import HERE, read_csv, write_csv

CAPTURE_FIELDS = ["target_utc", "wayback_timestamp", "snapshot_utc", "distance_hours",
                  "archive_url", "note", "status"]


def capture_row(match, note=""):
    hours = abs((match.actual_utc - match.target_utc).total_seconds()) / 3600 if match.actual_utc else ""
    return {
        "target_utc": match.target_utc.isoformat(),
        "wayback_timestamp": match.timestamp or "",
        "snapshot_utc": match.actual_utc.isoformat() if match.actual_utc else "",
        "distance_hours": hours,
        "archive_url": match.url or "",
        "note": note,
        "status": match.status,
    }


def prepare_captures(output, mode):
    import openrouter_scraper as scraper

    if mode == "saved":
        rows = read_csv(HERE / "inputs/captures.csv")
        for row in rows:
            row["status"] = "saved"
        candidates = []
    else:
        targets = sorted(scraper.weekly_targets())
        print(f"Searching Wayback captures for {len(targets)} weekly dates", flush=True)
        # The original notebook queried four extra days and matched within three.
        candidates = scraper.fetch_cdx_captures(
            start=min(targets) - timedelta(days=4),
            end=max(targets) + timedelta(days=4),
        )
        write_csv(output / "capture_search.csv", candidates,
                  ["timestamp", "original", "statuscode", "mimetype", "digest"])
        matches = scraper.nearest_snapshots(targets, candidates, max_distance=timedelta(days=3))
        rows = [capture_row(match) for match in matches]
    write_csv(output / "inputs/captures.csv", rows, CAPTURE_FIELDS)
    return rows, candidates


async def scrape_capture(page, capture, candidates, output, mode):
    import openrouter_scraper as scraper

    target = datetime.fromisoformat(capture["target_utc"])
    actual = datetime.fromisoformat(capture["snapshot_utc"])
    match = scraper.SnapshotMatch(
        target, capture["wayback_timestamp"], actual,
        abs((actual - target).total_seconds()) / 3600, "matched", capture["archive_url"],
    )
    if mode == "saved":
        rows = await scraper.async_scrape_models_snapshot(page, match, output)
        if not any(row.get("weekly_tokens") is not None for row in rows):
            raise RuntimeError(f"No usage extracted from {match.url}")
        return rows, capture, []

    rows, used_match, attempts = await scraper.async_scrape_models_snapshot_with_fallback(
        page, match, candidates, output, max_distance=timedelta(days=3),
    )
    if not any(attempt["accepted"] for attempt in attempts):
        capture = dict(capture, status="unusable", note="No usable capture within three days")
        return [], capture, attempts
    note = "Used another capture within three days" if used_match.timestamp != match.timestamp else ""
    return rows, capture_row(used_match, note), attempts


async def click_control(page, label):
    import openrouter_scraper as scraper

    context = await scraper.async_active_context(page)
    clicked = await context.evaluate("""(label) => {
        for (const node of document.querySelectorAll('button, a, label')) {
            const text = (node.innerText || node.textContent || '').trim().toLowerCase();
            if (text === label || text.includes(label)) {
                node.click();
                return true;
            }
        }
        return false;
    }""", label)
    await page.wait_for_timeout(1500)
    return clicked


async def collect(output, capture_mode="discover", discover_only=False):
    """Collect archive rows, live models, and their classifications."""
    # keep --help usable without Playwright
    from playwright.async_api import async_playwright
    import openrouter_scraper as scraper

    output.mkdir(parents=True, exist_ok=False)
    input_dir = output / "inputs"
    input_dir.mkdir()
    captures, candidates = prepare_captures(output, capture_mode)
    missing = [row["target_utc"] for row in captures if row["status"] not in {"matched", "saved"}]
    print(f"{len(captures) - len(missing)}/{len(captures)} weekly dates have a selected capture", flush=True)
    if discover_only:
        print(f"Saved capture search and selections to {output}. No pages were scraped.")
        return
    if missing:
        raise RuntimeError("No capture within three days for: " + ", ".join(missing)
                           + ". See inputs/captures.csv. The collection has not started.")
    weekly_rows = []
    all_attempts = []
    models_to_classify = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1200})
        try:
            for index, capture in enumerate(captures, 1):
                target = datetime.fromisoformat(capture["target_utc"])
                print(f"Archive {index}/{len(captures)}: {target.date()}", flush=True)
                rows, used_capture, attempts = await scrape_capture(page, capture, candidates, output, capture_mode)
                captures[index - 1] = used_capture
                write_csv(input_dir / "captures.csv", captures, CAPTURE_FIELDS)
                all_attempts.extend(attempts)
                if all_attempts:
                    write_csv(output / "capture_attempts.csv", all_attempts)
                if not rows:
                    raise RuntimeError(f"No usable capture within three days of {target.date()}. "
                                       "See capture_attempts.csv. No older week was substituted.")
                for row in rows:
                    model_id = urlparse(row["model_url"]).path.lstrip("/")
                    weekly_rows.append({
                        "target_utc": capture["target_utc"],
                        "model_id": model_id,
                        "author": row["author"],
                        "weekly_tokens_text": row["weekly_tokens_text"],
                        "weekly_tokens": row.get("weekly_tokens"),
                    })
                    if row.get("weekly_tokens") is not None:
                        models_to_classify[model_id] = row
                write_csv(input_dir / "weekly_models.csv", weekly_rows)

            print("Collecting the current catalogue with Show Deprecated", flush=True)
            await scraper.async_render_page(page, scraper.MODELS_URL, timeout_ms=90000)
            await click_control(page, "inactive models")
            if not await click_control(page, "show deprecated"):
                raise RuntimeError("Show Deprecated was not found; inspect the saved run")
            context = await scraper.async_active_context(page)
            await context.evaluate("() => window.scrollTo(0, 0)")
            rows = await scraper.async_collect_models_while_scrolling(
                page,
                max_scrolls=180,
                stable_rounds=24,
            )
            if not rows:
                raise RuntimeError("The live catalogue returned no rows")
            scraper.save_text(
                output / "evidence/live_catalogue.html",
                await scraper.async_rendered_content(page),
            )
            scrape_time = datetime.now(timezone.utc).isoformat()
            live_rows = []
            for row in rows:
                model_id = urlparse(row["model_url"]).path.lstrip("/")
                live_rows.append({
                    "model_id": model_id,
                    "author": row["author"],
                    "model_name": row["model_name"],
                    "scrape_utc": scrape_time,
                })
                row["target_utc"] = scrape_time
                models_to_classify.setdefault(model_id, row)
            write_csv(input_dir / "live_models.csv", live_rows)

            classification_rows = []
            for index, (model_id, row) in enumerate(sorted(models_to_classify.items()), 1):
                print(f"Classifying {index}/{len(models_to_classify)}: {model_id}", flush=True)
                result = await scraper.async_classify_model_detail(page, row, output)
                classification_rows.append({
                    "model_id": model_id,
                    "classification": result["classification"],
                    "classification_source": result["classification_source"],
                    "detail_url": result["detail_url"],
                    "has_model_weights_link": result["has_model_weights_link"],
                    "weights_links": result["weights_links"],
                    "hf_exact_url": result["hf_exact_url"],
                    "hf_exact_found": result["hf_exact_found"],
                })
                write_csv(input_dir / "classifications.csv", classification_rows)
        finally:
            await browser.close()
    print(f"Saved new data to {input_dir}. The bundled study inputs were not changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True,
        help="New folder for the collection and page evidence",
    )
    parser.add_argument("--capture-mode", choices=["discover", "saved"], default="discover",
                        help="Search Wayback for weekly captures (default), or revisit the saved URLs")
    parser.add_argument("--discover-only", action="store_true",
                        help="Save the capture search and selections without scraping pages")
    args = parser.parse_args()
    if args.discover_only and args.capture_mode != "discover":
        parser.error("--discover-only requires --capture-mode discover")
    if args.output.exists():
        parser.error("Output already exists. Choose a new folder.")
    asyncio.run(collect(args.output, args.capture_mode, args.discover_only))


if __name__ == "__main__":
    main()
