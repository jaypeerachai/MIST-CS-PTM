"""Run a caller baseline from the annotated sinks at fixed repository commits."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import psutil

from common import EVALUATION, HERE, checkout, read_csv, select_cases, source_file, write_csv
import yasmin


def annotation(row):
    seeds = tuple(yasmin.CodeLocation(s['file_path'], int(s['start_line']), s['url'])
                  for s in json.loads(row['sink_locations']))
    return yasmin.AnnotationCase(row['case_id'], '', row['repository'].replace('/', '__'), row['commit'],
        row['source_file'], int(row['source_line']), row['ptm_id'], False, '', seeds, (),
        len(row['path_files'].split('|')), '', row['source_url'])


def result(row, method, status, files=(), functions=0):
    files = set(files)
    return dict(case_id=row['case_id'], method=method, status=status,
                source_file_retrieved=str(row['source_file'] in files), candidate_file_count=len(files),
                candidate_files='|'.join(sorted(files)), candidate_function_count=functions)


def run_yasmin(repo, cases):
    tracer = yasmin.ReverseCallerTracer(repo, max_depth=6, reference_backend='indexed')
    cache = {}
    results = []
    for row in cases:
        case = annotation(row)
        records, statuses = [], []
        valid_seeds = 0
        for number, seed in enumerate(case.loader_locations, 1):
            key = seed.file_path, seed.line_number
            if key not in cache:
                cache[key] = tracer.trace_seed(case, number, seed)
            traced, status = cache[key]
            records.extend(yasmin.rebind_record(r, case, number) for r in traced)
            statuses.append(status)
            valid_seeds += status in {'ok', 'module_level_seed', 'timed_out'}
        found = yasmin.build_case_result(case, repo, tracer, yasmin.dedupe_records(records), statuses, valid_seeds, 0)
        results.append(result(row, 'yasmin', found.trace_status,
                              filter(None, found.candidate_files.split('|')), found.candidate_function_count))
    return results


def run_drosos(repo, cases, output):
    from drosos import FastenProjectGraph
    sys.path.insert(0, str(HERE / 'upstream/pycg'))
    from pycg import formats
    from pycg.pycg import CallGraphGenerator
    from pycg.utils.constants import CALL_GRAPH_OP

    excluded = {'test', 'tests', 'docs', 'examples'}
    entries = sorted(path.resolve() for path in repo.glob('**/*.py')
                     if path.is_file() and not any(part in excluded for part in path.parts[:-1]))
    generator = CallGraphGenerator([str(p) for p in entries], str(repo), -1, CALL_GRAPH_OP)
    generator.analyze()
    payload = formats.Fasten(generator, str(repo), cases[0]['repository'], 'PyPI', '1', 0).generate()
    with gzip.open(output / 'callgraph.json.gz', 'wt', encoding='utf-8') as handle:
        json.dump(payload, handle)
    graph = FastenProjectGraph(payload)
    index = yasmin.SourceIndex(repo)
    results = []
    for row in cases:
        seeds = set()
        for location in json.loads(row['sink_locations']):
            path, line = location['file_path'], int(location['start_line'])
            found, _ = graph.find_seed_nodes(path, line, index.enclosing_callable(path, line))
            seeds.update(found)
        if not seeds:
            results.append(result(row, 'drosos', 'missing_seed_namespace'))
            continue
        distances = graph.reverse_distances(seeds)
        results.append(result(row, 'drosos', 'ok', graph.candidate_files(distances),
                              len(graph.candidate_functions(distances))))
    return results


def process_tree_rss(pid):
    try:
        process = psutil.Process(pid)
        processes = [process, *process.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0
    total = 0
    for process in processes:
        try:
            total += process.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return total


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_repository(job, args):
    repo, cases = job
    identity = cases[0]
    folder = args.output / (identity['repository'].replace('/', '__') + '_' + identity['commit'][:12])
    folder.mkdir()
    inputs = folder / 'input.json'
    inputs.write_text(json.dumps(dict(method=args.method, repository=str(repo), cases=cases)), encoding='utf-8')
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker-input', str(inputs)]
    env = dict(os.environ)
    env.update({key: '1' for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                                    'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS')})
    started, peak, status = time.monotonic(), 0, 'ok'
    with (folder / 'run.log').open('x') as log:
        process = subprocess.Popen(command, cwd=folder, env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            while process.poll() is None:
                rss = process_tree_rss(process.pid)
                peak = max(peak, rss)
                if rss >= args.memory_gib * 1024**3:
                    status = 'memory_limited'
                    stop(process)
                    break
                if time.monotonic() - started >= args.timeout:
                    status = 'timed_out'
                    stop(process)
                    break
                time.sleep(0.1)
            if process.wait() != 0 and status == 'ok':
                status = 'failed'
        finally:
            if process.poll() is None:
                stop(process)
    metrics = dict(method=args.method, repository=identity['repository'], commit=identity['commit'],
                   status=status, elapsed_seconds=round(time.monotonic() - started, 4), peak_rss_bytes=peak)
    if status == 'ok':
        rows = read_csv(folder / 'results.csv')
    else:
        rows = [result(row, args.method, status) for row in cases]
    print(f'{identity["repository"]}: {status}', flush=True)
    return rows, metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--method', choices=['drosos', 'yasmin'])
    parser.add_argument('--cases', type=Path, default=EVALUATION / 'reachability/cases.csv')
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--snapshots', type=Path, help='root containing owner__repo/commit checkouts')
    inputs.add_argument('--checkout', type=Path, help='one checkout, used with --repository')
    parser.add_argument('--repository', help='owner/repository')
    parser.add_argument('--case-id', action='append')
    parser.add_argument('--output', type=Path, help='new output directory')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=43200, help='seconds per repository')
    parser.add_argument('--memory-gib', type=float, default=12, help='RSS limit per repository process tree')
    parser.add_argument('--worker-input', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_input:
        spec = json.loads(args.worker_input.read_text())
        repo, folder = Path(spec['repository']), args.worker_input.parent
        rows = run_yasmin(repo, spec['cases']) if spec['method'] == 'yasmin' else run_drosos(repo, spec['cases'], folder)
        write_csv(folder / 'results.csv', rows)
        return
    if not args.method or not args.output or not (args.snapshots or args.checkout):
        parser.error('--method, --output, and --snapshots or --checkout are required')
    if args.checkout and not args.repository:
        parser.error('--checkout requires --repository')
    if args.workers <= 0 or args.timeout <= 0 or args.memory_gib <= 0:
        parser.error('Workers, timeout, and memory limit must be positive')
    cases = select_cases(read_csv(args.cases), args.repository, args.case_id)
    if len({row['case_id'] for row in cases}) != len(cases):
        raise ValueError('Duplicate reachability case IDs')
    grouped = defaultdict(list)
    for row in cases:
        grouped[row['repository'], row['commit']].append(row)
    jobs = []
    for (repository, commit), selected in sorted(grouped.items()):
        repo = checkout(args.snapshots, repository, commit, args.checkout)
        for row in selected:
            for location in json.loads(row['sink_locations']):
                source_file(repo, location['file_path'])
        jobs.append((repo, selected))
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        completed = list(pool.map(lambda job: run_repository(job, args), jobs))
    write_csv(args.output / 'results.csv', [row for rows, _ in completed for row in rows])
    write_csv(args.output / 'runtime.csv', [metric for _, metric in completed])
    print(f'Wrote {len(cases)} cases. Wall time: {time.monotonic() - started:.1f}s')


if __name__ == '__main__':
    main()
