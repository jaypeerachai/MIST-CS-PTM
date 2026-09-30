"""Check discovered file versions against their Git commits."""

import os
import re
import subprocess
from collections import defaultdict
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit


def repo_name(name):
    parts = name.split('/')
    if len(parts) != 2 or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part)
                              or part in {'.', '..'} for part in parts):
        raise ValueError(f'Invalid repository name: {name}')
    return name


def commit_from_url(url, repo, path):
    parsed = urlsplit(url)
    parts = unquote(parsed.path).split('/')
    if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or len(parts) < 6
            or '/'.join(parts[1:3]).lower() != repo.lower() or parts[3] != 'blob'
            or not re.fullmatch('[0-9a-f]{40}', parts[4]) or '/'.join(parts[5:]) != path):
        raise ValueError(f'Search result has no matching fixed commit URL: {url}')
    if PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
        raise ValueError(f'Invalid source path: {path}')
    return parts[4]


def git(directory, *args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(['git', '-c', f'core.hooksPath={os.devnull}', *args],
                          cwd=directory, env=env, capture_output=True, text=True, timeout=600)


def prepare_repo(root, repo):
    directory = root / repo_name(repo)
    if not (directory / '.git').exists():
        directory.mkdir(parents=True, exist_ok=True)
        for command in [('init',), ('remote', 'add', 'origin', f'https://github.com/{repo}.git')]:
            if git(directory, *command).returncode:
                raise RuntimeError(f'Cannot prepare Git cache for {repo}')
    return directory


def check_repo(directory, repo, rows):
    commits = sorted({row['commit'] for row in rows})
    times = {}
    for commit in commits:
        exists = git(directory, 'cat-file', '-e', f'{commit}^{{commit}}').returncode == 0
        if not exists and git(directory, 'fetch', '--depth=1', 'origin', commit).returncode:
            print(f'Commit unavailable: {repo} {commit}', flush=True)
            continue
        result = git(directory, 'show', '-s', '--format=%ct', commit)
        if result.returncode:
            raise RuntimeError(f'Cannot read commit date: {repo} {commit}')
        times[commit] = int(result.stdout.strip())
    latest = max(times, key=times.get) if times else ''

    def blob(commit, path):
        if commit not in times:
            return ''
        result = git(directory, 'rev-parse', f'{commit}:{path}')
        return result.stdout.strip() if result.returncode == 0 else ''

    for row in rows:
        source_blob = blob(row['commit'], row['path'])
        latest_blob = blob(latest, row['path'])
        source_status = 'source_snapshot_unavailable'
        if row['commit'] in times:
            source_status = 'matches_source_snapshot' if source_blob == row['blob'] else 'source_snapshot_mismatch'
        latest_status = 'latest_snapshot_unavailable'
        if latest:
            latest_status = 'matches_latest_snapshot' if latest_blob == row['blob'] else 'changed_at_latest_snapshot'
            if not latest_blob:
                latest_status = 'missing_at_latest_snapshot'
        yield {'repository_full_name': repo, 'file_path': row['path'],
               'source_snapshot_commit_sha': row['commit'], 'latest_snapshot_commit_sha': latest,
               'stored_file_sha': row['blob'], 'source_snapshot_file_sha': source_blob,
               'latest_snapshot_file_sha': latest_blob, 'source_snapshot_status': source_status,
               'latest_snapshot_status': latest_status, 'models': '|'.join(sorted(row['models']))}


def collect_checks(matches, queries, literal_keys, cache):
    files = defaultdict(set)
    for row in matches:
        model = queries[row['query_row_id']]['model_id']
        if (row['github_repo_id'], row['file_path'], row['file_sha'], model) not in literal_keys:
            continue
        repo = repo_name(row['repository_full_name'])
        commit = commit_from_url(row['match_url'], repo, row['file_path'])
        files[repo, row['file_path'], row['file_sha'], commit].add(model)
    repos = defaultdict(list)
    for (repo, path, blob, commit), models in sorted(files.items()):
        repos[repo].append({'path': path, 'blob': blob, 'commit': commit, 'models': models})
    for index, (repo, rows) in enumerate(sorted(repos.items()), 1):
        print(f'Snapshot checks {index}/{len(repos)}: {repo}', flush=True)
        yield from check_repo(prepare_repo(cache, repo), repo, rows)
