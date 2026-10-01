"""Run MIST on a clean repository checkout at a fixed commit."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from mist.bindings.selection import SINK_MODES


def check_snapshot(repo, commit):
    if not repo.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Provide a repository checkout and its full 40-character commit SHA")
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True)
    if result.stdout.strip() != commit:
        raise ValueError("The checkout is not at the requested commit. MIST will not change it.")
    root = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if Path(root).resolve() != repo:
        raise ValueError("--repo must be the repository root")
    result = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True, check=True)
    if result.stdout.strip():
        raise ValueError("The checkout has local changes or untracked files. Use a clean snapshot.")
    from mist.io import iter_python_files
    tracked = set(subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z"],
                                         text=True).split("\0"))
    for path in iter_python_files(repo):
        if path.relative_to(repo).as_posix() not in tracked or path.is_symlink():
            raise ValueError(f"Python source is not a regular file from this checkout: {path.relative_to(repo)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="local Git checkout")
    parser.add_argument("--commit", required=True, help="full commit SHA already checked out")
    parser.add_argument("--repository", required=True, help="GitHub owner/repository for evidence links")
    parser.add_argument("--output", type=Path, required=True, help="new directory outside the checkout")
    parser.add_argument("--full", action="store_true", help="also save the graph and candidate evidence")
    parser.add_argument('--sink-mode', choices=SINK_MODES, default='shortest',
                        help='sink validation mode (default: shortest)')
    parser.add_argument("--model-ids", type=Path,
                        help="CSV with a model_id column (default: bundled 427 PTM IDs)")
    args = parser.parse_args()
    if os.environ.get("PYTHONHASHSEED") != "0":
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable, "-m", "mist", *sys.argv[1:]])
    repo, output = args.repo.resolve(), args.output.resolve()
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
            raise ValueError("--repository must be GitHub owner/repository")
        if output.exists() or output == repo or repo in output.parents:
            raise ValueError("Choose a new output directory outside the analyzed repository")
        check_snapshot(repo, args.commit)
        from mist.pipeline import analyze
        summary = analyze(repo, args.commit, args.repository, output, args.full,
                          model_ids=args.model_ids, sink_mode=args.sink_mode)
    except (ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    print(json.dumps({key: summary[key] for key in
                      ["repository", "commit", "confirmed_reuse", "occurrences", "confirmed_occurrences"]}, indent=2))
    print(f"Results: {output}")
    return 0
