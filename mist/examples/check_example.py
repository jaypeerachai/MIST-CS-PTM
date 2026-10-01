"""Compare a fixed repository with the saved MIST evidence."""

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def csv_hash(path):
    with path.open(encoding="utf-8", newline="") as file:
        rows = [json.dumps(row, sort_keys=True, separators=(",", ":"))
                for row in csv.DictReader(file)]
    return hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest(), len(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    expected = json.loads(Path(__file__).with_name("audiotto.json").read_text())
    with tempfile.TemporaryDirectory(prefix="mist-example-") as folder:
        output = Path(folder) / "results"
        subprocess.run([sys.executable, "-m", "mist", "--repo", str(args.repo),
                        "--repository", expected["repository"], "--commit", expected["commit"],
                        "--output", str(output), "--full"], check=True)
        for name, reference in expected["evidence"].items():
            digest, rows = csv_hash(output / name)
            if digest != reference["rows_sha256"] or rows != reference["rows"]:
                raise SystemExit(f"Mismatch in {name}. Check the commit, environment, and input rules.")
        print("PASS: traces, path steps, and graph edges match the saved MIST result.")


if __name__ == "__main__":
    main()
