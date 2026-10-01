"""Read public inputs and check fixed repository snapshots."""

import csv
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

from openpyxl import load_workbook

HERE = Path(__file__).resolve().parent
EVALUATION = HERE.parent
csv.field_size_limit(10_000_000)


def read_csv(path):
    with Path(path).open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows):
    if not rows:
        raise ValueError('No result rows')
    with Path(path).open('x', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def source_location(url):
    parts = unquote(urlsplit(url).path).lstrip('/').split('/')
    if len(parts) < 5 or parts[2] != 'blob':
        raise ValueError(f'Expected a fixed GitHub source URL: {url}')
    return '/'.join(parts[:2]), parts[3], '/'.join(parts[4:])


def load_cases(dataset, workbook=None):
    names = ('benchmark', 'unseen_holdout') if dataset == 'all' else (dataset,)
    paths = [Path(workbook)] if workbook else [EVALUATION / 'classification' / f'{name}.xlsx' for name in names]
    cases = []
    seen = set()
    for path in paths:
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            rows = iter(book['annotations'].values)
            headers = next(rows)
            for values in rows:
                if not any(v is not None for v in values):
                    continue
                row = dict(zip(headers, values))
                if not row['case_id'] or row['case_id'] in seen:
                    raise ValueError('Missing or duplicate case ID')
                seen.add(row['case_id'])
                repository, commit, file_path = source_location(row['source_url'])
                if repository != row['repository']:
                    raise ValueError(f'Repository does not match source URL: {row["case_id"]}')
                # Labels and saved predictions are not inputs to either analyzer.
                cases.append(dict(case_id=row['case_id'], repository=repository, commit=commit,
                                  file_path=file_path, ptm_id=row['ptm_id']))
        finally:
            book.close()
    return cases


def checkout(snapshots, repository, commit, direct=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError(f'Invalid repository: {repository}')
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError(f'Expected a full commit SHA: {commit}')
    path = Path(direct).resolve() if direct else Path(snapshots).resolve() / repository.replace('/', '__') / commit
    head = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, check=True).stdout.strip()
    if head != commit:
        raise ValueError(f'Wrong commit for {repository}: {head} != {commit}')
    subprocess.run(['git', '-C', str(path), 'diff', '--quiet', 'HEAD', '--'], check=True)
    return path


def source_file(repo, relative):
    path = (repo / relative).resolve()
    if not path.is_relative_to(repo.resolve()) or not path.is_file():
        raise ValueError(f'Missing file or external link: {relative}')
    return path


def select_cases(cases, repository=None, case_ids=None):
    wanted = set(case_ids or [])
    selected = [row for row in cases if (not repository or row['repository'] == repository)
                and (not wanted or row['case_id'] in wanted)]
    if not selected or wanted - {row['case_id'] for row in selected}:
        raise ValueError('No matching cases, or unknown case IDs')
    return selected
