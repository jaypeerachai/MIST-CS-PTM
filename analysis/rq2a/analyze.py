#!/usr/bin/env python3
"""Summarize PTM interfaces and binding locality from the reuse database."""

import argparse
import csv
import json
import sqlite3
import textwrap
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAMILY_ORDER = ["F01", "F03", "F04", "F02"]
FAMILY_COLORS = {
    "F01": "#8FA9D4", "F02": "#9ABF8C",
    "F03": "#A58BC4", "F04": "#C9675F",
}
AUTHOR_NAMES = {
    "openai": "OpenAI", "google": "Google", "anthropic": "Anthropic",
    "qwen": "Qwen", "x-ai": "xAI",
}
AUTHOR_COLORS = {
    "openai": "#4A5568", "google": "#4285F4", "anthropic": "#A35D3D",
    "qwen": "#6B5DD3", "x-ai": "#222222",
}


def read_mapping(path):
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    mapping = {row["interface_origin"]: row for row in rows}
    if not rows or len(mapping) != len(rows):
        raise ValueError("The interface mapping is empty or has duplicate origins")
    names = defaultdict(set)
    for row in rows:
        if row["family_id"] not in FAMILY_ORDER or not row["family_name"]:
            raise ValueError(f"Unknown family: {row}")
        names[row["family_id"]].add(row["family_name"])
    if any(len(values) != 1 for values in names.values()):
        raise ValueError("A family has more than one name")
    return mapping


def read_bindings(database):
    # Count the initial snapshots separately from historical releases.
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT b.binding_id, r.name AS repository, s.commit_sha,
                   o.ptm_id, o.path AS source_path, o.line AS source_line,
                   b.sink_path, b.sink_line, b.interface_origin, b.call_name,
                   l.cross_file, l.cross_procedure
            FROM bindings b
            LEFT JOIN binding_locality l ON l.binding_id = b.binding_id
            JOIN analyses a ON a.analysis_id = b.analysis_id
            JOIN occurrences o ON o.occurrence_id = b.occurrence_id
            JOIN snapshots s ON s.snapshot_id = b.snapshot_id
            JOIN repositories r ON r.repository_id = s.repository_id
            WHERE a.analysis_type = 'snapshot'
            ORDER BY r.name, b.binding_id
        """).fetchall()
    return [dict(row) for row in rows]


def prepare_bindings(rows, mapping):
    if not rows:
        raise ValueError("No retained snapshot bindings")
    if len({row["binding_id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate binding IDs")
    commits = defaultdict(set)
    result = []
    for row in rows:
        origin = row["interface_origin"]
        if origin not in mapping:
            raise ValueError(f"No interface mapping for {origin!r}")
        if any(row[field] not in (0, 1) for field in ("cross_file", "cross_procedure")):
            raise ValueError(f"Missing or invalid locality: {row['binding_id']}")
        namespace, separator, name = row["ptm_id"].partition("/")
        if not separator or not namespace.removeprefix("~") or not name:
            raise ValueError(f"Expected a canonical PTM ID: {row['ptm_id']}")
        commits[row["repository"]].add(row["commit_sha"])
        result.append({
            **row, **mapping[origin],
            # Strip the catalogue prefix only for author grouping, not the PTM ID.
            "model_author": namespace.removeprefix("~"),
            "non_local": bool(row["cross_file"] or row["cross_procedure"]),
        })
    if any(len(values) != 1 for values in commits.values()):
        raise ValueError("Expected one snapshot commit per repository")
    return result


def group_summary(rows, keys):
    groups = defaultdict(list)
    repository_total = len({row["repository"] for row in rows})
    for row in rows:
        groups[tuple(row[key] for key in keys)].append(row)
    output = []
    for values, selected in sorted(groups.items()):
        non_local = sum(row["non_local"] for row in selected)
        repositories = len({row["repository"] for row in selected})
        output.append({
            **dict(zip(keys, values)),
            "binding_count": len(selected),
            "binding_share": len(selected) / len(rows),
            "repository_count": repositories,
            "repository_share": repositories / repository_total,
            "local_binding_count": len(selected) - non_local,
            "non_local_binding_count": non_local,
            "non_local_binding_share": non_local / len(selected),
        })
    return output


def calculate(rows, mapping):
    rows = prepare_bindings(rows, mapping)
    families = group_summary(rows, ["family_id", "family_name"])
    origins = group_summary(rows, ["interface_origin", "family_id", "family_name"])
    authors = group_summary(rows, ["model_author"])
    pathways = group_summary(rows, ["model_author", "family_id", "interface_origin"])
    origin_counts = Counter(row["interface_origin"] for row in rows)
    for row in pathways:
        row["share_within_origin"] = row["binding_count"] / origin_counts[row["interface_origin"]]
    for row in authors:
        selected = [item for item in rows if item["model_author"] == row["model_author"]]
        row["interface_family_count"] = len({item["family_id"] for item in selected})
        row["interface_origin_count"] = len({item["interface_origin"] for item in selected})

    by_repository = defaultdict(list)
    for row in rows:
        by_repository[row["repository"]].append(row)
    repositories = []
    for name, selected in sorted(by_repository.items()):
        non_local = sum(row["non_local"] for row in selected)
        model_authors = sorted({row["model_author"] for row in selected})
        family_ids = sorted({row["family_id"] for row in selected})
        interface_origins = sorted({row["interface_origin"] for row in selected})
        repositories.append({
            "repository": name, "commit_sha": selected[0]["commit_sha"],
            "binding_count": len(selected),
            "model_author_count": len(model_authors),
            "interface_family_count": len(family_ids),
            "interface_origin_count": len(interface_origins),
            "local_binding_count": len(selected) - non_local,
            "non_local_binding_count": non_local,
            "locality": "has_non_local" if non_local else "local_only",
            "model_authors": " | ".join(model_authors),
            "interface_families": " | ".join(sorted({row["family_name"] for row in selected})),
            "interface_origins": " | ".join(interface_origins),
        })

    locality = []
    for label, flags in [
        ("local", {(0, 0)}),
        ("cross_file_only", {(1, 0)}),
        ("cross_procedure_only", {(0, 1)}),
        ("cross_file_and_procedure", {(1, 1)}),
        ("non_local", {(1, 0), (0, 1), (1, 1)}),
    ]:
        selected = [row for row in rows if (row["cross_file"], row["cross_procedure"]) in flags]
        repository_count = len({row["repository"] for row in selected})
        locality.append({
            "locality": label, "binding_count": len(selected),
            "binding_share": len(selected) / len(rows),
            "repository_count": repository_count,
            "repository_share": repository_count / len(repositories),
        })

    frameworks = {row["repository"] for row in rows if row["family_id"] == "F03"}
    sdks = {row["repository"] for row in rows if row["family_id"] == "F01"}
    openai_other = [row for row in rows if row["interface_origin"] == "openai" and row["model_author"] != "openai"]
    summary = {
        "analysis_type": "snapshot",
        "bindings": len(rows), "repositories": len(repositories),
        "model_authors": len(authors), "interface_families": len(families),
        "interface_origins": len(origins),
        "non_local_bindings": sum(row["non_local"] for row in rows),
        "non_local_binding_share": sum(row["non_local"] for row in rows) / len(rows),
        "local_bindings": sum(not row["non_local"] for row in rows),
        "repositories_with_non_local_binding": sum(row["non_local_binding_count"] > 0 for row in repositories),
        "repositories_local_only": sum(row["non_local_binding_count"] == 0 for row in repositories),
        "repositories_using_multiple_families": sum(row["interface_family_count"] > 1 for row in repositories),
        "repositories_using_multiple_origins": sum(row["interface_origin_count"] > 1 for row in repositories),
        "framework_repositories": len(frameworks),
        "framework_and_sdk_repositories": len(frameworks & sdks),
        "framework_repositories_also_using_sdk_share": len(frameworks & sdks) / len(frameworks) if frameworks else None,
        "openai_origin_bindings": origin_counts["openai"],
        "openai_origin_other_author_bindings": len(openai_other),
        "openai_origin_other_author_repositories": len({row["repository"] for row in openai_other}),
        "openai_origin_other_author_share": len(openai_other) / origin_counts["openai"] if origin_counts["openai"] else None,
    }
    for key in ["repositories_with_non_local_binding", "repositories_using_multiple_families", "repositories_using_multiple_origins"]:
        summary[key + "_share"] = summary[key] / len(repositories)
    tables = {
        "interface_families": families, "interface_origins": origins,
        "model_authors": authors, "author_interfaces": pathways,
        "repositories": repositories, "locality": locality,
    }
    return tables, summary


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def save_figure(figure, output, name):
    figure.savefig(output / (name + ".pdf"), metadata={"CreationDate": None, "ModDate": None})
    figure.savefig(output / (name + ".svg"), metadata={"Date": None})


def plot_interfaces(tables, output, plt):
    from matplotlib.path import Path as PlotPath
    from matplotlib.patches import PathPatch, Rectangle

    family_counts = {row["family_id"]: row["binding_count"] for row in tables["interface_families"]}
    author_counts = {row["model_author"]: row["binding_count"] for row in tables["model_authors"]}
    # Group small origins only in the plot. The CSVs retain every origin.
    named_origins = {"openai", "google", "anthropic", "litellm", "langchain_openai", "llama_index", "crewai", "agents", "requests"}
    other_names = {"F01": "other provider SDKs", "F02": "cloud-platform SDKs", "F03": "other frameworks", "F04": "other HTTP clients"}
    left_edges, right_edges = Counter(), Counter()
    for row in tables["author_interfaces"]:
        family, origin = row["family_id"], row["interface_origin"]
        display_origin = origin if origin in named_origins else other_names[family]
        left_edges[(row["model_author"], family)] += row["binding_count"]
        right_edges[(family, display_origin)] += row["binding_count"]
    origin_counts = Counter()
    origin_family = {}
    for (family, origin), count in right_edges.items():
        origin_counts[origin] += count
        origin_family[origin] = family
    authors = sorted(author_counts, key=lambda key: (-author_counts[key], key))
    families = [key for key in FAMILY_ORDER if key in family_counts]
    origins = sorted(origin_counts, key=lambda key: (families.index(origin_family[key]), -origin_counts[key], key))
    orders = [authors, families, origins]
    counts = [author_counts, family_counts, origin_counts]
    gaps = [22, 55, 12]
    total = sum(author_counts.values())
    scale = min((615 - gap * (len(order) - 1)) / total for order, gap in zip(orders, gaps))
    positions = []
    for order, column_counts, gap in zip(orders, counts, gaps):
        y = 85
        column = {}
        for key in order:
            height = column_counts[key] * scale
            column[key] = (y, height)
            y += height + gap
        positions.append(column)

    figure, ax = plt.subplots(figsize=(15.2, 6.24))
    figure.subplots_adjust(left=0, right=1, bottom=0, top=1)
    ax.set(xlim=(0, 1900), ylim=(780, 0))
    ax.axis("off")
    x_values, width = [350, 890, 1385], 22
    for column, edges in enumerate([left_edges, right_edges]):
        source_used, target_used = Counter(), Counter()
        x1, x2 = x_values[column] + width, x_values[column + 1]
        bend = (x2 - x1) * 0.48
        for source in orders[column]:
            for target in orders[column + 1]:
                count = edges[(source, target)]
                if not count:
                    continue
                y1 = positions[column][source][0] + source_used[source] * scale
                y2 = positions[column + 1][target][0] + target_used[target] * scale
                height = count * scale
                vertices = [(x1, y1), (x1 + bend, y1), (x2 - bend, y2), (x2, y2),
                            (x2, y2 + height), (x2 - bend, y2 + height),
                            (x1 + bend, y1 + height), (x1, y1 + height), (x1, y1)]
                codes = [PlotPath.MOVETO, PlotPath.CURVE4, PlotPath.CURVE4, PlotPath.CURVE4,
                         PlotPath.LINETO, PlotPath.CURVE4, PlotPath.CURVE4, PlotPath.CURVE4, PlotPath.CLOSEPOLY]
                family = target if column == 0 else source
                ax.add_patch(PathPatch(PlotPath(vertices, codes), facecolor=FAMILY_COLORS[family], alpha=0.66, edgecolor="none"))
                source_used[source] += count
                target_used[target] += count

    for column, order in enumerate(orders):
        for key in order:
            y, height = positions[column][key]
            color = AUTHOR_COLORS.get(key, "#607D8B") if column == 0 else FAMILY_COLORS[key if column == 1 else origin_family[key]]
            ax.add_patch(Rectangle((x_values[column], y), width, height, facecolor=color, edgecolor="white", linewidth=0.5))
    for author in authors:
        y, height = positions[0][author]
        count = author_counts[author]
        ax.text(338, y + height / 2, f"{AUTHOR_NAMES.get(author, author)}  {count:,} ({count / total:.1%})", ha="right", va="center", fontsize=14)
    family_labels = {
        row["family_id"]: textwrap.fill(row["family_name"], width=28)
        for row in tables["interface_families"]
    }
    for family in families:
        y, height = positions[1][family]
        count = family_counts[family]
        ax.text(901, y + height / 2 - 8, family_labels[family], ha="center", va="bottom", fontsize=14, weight="bold")
        ax.text(901, y + height / 2 + 10, f"{count:,} ({count / total:.1%})", ha="center", va="top", fontsize=12)
    label_positions = []
    for origin in origins:
        y, height = positions[2][origin]
        label_positions.append(max(y + height / 2, label_positions[-1] + 43 if label_positions else 85))
    if label_positions[-1] > 715:
        shift = label_positions[-1] - 715
        label_positions = [value - shift for value in label_positions]
    for origin, label_y in zip(origins, label_positions):
        y, height = positions[2][origin]
        count = origin_counts[origin]
        ax.plot([1407, 1422], [y + height / 2, label_y], color="#8A969E", linewidth=0.6)
        ax.text(1428, label_y, f"{origin}  {count:,} ({count / total:.1%})", va="center", fontsize=12)
    for x, title in [(265, "PTM author"), (901, "Code interface family"), (1545, "Interface origin")]:
        ax.text(x, 38, title, ha="center", va="center", fontsize=16, weight="bold")
    save_figure(figure, output, "interfaces")
    plt.close(figure)


def plot_locality(summary, output, plt):
    figure, ax = plt.subplots(figsize=(8, 2.8), layout="constrained")
    counts = [summary["non_local_bindings"], summary["repositories_with_non_local_binding"]]
    totals = [summary["bindings"], summary["repositories"]]
    labels = ["Bindings", "Repositories"]
    for y, (count, total) in enumerate(zip(counts, totals)):
        percent = 100 * count / total
        ax.barh(y, percent, color=FAMILY_COLORS["F01"], label="Non-local binding(s)" if y == 0 else None)
        ax.barh(y, 100 - percent, left=percent, color="#E3E7EB", label="Local only" if y == 0 else None)
        ax.text(percent / 2, y, f"{count:,}/{total:,} ({percent:.1f}%)", ha="center", va="center", fontsize=11)
        ax.text(percent + (100 - percent) / 2, y, f"{total - count:,}/{total:,} ({100 - percent:.1f}%)", ha="center", va="center", fontsize=11)
    ax.set(xlim=(0, 100), yticks=[0, 1], yticklabels=labels, xlabel="Share (%)")
    ax.invert_yaxis()
    ax.tick_params(axis="y", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.legend(loc="lower center", bbox_to_anchor=(0.5, 1), ncols=2, frameon=False)
    save_figure(figure, output, "locality")
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--mapping", type=Path, default=HERE / "interface_mapping.csv")
    parser.add_argument("--output", type=Path, required=True, help="An empty output directory")
    args = parser.parse_args()
    if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):
        parser.error("Use a new or empty output directory")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": "DejaVu Sans", "svg.fonttype": "none", "svg.hashsalt": "rq2a"})

    tables, summary = calculate(read_bindings(args.database), read_mapping(args.mapping))
    args.output.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        write_csv(args.output / (name + ".csv"), rows)
    with (args.output / "summary.json").open("w", encoding="utf-8") as file:
        json.dump(summary, file, indent=2)
        file.write("\n")
    plot_interfaces(tables, args.output, plt)
    plot_locality(summary, args.output, plt)
    print(f"{summary['bindings']:,} bindings in {summary['repositories']:,} repositories")
    print(f"Results: {args.output}")


if __name__ == "__main__":
    main()
