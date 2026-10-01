"""Small helpers for matching binding records."""

import csv
import gzip
import hashlib
import json
from pathlib import Path
import subprocess


def structural_fingerprint(value):
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return 'sha256:' + hashlib.sha256(text.encode('utf-8')).hexdigest()


def run_git(repository, arguments):
    return subprocess.run(['git', '-C', str(repository), *arguments],
                          check=True, capture_output=True).stdout


def read_jsonl(path):
    opener = gzip.open if path.suffix == '.gz' else Path.open
    with opener(path, 'rt', encoding='utf-8') as file:
        return [json.loads(line) for line in file if line.strip()]


def read_csv(path):
    opener = gzip.open if path.suffix == '.gz' else Path.open
    with opener(path, 'rt', encoding='utf-8', newline='') as file:
        return list(csv.DictReader(file))


def write_csv(path, rows, fields):
    with path.open('x', encoding='utf-8', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)

