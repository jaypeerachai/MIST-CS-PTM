"""Summarize recorded completion, runtime, memory, and returned context."""

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
METHODS = ('drosos','yasmin','mist')
STATUSES = {'ok','failed','timed_out','memory_limited'}
csv.field_size_limit(10_000_000)


def read_csv(path):
    with path.open(newline='',encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def duration(seconds):
    hours,remainder = divmod(round(seconds),3600)
    minutes,seconds = divmod(remainder,60)
    return f'{hours:d}:{minutes:02d}:{seconds:02d}'


def measure_runtime(records,environment,cases):
    expected = {(r['repository'],r['commit']) for r in cases}
    if {r['method'] for r in records} != set(METHODS):
        raise ValueError('Expected measurements for all three methods')
    results = []
    for method in METHODS:
        rows = [r for r in records if r['method']==method]
        identities = {(r['repository'],r['commit']) for r in rows}
        if identities != expected or len(rows) != len(expected):
            raise ValueError(f'{method}: missing, duplicate, or unknown repository')
        counts = Counter(r['status'] for r in rows)
        if set(counts)-STATUSES:
            raise ValueError(f'{method}: unknown completion status')
        elapsed = [float(r['elapsed_seconds']) for r in rows]
        peaks = [int(r['peak_rss_bytes']) for r in rows]
        wall = float(environment['method_wall_seconds'][method])
        if any(not math.isfinite(v) or v<0 for v in [wall,*elapsed,*peaks]):
            raise ValueError(f'{method}: invalid time or memory value')
        results.append(dict(method=method,repositories=len(rows),completed=counts['ok'],
            timed_out=counts['timed_out'],memory_limited=counts['memory_limited'],failed=counts['failed'],
            wall_seconds=wall,wall_hms=duration(wall),peak_rss_bytes=max(peaks),peak_rss_gib=max(peaks)/(2**30),
            min_repository_seconds=min(elapsed),median_repository_seconds=statistics.median(elapsed),
            max_repository_seconds=max(elapsed)))
    return results


def measure_context(results,cases):
    expected = {r['case_id'] for r in cases}
    if {r['method'] for r in results} != set(METHODS):
        raise ValueError('Expected returned context for all three methods')
    output = []
    for method in METHODS:
        rows = [r for r in results if r['method']==method]
        if len(rows)!=len(expected) or {r['case_id'] for r in rows}!=expected:
            raise ValueError(f'{method}: missing, duplicate, or unknown case')
        # The paper reports PyCG context only when its graph and seed are usable.
        selected = [r for r in rows if r['status']=='ok'] if method=='drosos' else rows
        if not selected:
            raise ValueError(f'{method}: no usable context measurements')
        files = [int(r['candidate_file_count']) if r['status']=='ok' else 0 for r in selected]
        functions = [int(r['candidate_function_count']) if r['status']=='ok' else 0 for r in selected
                     if r['candidate_function_count']!='']
        if (method=='mist' and functions) or (method!='mist' and len(functions)!=len(selected)):
            raise ValueError(f'{method}: unexpected or missing function counts')
        output.append(dict(method=method,context_cases=len(selected),
            median_files=statistics.median(files),
            median_functions=statistics.median(functions) if functions else ''))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,help='write summary metrics to a new CSV')
    args = parser.parse_args()
    cases = read_csv(ROOT/'cases.csv')
    environment = json.loads((ROOT/'operational_environment.json').read_text())
    runtime = measure_runtime(read_csv(ROOT/'operational_metrics.csv'),environment,cases)
    context = measure_context(read_csv(ROOT/'results.csv'),cases)
    print(f'{"Method":10} {"Completed":10} {"Timeout":>8} {"Memory cap":>10} {"Failed":>7} {"Wall h:mm:ss":>13} {"Peak GiB":>10}')
    for row in runtime:
        print(f'{row["method"]:10} {str(row["completed"])+"/"+str(row["repositories"]):10} '
              f'{row["timed_out"]:8} {row["memory_limited"]:10} {row["failed"]:7} '
              f'{row["wall_hms"]:>13} {row["peak_rss_gib"]:10.2f}')
    print(f'\n{"Method":10} {"Context cases":>14} {"Median files":>14} {"Median functions":>18}')
    for row in context:
        functions = row['median_functions'] if row['median_functions']!='' else 'not reported'
        print(f'{row["method"]:10} {row["context_cases"]:14} {row["median_files"]:14g} {str(functions):>18}')
    print('\nRepository completion and usable case context are separate measures.')
    if args.output:
        combined = [dict(**time,**{k:v for k,v in count.items() if k!='method'})
                    for time,count in zip(runtime,context)]
        with args.output.open('x',newline='',encoding='utf-8') as handle:
            writer = csv.DictWriter(handle,fieldnames=list(combined[0]))
            writer.writeheader()
            writer.writerows(combined)


if __name__=='__main__':
    main()
