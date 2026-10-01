#!/usr/bin/env python3
"""Collect new GitHub candidates using the study's search and filter rules."""

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from collect_occurrences import FIELDS as OCCURRENCE_FIELDS, collect_occurrences
from collect_snapshots import FIELDS as SNAPSHOT_FIELDS, checkout_snapshots, collect_checks, repo_name
from filter_rules import preliminary_filter, release_filter
from github_api import GitHub
from reproduce import HERE, OUTPUT_FIELDS, calculate, original_queries, query_text, read_csv, sha256, write_csv

STAGES = ['search', 'metadata', 'releases', 'files', 'snapshots']
FIELDS = {
    'query_splits.csv.gz': ['query_row_id', 'model_id', 'search_seed', 'search_seed_type',
                            'quote_style', 'query_text', 'size_min', 'size_max', 'total_count',
                            'incomplete_results', 'collection_status', 'item_count_fetched',
                            'checked_at_utc'],
    'search_matches.csv.gz': ['query_row_id', 'github_repo_id', 'repository_full_name',
                              'file_path', 'file_sha', 'match_url'],
    'repositories.csv.gz': ['github_repo_id', 'metadata_repo_id', 'input_full_name', 'full_name',
                            'metadata_collection_status', 'fork', 'size', 'stargazers_count',
                            'forks_count', 'language', 'pushed_at', 'topics', 'description',
                            'collected_at_utc'],
    'release_status.csv.gz': ['release_collection_id', 'github_repo_id', 'full_name',
                              'release_collection_status', 'collected_at_utc'],
    'releases.csv.gz': ['release_collection_id', 'github_repo_id', 'full_name', 'tag_name',
                        'draft', 'prerelease', 'published_at'],
    'literal_checks.csv.gz': ['github_repo_id', 'repository_full_name', 'file_path', 'file_sha',
                              'model_id', 'occurrence_count', 'occurrence_extraction_status'],
    'snapshot_checks.csv.gz': ['repository_full_name', 'file_path', 'source_snapshot_commit_sha',
                               'latest_snapshot_commit_sha', 'stored_file_sha', 'source_snapshot_file_sha',
                               'latest_snapshot_file_sha', 'source_snapshot_status',
                               'latest_snapshot_status', 'models'],
}


def save_csv(path, rows, fields):
    # Keep the last complete file if a run is interrupted.
    temporary = path.with_name('pending_' + path.name)
    write_csv(temporary, rows, fields)
    temporary.replace(path)


def save_json(path, data):
    temporary = path.with_name('pending_' + path.name)
    temporary.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def search_forms(model_id):
    short_id = model_id.split('/', 1)[1] if '/' in model_id else model_id.lstrip('~')
    for seed, seed_type in [(model_id, 'openrouter_model_id'), (short_id, 'namespace_free_model_id')]:
        for style in ['double', 'single']:
            yield {'model_id': model_id, 'search_seed': seed, 'search_seed_type': seed_type,
                   'quote_style': style}


def search_slices(api, form, low=0, high=384000):
    row = dict(form, size_min=low, size_max=high)
    row['query_text'] = query_text(row)
    data, checked_at, _ = api.get('/search/code', {'q': row['query_text'], 'per_page': 1,
                                                  'page': 1, 'sort': 'indexed', 'order': 'desc'})
    count = int(data['total_count'])
    row.update(total_count=count, incomplete_results=str(data['incomplete_results']).lower(),
               item_count_fetched=0, checked_at_utc=checked_at)
    if count == 0:
        row['collection_status'] = 'count_zero'
    elif count <= 1000:
        row['collection_status'] = 'fetch_ready'
    elif low == high:
        row['collection_status'] = 'capped_unresolved'
    else:
        row['collection_status'] = 'split_parent'
    yield row
    if row['collection_status'] == 'split_parent':
        middle = (low + high) // 2
        yield from search_slices(api, form, low, middle)
        yield from search_slices(api, form, middle + 1, high)


def collect_search(api, ids, inputs):
    queries = []

    def matches():
        for index, model_id in enumerate(ids, 1):
            print(f'Search {index}/{len(ids)}: {model_id}', flush=True)
            for form in search_forms(model_id):
                # Plan every size slice before fetching, as in the original collector.
                for row in list(search_slices(api, form)):
                    row['query_row_id'] = f'query_{len(queries) + 1:08d}'
                    if row['collection_status'] == 'fetch_ready':
                        seen = set()
                        pages = (row['total_count'] + 99) // 100
                        for page in range(1, pages + 1):
                            data, _, _ = api.get('/search/code', {'q': row['query_text'], 'per_page': 100,
                                                                 'page': page, 'sort': 'indexed', 'order': 'desc'})
                            if data['incomplete_results']:
                                row['incomplete_results'] = 'true'
                            row['item_count_fetched'] += len(data['items'])
                            for item in data['items']:
                                repo = item['repository']
                                key = str(repo['id']), item['path'], item['sha']
                                if key in seen:
                                    continue
                                seen.add(key)
                                yield {'query_row_id': row['query_row_id'], 'github_repo_id': key[0],
                                       'repository_full_name': repo_name(repo['full_name']),
                                       'file_path': item['path'], 'file_sha': item['sha'],
                                       'match_url': item['html_url']}
                            if len(data['items']) < 100:
                                break
                        row['collection_status'] = 'fetched'
                    queries.append(row)

    save_csv(inputs / 'search_matches.csv.gz', matches(), FIELDS['search_matches.csv.gz'])
    save_csv(inputs / 'query_splits.csv.gz', queries, FIELDS['query_splits.csv.gz'])
    save_csv(inputs.parent / 'queries.csv', original_queries(queries), OUTPUT_FIELDS['queries.csv'])
    warnings = sum(row['incomplete_results'] == 'true' or row['collection_status'] == 'capped_unresolved'
                   for row in queries)
    print(f'{len(queries)} queries logged; {warnings} incomplete or capped queries.', flush=True)


def collect_metadata(api, inputs):
    repos = {}
    for row in read_csv(inputs / 'search_matches.csv.gz'):
        repos.setdefault(row['github_repo_id'], row['repository_full_name'])

    def rows():
        for index, (repo_id, name) in enumerate(sorted(repos.items(), key=lambda item: item[1]), 1):
            data, checked_at, status = api.get('/repos/' + quote(name, safe='/'))
            row = dict.fromkeys(FIELDS['repositories.csv.gz'], '')
            row.update(github_repo_id=repo_id, input_full_name=name, full_name=name,
                       metadata_collection_status='collected' if status == 200 else 'not_found',
                       collected_at_utc=checked_at)
            if data is not None:
                for field in ['full_name', 'size', 'stargazers_count', 'forks_count', 'language',
                              'pushed_at', 'description']:
                    row[field] = data.get(field) or ''
                row.update(metadata_repo_id=data['id'], fork=str(data['fork']).lower(),
                           topics=','.join(data.get('topics', [])))
            if index % 100 == 0 or index == len(repos):
                print(f'Repository metadata {index}/{len(repos)}', flush=True)
            yield row

    save_csv(inputs / 'repositories.csv.gz', rows(), FIELDS['repositories.csv.gz'])


def collect_releases(api, inputs, settings):
    statuses = []

    def rows():
        selected = [row for row in read_csv(inputs / 'repositories.csv.gz')
                    if not preliminary_filter(row, settings)]
        for index, repo in enumerate(selected, 1):
            collection_id = f'release_collection_{index:08d}'
            name = repo['input_full_name']
            page = 1
            while True:
                data, checked_at, status = api.get('/repos/' + quote(name, safe='/') + '/releases',
                                                   {'per_page': 100, 'page': page})
                if status == 404:
                    break
                if not isinstance(data, list):
                    raise ValueError(f'Unexpected releases response: {name}')
                for release in data:
                    yield {'release_collection_id': collection_id, 'github_repo_id': repo['github_repo_id'],
                           'full_name': name, 'tag_name': release['tag_name'],
                           'draft': str(release['draft']).lower(), 'prerelease': str(release['prerelease']).lower(),
                           'published_at': release.get('published_at') or ''}
                if len(data) < 100:
                    break
                page += 1
            statuses.append({'release_collection_id': collection_id, 'github_repo_id': repo['github_repo_id'],
                             'full_name': name, 'release_collection_status': 'collected' if status == 200 else 'not_found',
                             'collected_at_utc': checked_at})
            print(f'Release metadata {index}/{len(selected)}: {name}', flush=True)

    save_csv(inputs / 'releases.csv.gz', rows(), FIELDS['releases.csv.gz'])
    save_csv(inputs / 'release_status.csv.gz', statuses, FIELDS['release_status.csv.gz'])


def literal_count(text, specs):
    count = 0
    for seed, style in set(specs):
        mark = '"' if style == 'double' else "'"
        needle = mark + seed + mark
        start = 0
        while True:
            position = text.find(needle, start)
            if position < 0:
                break
            count += 1
            start = position + 1
    return count


def collect_files(api, inputs, settings):
    releases = defaultdict(list)
    for row in read_csv(inputs / 'releases.csv.gz'):
        releases[row['release_collection_id']].append(row)
    selected = set()
    for row in read_csv(inputs / 'release_status.csv.gz'):
        reasons, _ = release_filter(row['release_collection_status'], releases[row['release_collection_id']], settings)
        if not reasons:
            selected.add(row['github_repo_id'])
    queries = {row['query_row_id']: row for row in read_csv(inputs / 'query_splits.csv.gz')}
    files = defaultdict(lambda: defaultdict(set))
    for row in read_csv(inputs / 'search_matches.csv.gz'):
        if row['github_repo_id'] not in selected:
            continue
        key = row['github_repo_id'], row['repository_full_name'], row['file_path'], row['file_sha']
        query = queries[row['query_row_id']]
        files[key][query['model_id']].add((query['search_seed'], query['quote_style']))

    def rows():
        for index, ((repo_id, repo, path, blob), models) in enumerate(sorted(files.items()), 1):
            if not re.fullmatch('[0-9a-f]{40}', blob):
                raise ValueError(f'Invalid blob SHA: {repo} {path}')
            data, _, status = api.get(f'/repos/{quote(repo_name(repo), safe="/")}/git/blobs/{blob}')
            text = None
            if status == 200:
                if data.get('encoding') != 'base64':
                    raise ValueError(f'Unexpected blob encoding: {repo} {blob}')
                content = base64.b64decode(''.join(data['content'].split()), validate=True)
                digest = hashlib.sha1(f'blob {len(content)}\0'.encode() + content).hexdigest()
                if digest != blob:
                    raise ValueError(f'Downloaded blob does not match search result: {repo} {path}')
                destination = inputs.parent / 'source_files' / repo / f'{blob}.py.gz'
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(gzip.compress(content, mtime=0))
                try:
                    text = content.decode('utf-8')
                except UnicodeDecodeError:
                    text = content.decode('latin-1')
            for model, specs in sorted(models.items()):
                count = literal_count(text, specs) if text is not None else 0
                outcome = 'extracted' if count else 'no_exact_occurrence'
                if text is None:
                    outcome = 'missing_content'
                yield {'github_repo_id': repo_id, 'repository_full_name': repo, 'file_path': path,
                       'file_sha': blob, 'model_id': model, 'occurrence_count': count,
                       'occurrence_extraction_status': outcome}
            if index % 100 == 0 or index == len(files):
                print(f'Candidate file downloads {index}/{len(files)}', flush=True)

    save_csv(inputs / 'literal_checks.csv.gz', rows(), FIELDS['literal_checks.csv.gz'])
    save_csv(inputs.parent / 'occurrences.csv.gz', collect_occurrences(inputs), OCCURRENCE_FIELDS)


def collect_snapshots(inputs):
    queries = {row['query_row_id']: row for row in read_csv(inputs / 'query_splits.csv.gz')}
    literals = {(row['github_repo_id'], row['file_path'], row['file_sha'], row['model_id'])
                for row in read_csv(inputs / 'literal_checks.csv.gz')
                if row['occurrence_extraction_status'] == 'extracted'}
    rows = collect_checks(read_csv(inputs / 'search_matches.csv.gz'), queries, literals,
                          inputs.parent / 'cache/git')
    save_csv(inputs / 'snapshot_checks.csv.gz', rows, FIELDS['snapshot_checks.csv.gz'])


def export_selection(directory, settings):
    repositories, files, decisions, counts = calculate(directory / 'inputs', settings)
    for name, rows in [('repositories.csv', repositories), ('files.csv', files),
                       ('filter_decisions.csv.gz', decisions)]:
        save_csv(directory / name, rows, OUTPUT_FIELDS[name])
    save_json(directory / 'summary.json', counts)
    print(json.dumps(counts, indent=2))
    return repositories, files


def prepare_run(directory, ids, settings, resume):
    package = HERE.parents[1]
    if directory == package or package in directory.parents:
        raise ValueError('Use an output directory outside the replication package.')
    code = {name: sha256(HERE / name) for name in
            ['collect.py', 'github_api.py', 'collect_occurrences.py', 'collect_snapshots.py',
             'reproduce.py', 'filter_rules.py']}
    config = {'model_ids': ids, 'settings': settings, 'code_sha256': code}
    if resume:
        state = json.loads((directory / 'run.json').read_text())
        if state['config'] != config:
            raise ValueError('Run settings or scripts changed. Use a new output directory.')
        for name, digest in state['completed_files'].items():
            if sha256(directory / name) != digest:
                raise ValueError(f'Completed output changed: {name}')
    else:
        if directory.exists() and any(directory.iterdir()):
            raise ValueError('Output directory is not empty. Use --resume for an existing run.')
        directory.mkdir(parents=True, exist_ok=True)
        state = {'started_at_utc': datetime.now(timezone.utc).isoformat(), 'config': config,
                 'completed_stages': [], 'completed_files': {}}
        save_json(directory / 'run.json', state)
    (directory / 'inputs').mkdir(exist_ok=True)
    (directory / 'cache').mkdir(exist_ok=True)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True, help='new directory outside this package')
    parser.add_argument('--ids', type=Path, default=HERE.parent / 'ptm_catalogue/discovery_ids.csv')
    parser.add_argument('--limit-models', type=int, help='use the first N IDs for a small trial')
    parser.add_argument('--stop-after', choices=STAGES, default='snapshots')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    ids = list(dict.fromkeys(row['model_id'] for row in read_csv(args.ids)))
    if args.limit_models is not None:
        if args.limit_models < 1:
            parser.error('--limit-models must be positive')
        ids = ids[:args.limit_models]
    if not ids:
        parser.error('The ID list is empty')
    token = os.environ.get('GITHUB_TOKEN', '').strip()
    if not token:
        parser.error('Set GITHUB_TOKEN in the environment. Do not put it in a script or CSV.')
    settings = json.loads((HERE / 'filter_settings.json').read_text())
    directory = args.output_dir.resolve()
    state = prepare_run(directory, ids, settings, args.resume)
    api = GitHub(token, directory / 'cache/github.sqlite')
    inputs = directory / 'inputs'
    try:
        for stage in STAGES:
            if stage not in state['completed_stages']:
                print(f'Starting {stage}', flush=True)
                if stage == 'search':
                    collect_search(api, ids, inputs)
                elif stage == 'metadata':
                    collect_metadata(api, inputs)
                elif stage == 'releases':
                    collect_releases(api, inputs, settings)
                elif stage == 'files':
                    collect_files(api, inputs, settings)
                else:
                    collect_snapshots(inputs)
                state['completed_stages'].append(stage)
                state['completed_files'] = {str(path.relative_to(directory)): sha256(path)
                                             for path in sorted(inputs.glob('*.csv.gz'))
                                             if not path.name.startswith('pending_')}
                if (directory / 'occurrences.csv.gz').exists():
                    state['completed_files']['occurrences.csv.gz'] = sha256(directory / 'occurrences.csv.gz')
                save_json(directory / 'run.json', state)
            if stage == args.stop_after:
                break
        if 'snapshots' in state['completed_stages']:
            repositories, files = export_selection(directory, settings)
            save_csv(directory / 'snapshots.csv', checkout_snapshots(directory, repositories, files),
                     SNAPSHOT_FIELDS)
            state['completed_files']['snapshots.csv'] = sha256(directory / 'snapshots.csv')
            save_json(directory / 'run.json', state)
        else:
            print(f'Stopped after {args.stop_after}. Resume without --stop-after to continue.', flush=True)
    finally:
        api.close()


if __name__ == '__main__':
    main()
