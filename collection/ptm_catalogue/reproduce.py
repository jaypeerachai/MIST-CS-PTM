#!/usr/bin/env python3
"""Reproduce author selection and discovery IDs from the saved catalogue data."""

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def read_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path, rows, fields=None):
    with Path(path).open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields or list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def unique_rows(rows, keys, name):
    result = {}
    for row in rows:
        key = tuple(row[field] for field in keys)
        if key in result:
            raise ValueError(f"Duplicate {name}: {key}")
        result[key] = row
    return result


def calculate(data_dir):
    """Rank authors by weekly usage and collect their discovery IDs."""
    weekly_rows = read_csv(data_dir / "weekly_models.csv")
    live_rows = read_csv(data_dir / "live_models.csv")
    label_rows = read_csv(data_dir / "classifications.csv")
    captures = read_csv(data_dir / "captures.csv")

    unique_rows(weekly_rows, ["target_utc", "model_id"], "weekly observation")
    unique_rows(live_rows, ["model_id"], "live model")
    unique_rows(label_rows, ["model_id"], "classification")
    unique_rows(captures, ["target_utc"], "weekly target")

    classification = {row["model_id"]: row["classification"] for row in label_rows}
    allowed = {"closed", "open_weight", "open_weight_hf_exact", "unknown"}
    if set(classification.values()) - allowed:
        raise ValueError("Unexpected classification label")

    weeks = sorted(row["target_utc"] for row in captures)
    if not weeks or set(weeks) != {row["target_utc"] for row in weekly_rows}:
        raise ValueError("Weekly rows do not cover the capture manifest")
    for row in captures:
        target = datetime.fromisoformat(row["target_utc"])
        actual = datetime.fromisoformat(row["snapshot_utc"])
        hours = abs((actual - target).total_seconds()) / 3600
        if not math.isclose(hours, float(row["distance_hours"]), abs_tol=1e-6):
            raise ValueError("Capture distance does not match its dates")

    author_tokens = defaultdict(lambda: defaultdict(float))
    historical_models = {}
    for row in weekly_rows:
        raw = row["weekly_tokens"]
        if not raw:
            continue  # missing usage, not zero
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError("Invalid token count")
        if row["model_id"] not in classification:
            raise ValueError(f"Missing classification: {row['model_id']}")
        if classification[row["model_id"]] != "closed":
            continue
        author_tokens[row["author"]][row["target_utc"]] += value
        historical_models[row["model_id"]] = row["author"]
    weekly_totals = {}
    for week in weeks:
        weekly_totals[week] = sum(values.get(week, 0) for values in author_tokens.values())
    if any(total <= 0 for total in weekly_totals.values()):
        raise ValueError("A weekly target has no closed-weight token use")

    rankings = []
    for author, values in author_tokens.items():
        observed_weeks = sum(values.get(week, 0) > 0 for week in weeks)
        weekly_shares = [values.get(week, 0) / weekly_totals[week] for week in weeks]
        mean_share = 100 * sum(weekly_shares) / len(weeks)
        rankings.append({
            "author": author,
            "observed_weeks": observed_weeks,
            "mean_weekly_share_pct": mean_share,
            "total_tokens": sum(values.values()),
        })
    rankings.sort(key=lambda row: (
        -row["mean_weekly_share_pct"], -row["total_tokens"], row["author"]
    ))
    selected_authors = [
        row["author"] for row in rankings if row["observed_weeks"] == len(weeks)
    ][:5]

    live_closed_models = {}
    for row in live_rows:
        if row["model_id"] not in classification:
            raise ValueError(f"Missing live classification: {row['model_id']}")
        if classification[row["model_id"]] == "closed":
            live_closed_models[row["model_id"]] = row["author"]
    discovery = []
    for model_id in sorted(historical_models.keys() | live_closed_models.keys()):
        # use the live author when available
        author = live_closed_models.get(model_id, historical_models.get(model_id))
        if author in selected_authors:
            discovery.append({
                "model_id": model_id,
                "author": author,
                "historical": model_id in historical_models,
                "live_including_deprecated": model_id in live_closed_models,
                "model_url": "https://openrouter.ai/" + model_id,
            })
    author_rows = []
    for rank, row in enumerate(rankings, 1):
        id_count = sum(item["author"] == row["author"] for item in discovery)
        author_rows.append({
            "rank": rank,
            "author": row["author"],
            "observed_weeks": row["observed_weeks"],
            "mean_weekly_share_pct": round(row["mean_weekly_share_pct"], 10),
            "selected": row["author"] in selected_authors,
            "discovery_ids": id_count,
        })

    combined_share = sum(
        row["mean_weekly_share_pct"] for row in rankings if row["author"] in selected_authors
    )
    summary = {
        "weekly_targets": len(weeks),
        "distinct_captures": len({row["wayback_timestamp"] for row in captures}),
        "weekly_rows": len(weekly_rows),
        "live_rows": len(live_rows),
        "selected_authors": selected_authors,
        "combined_share_pct": combined_share,
        "discovery_ids": len(discovery),
    }
    return author_rows, discovery, summary


def check_saved_results(authors, discovery):
    results = [("author_rankings.csv", authors), ("discovery_ids.csv", discovery)]
    for name, actual in results:
        normalized = []
        for row in actual:
            normalized.append({key: str(value) for key, value in row.items()})
        if read_csv(HERE / name) != normalized:
            raise ValueError(f"Calculated results differ from {name}")
    metadata = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))
    for name, expected in metadata["input_sha256"].items():
        actual = hashlib.sha256((HERE / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Input file has changed: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=HERE / "inputs")
    parser.add_argument("--output", type=Path, help="Write results to a new folder")
    args = parser.parse_args()
    authors, discovery, summary = calculate(args.data)
    if args.data.resolve() == (HERE / "inputs").resolve():
        check_saved_results(authors, discovery)
        print("Saved inputs and results match.")
    if args.output:
        args.output.mkdir(parents=True, exist_ok=False)
        write_csv(args.output / "author_rankings.csv", authors)
        write_csv(args.output / "discovery_ids.csv", discovery)
    print(
        f"{summary['weekly_targets']} weekly targets, "
        f"{summary['distinct_captures']} distinct archive captures"
    )
    print(f"{summary['weekly_rows']:,} weekly rows, {summary['live_rows']} live catalogue rows")
    print("Selected authors: " + ", ".join(summary["selected_authors"]))
    print(f"Combined mean weekly share: {summary['combined_share_pct']:.4f}%")
    print(f"Discovery IDs: {summary['discovery_ids']}")
    for row in authors:
        if row["selected"]:
            print(f"  {row['author']}: {row['discovery_ids']}")


if __name__ == "__main__":
    main()
