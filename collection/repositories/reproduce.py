#!/usr/bin/env python3
"""Reproduce repository selection from the saved GitHub collection."""

import argparse
import csv
import gzip
import hashlib
import io
import json
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, urlsplit

from filter_rules import preliminary_filter, release_filter

HERE = Path(__file__).resolve().parent
OUTPUT_FIELDS = {
    'queries.csv': ['query_row_id', 'model_id', 'search_seed', 'search_seed_type',
                    'quote_style', 'query_text', 'total_count', 'checked_at_utc'],
    'repositories.csv': ['github_repo_id', 'repository', 'commit_sha', 'url',
                         'collected_file_versions', 'files_at_selected_commit', 'saved_snapshots'],
    'files.csv': ['repository', 'commit_sha', 'path', 'blob_sha', 'model_ids', 'url'],
    'filter_decisions.csv.gz': ['github_repo_id', 'repository', 'preliminary_pass', 'preliminary_reasons',
                              'release_pass', 'release_reasons', 'exact_literal_found', 'snapshot_available'],
}


def read_csv(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8', newline='') as file:
        yield from csv.DictReader(file)


def write_csv(path, rows, fields):
    with path.open('wb') as raw:
        stream = gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) if path.suffix == '.gz' else raw
        with io.TextIOWrapper(stream, encoding='utf-8', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_inputs(directory=HERE):
    for item in json.loads((directory / 'sources.json').read_text())['inputs']:
        path = directory / item['file']
        if sha256(path) != item['sha256']:
            raise ValueError(f'Input changed: {path.name}')


def model_key(row):
    return row['github_repo_id'], row['file_path'], row['file_sha'], row['model_id']


def query_text(row):
    quote_char = '"' if row['quote_style'] == 'double' else "'"
    literal = json.dumps(quote_char + row['search_seed'] + quote_char)
    return f"{literal} in:file NOT is:fork language:Python size:{row['size_min']}..{row['size_max']}"


def original_queries(rows):
    """Keep one original query for each ID, namespace form, and quote style."""
    grouped = defaultdict(list)
    for row in rows:
        key = row['model_id'], row['search_seed_type'], row['quote_style']
        grouped[key].append(row)
    fields = ['query_row_id', 'model_id', 'search_seed', 'search_seed_type',
              'quote_style', 'query_text', 'total_count', 'checked_at_utc']
    result = []
    for key, group in grouped.items():
        low = min(int(row['size_min']) for row in group)
        high = max(int(row['size_max']) for row in group)
        roots = [row for row in group
                 if int(row['size_min']) == low and int(row['size_max']) == high]
        if len(roots) != 1:
            raise ValueError(f'Expected one original query: {key}')
        result.append({field: roots[0][field] for field in fields})
    return result


def calculate(data_dir, settings):
    """Apply filters and retain the saved, checked file versions."""
    queries = {}
    for row in read_csv(data_dir / 'query_splits.csv.gz'):
        if row['query_row_id'] in queries or row['query_text'] != query_text(row):
            raise ValueError('Duplicate query ID or unexpected query text')
        queries[row['query_row_id']] = row

    decisions = {}
    for row in read_csv(data_dir / 'repositories.csv.gz'):
        repo_id = row['github_repo_id']
        if repo_id in decisions:
            raise ValueError(f'Duplicate repository ID: {repo_id}')
        reasons = preliminary_filter(row, settings)
        decisions[repo_id] = {
            'github_repo_id': repo_id, 'repository': row['input_full_name'],
            'preliminary_pass': str(not reasons).lower(),
            'preliminary_reasons': '|'.join(reasons),
            'release_pass': '', 'release_reasons': '',
            'exact_literal_found': '', 'snapshot_available': '',
        }

    releases = defaultdict(list)
    for row in read_csv(data_dir / 'releases.csv.gz'):
        releases[row['release_collection_id']].append(row)
    retained_releases = 0
    release_ids = set()
    for row in read_csv(data_dir / 'release_status.csv.gz'):
        repo_id = row['github_repo_id']
        if repo_id in release_ids or decisions[repo_id]['preliminary_pass'] != 'true':
            raise ValueError(f'Unexpected release collection: {repo_id}')
        release_ids.add(repo_id)
        reasons, kept = release_filter(row['release_collection_status'], releases[row['release_collection_id']], settings)
        decisions[repo_id]['release_pass'] = str(not reasons).lower()
        decisions[repo_id]['release_reasons'] = '|'.join(reasons)
        retained_releases += len(kept)
    if release_ids != {repo_id for repo_id, row in decisions.items() if row['preliminary_pass'] == 'true'}:
        raise ValueError('Release collection does not cover the preliminary selection')
    del releases

    literal_keys = set()
    for row in read_csv(data_dir / 'literal_checks.csv.gz'):
        decision = decisions[row['github_repo_id']]
        if decision['release_pass'] != 'true':
            raise ValueError('Literal check outside the release selection')
        if row['occurrence_extraction_status'] == 'extracted':
            if int(row['occurrence_count']) < 1:
                raise ValueError('Extracted literal has no occurrence')
            literal_keys.add(model_key(row))
    literal_repos = {key[0] for key in literal_keys}
    for repo_id, row in decisions.items():
        if row['release_pass'] == 'true':
            row['exact_literal_found'] = str(repo_id in literal_repos).lower()

    # One file version can occur in several queries and collected commits.
    file_keys = set()
    search_repos = set()
    fetched_counts = Counter()
    snapshot_models = defaultdict(set)
    repo_ids = {}
    match_count = 0
    for row in read_csv(data_dir / 'search_matches.csv.gz'):
        query = queries[row['query_row_id']]
        repo_id = row['github_repo_id']
        file_keys.add((repo_id, row['file_path'], row['file_sha']))
        search_repos.add(repo_id)
        fetched_counts[row['query_row_id']] += 1
        match_count += 1
        row['model_id'] = query['model_id']
        if model_key(row) not in literal_keys:
            continue
        parts = urlsplit(row['match_url']).path.split('/')
        if len(parts) < 6 or parts[3] != 'blob':
            raise ValueError(f"Missing fixed commit: {row['match_url']}")
        commit = parts[4]
        if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
            raise ValueError(f'Not a commit SHA: {commit}')
        repo = row['repository_full_name']
        key = repo, row['file_path'], row['file_sha'], commit
        snapshot_models[key].add(row['model_id'])
        repo_ids[repo] = repo_id
    if search_repos != set(decisions):
        raise ValueError('Search repositories differ from the metadata input')
    for query_id, row in queries.items():
        # The original collector removed duplicate items within each query.
        if fetched_counts[query_id] > int(row['item_count_fetched']):
            raise ValueError(f'More saved matches than fetched items: {query_id}')

    checks = defaultdict(list)
    checked_keys = set()
    for row in read_csv(data_dir / 'snapshot_checks.csv.gz'):
        repo = row['repository_full_name']
        key = repo, row['file_path'], row['stored_file_sha'], row['source_snapshot_commit_sha']
        if key in checked_keys or set(row['models'].split('|')) != snapshot_models[key]:
            raise ValueError(f'Snapshot evidence differs: {key}')
        checked_keys.add(key)
        checks[repo].append(row)
    if checked_keys != set(snapshot_models):
        raise ValueError('Snapshot checks do not cover the selected search results')

    files = []
    repositories = []
    for repo, rows in sorted(checks.items()):
        latest_commits = {row['latest_snapshot_commit_sha'] for row in rows}
        if len(latest_commits) != 1:
            raise ValueError(f'Conflicting latest commits: {repo}')
        latest = latest_commits.pop()
        collapse = bool(latest) and all(
            row['source_snapshot_status'] != 'source_snapshot_mismatch'
            and row['latest_snapshot_file_sha'] == row['stored_file_sha'] for row in rows)
        repo_files = []
        for row in rows:
            if not latest:
                continue
            if not collapse and row['source_snapshot_status'] == 'source_snapshot_unavailable':
                continue
            commit = latest if collapse else row['source_snapshot_commit_sha']
            actual_blob = row['latest_snapshot_file_sha'] if collapse else row['source_snapshot_file_sha']
            if actual_blob != row['stored_file_sha']:
                raise ValueError(f"File content mismatch: {repo}/{row['file_path']}")
            repo_files.append({
                'repository': repo, 'commit_sha': commit, 'path': row['file_path'],
                'blob_sha': row['stored_file_sha'], 'model_ids': row['models'],
                'url': f"https://github.com/{repo}/blob/{commit}/{quote(row['file_path'], safe='/')}",
            })
        decisions[repo_ids[repo]]['snapshot_available'] = str(bool(repo_files)).lower()
        if not repo_files:
            continue
        files.extend(repo_files)
        repositories.append({
            'github_repo_id': repo_ids[repo], 'repository': repo, 'commit_sha': latest,
            'url': f'https://github.com/{repo}/tree/{latest}',
            'collected_file_versions': len(repo_files),
            'files_at_selected_commit': sum(row['commit_sha'] == latest for row in repo_files),
            'saved_snapshots': len({row['commit_sha'] for row in repo_files}),
        })

    files.sort(key=lambda row: (row['repository'], row['commit_sha'], row['path']))
    counts = {
        'search_queries': len(queries), 'search_matches': match_count,
        'discovered_repositories': len(search_repos), 'discovered_file_versions': len(file_keys),
        'preliminary_pass': sum(row['preliminary_pass'] == 'true' for row in decisions.values()),
        'release_pass': sum(row['release_pass'] == 'true' for row in decisions.values()),
        'retained_stable_releases': retained_releases,
        'repositories_with_exact_literals': len(literal_repos),
        'selected_repositories': len(repositories), 'selected_file_versions': len(files),
        'saved_snapshots': sum(row['saved_snapshots'] for row in repositories),
    }
    return repositories, files, sorted(decisions.values(), key=lambda row: row['repository']), counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=HERE)
    parser.add_argument('--collection-dir', type=Path, default=HERE,
                        help='saved study data (default), or a completed collect.py run')
    args = parser.parse_args()
    directory = args.collection_dir.resolve()
    if directory != HERE and args.output_dir.resolve() == HERE:
        parser.error('Choose --output-dir for the new collection. Do not replace the saved study outputs.')
    verify_inputs(directory)
    if directory == HERE:
        settings = json.loads((HERE / 'filter_settings.json').read_text())
    else:
        settings = json.loads((directory / 'run.json').read_text())['config']['settings']
    repositories, files, decisions, counts = calculate(directory / 'inputs', settings)
    queries = original_queries(read_csv(directory / 'inputs/query_splits.csv.gz'))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, rows in [('queries.csv', queries), ('repositories.csv', repositories),
                       ('files.csv', files), ('filter_decisions.csv.gz', decisions)]:
        write_csv(args.output_dir / name, rows, OUTPUT_FIELDS[name])
    print(json.dumps(counts, indent=2))


if __name__ == '__main__':
    main()
