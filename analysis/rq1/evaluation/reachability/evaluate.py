"""Compare source-file recovery from the same annotated PTM-use sinks."""

import argparse
import csv
from collections import defaultdict, deque
import gc
import gzip
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
METHODS = ('drosos', 'yasmin', 'mist')
csv.field_size_limit(10_000_000)


def read_csv(path):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def node_location(node):
    parts = node.split('|')
    for index, value in enumerate(parts[:-1]):
        if value.endswith('.py') and parts[index+1].isdigit():
            return value, int(parts[index+1])
    return None


class Graph:
    def __init__(self, path, modules):
        self.predecessors = defaultdict(set)
        nodes = set()
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt', newline='', encoding='utf-8') as handle:
            for row in csv.DictReader(handle):
                self.predecessors[row['target']].add(row['source'])
                nodes.update((row['source'],row['target']))
        self.by_location = defaultdict(set)
        for node in nodes:
            location = node_location(node)
            if location:
                self.by_location[location].add(node)
        self.module_files = defaultdict(set)
        for row in modules:
            if row['module'] and row['file_path']:
                self.module_files[row['module']].add(row['file_path'])

    def node_files(self, node):
        location = node_location(node)
        if location:
            return {location[0]}
        files = set()
        for part in node.split('|'):
            scope = part
            while '.' in scope:
                files.update(self.module_files.get(scope,set()))
                scope = scope.rsplit('.',1)[0]
            files.update(self.module_files.get(scope,set()))
        return files

    def files_from_sinks(self, locations):
        matched = set()
        for location in locations:
            for line in range(int(location['start_line']),int(location['end_line'])+1):
                matched.update(self.by_location.get((location['file_path'],line),set()))
        explicit = {node for node in matched if node.startswith('sink|')}
        seeds = explicit or matched
        visited = set(seeds)
        queue = deque(sorted(seeds))
        while queue:
            node = queue.popleft()
            for previous in sorted(self.predecessors.get(node,set())):
                if previous not in visited:
                    visited.add(previous)
                    queue.append(previous)
        files = {path for node in visited for path in self.node_files(node)}
        # The supplied sink file is part of the returned context.
        files.update(location['file_path'] for location in locations)
        return files


def measure(cases, results):
    expected = {case['case_id']:case for case in cases}
    grouped = defaultdict(list)
    seen = set()
    for row in results:
        identity = row['case_id'],row['method']
        if row['method'] not in METHODS:
            raise ValueError(f'Unknown reachability method: {row["method"]}')
        if identity in seen or row['case_id'] not in expected:
            raise ValueError(f'Duplicate or unknown reachability case: {identity}')
        seen.add(identity)
        case = expected[row['case_id']]
        files = set(filter(None,row['candidate_files'].split('|')))
        if len(files) != int(row['candidate_file_count']):
            raise ValueError(f'Candidate count does not match file list: {identity}')
        hit = case['source_file'] in files
        if str(hit) != str(row['source_file_retrieved']):
            raise ValueError(f'Source-file flag does not match returned files: {identity}')
        # Keep partial outputs in the input, but score incomplete cases as misses.
        if row['status'] != 'ok':
            files = set()
            hit = False
        gold = set(case['path_files'].split('|'))
        grouped[row['method']].append((hit,len(files),gold <= files,len(gold & files)/len(gold)))
    output = []
    for method, rows in sorted(grouped.items()):
        if len(rows) != len(cases):
            raise ValueError(f'{method} is missing cases')
        output.append(dict(method=method,cases=len(rows),source_files_recovered=sum(r[0] for r in rows),
            recovery=sum(r[0] for r in rows)/len(rows),median_files=statistics.median(r[1] for r in rows),
            all_path_files_recovered=sum(r[2] for r in rows),mean_path_file_recall=statistics.fmean(r[3] for r in rows)))
    return output


def from_graphs(cases, graphs):
    modules = read_csv(ROOT/'modules.csv.gz')
    grouped = defaultdict(list)
    for case in cases:
        grouped[case['repository'],case['commit']].append(case)
    results = []
    for (repository,commit), selected in sorted(grouped.items()):
        name = repository.replace('/','__')+'_'+commit[:12]
        print(f'Reading {repository}',flush=True)
        path = graphs/name/'binding_graph.csv'
        if not path.is_file():
            path = path.with_suffix('.csv.gz')
        graph = Graph(path,
                      [r for r in modules if r['repository']==repository and r['commit']==commit])
        for case in selected:
            files = graph.files_from_sinks(json.loads(case['sink_locations']))
            results.append(dict(case_id=case['case_id'],method='mist',status='ok',
                source_file_retrieved=str(case['source_file'] in files),
                candidate_file_count=len(files),candidate_files='|'.join(sorted(files)),candidate_function_count=''))
        del graph
        gc.collect()
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',type=Path,default=ROOT/'results.csv')
    parser.add_argument('--graphs',type=Path,help='recalculate MIST from --full outputs; baseline sets stay unchanged')
    parser.add_argument('--output',type=Path,help='write recalculated per-case results to a new CSV')
    args=parser.parse_args()
    cases=read_csv(ROOT/'cases.csv')
    results=read_csv(args.results)
    if args.graphs:
        results=[r for r in results if r['method']!='mist']+from_graphs(cases,args.graphs)
    print(f'{"Method":12} {"Source recovery":18} {"Median files":14} {"All path files":15}')
    summaries = measure(cases,results)
    for row in summaries:
        print(f'{row["method"]:12} {row["source_files_recovered"]}/{row["cases"]} ({row["recovery"]:.1%})'
              f'       {row["median_files"]:5g}          {row["all_path_files_recovered"]}/{row["cases"]}')
    methods = [method for method in METHODS if any(row['method']==method for row in summaries)]
    for bound in (5,10,25,100):
        print(f'Within {bound} files: '+', '.join(f'{method} '+str(sum(
            r['status']=='ok' and r['source_file_retrieved']=='True' and int(r['candidate_file_count'])<=bound
            for r in results if r['method']==method))+f'/{len(cases)}' for method in methods))
    if args.output:
        with args.output.open('x',newline='',encoding='utf-8') as handle:
            writer=csv.DictWriter(handle,fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(results)


if __name__ == '__main__':
    main()
