"""Run the installed MIST package on the evaluation snapshots."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from classification.evaluate import DATASETS, load_dataset, load_repositories, read_csv

ROOT = Path(__file__).resolve().parent


def snapshot_name(repository, commit):
    return repository.replace('/', '__') + '_' + commit[:12]


def match_decisions(cases, decisions):
    index = {}
    for row in decisions:
        key = (row['path'], int(row['line']), int(row['column']), row['ptm_id'])
        if key in index:
            raise ValueError(f'Duplicate occurrence in MIST output: {key}')
        if row['confirmed_reuse'] not in ('True','False'):
            raise ValueError(f'Invalid reuse decision: {key}')
        index[key] = row
    result = []
    for case_id, case in cases.items():
        key = (case['file_path'], int(case['line']), int(case['column_start']), case['ptm_id'])
        if key not in index:
            raise ValueError(f'No exact occurrence match for {case_id}: {key}')
        result.append(dict(case_id=case_id, prediction='real_reuse' if index[key]['confirmed_reuse']=='True' else 'non_reuse'))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots', type=Path, help='checkouts in owner__repo/full_commit folders')
    parser.add_argument('--output', type=Path, required=True, help='result directory outside the checkouts')
    parser.add_argument('--model-ids', type=Path, required=True, help='your vocabulary CSV with a model_id column')
    parser.add_argument('--dataset', choices=[*DATASETS,'all'], default='all')
    parser.add_argument('--repository', help='run or collect one repository only')
    parser.add_argument('--full', action='store_true', help='retain graphs for reachability')
    parser.add_argument('--collect-only', action='store_true', help='match existing completed decisions to the cases')
    args = parser.parse_args()
    if not args.collect_only and args.snapshots is None:
        parser.error('--snapshots is required unless --collect-only is used')
    vocabulary = args.model_ids.resolve()
    if not vocabulary.is_file():
        parser.error(f'Vocabulary file not found: {vocabulary}')
    expected = hashlib.sha256(vocabulary.read_bytes()).hexdigest()
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents:
        parser.error('Keep run outputs outside the packaged evaluation folder')
    jobs = load_repositories(args.dataset, args.repository)
    if not jobs:
        parser.error('No matching repositories')
    output.mkdir(parents=True, exist_ok=True)
    datasets = {name:load_dataset(name)[0] for name in {r['dataset'] for r in jobs}}
    predictions, failures = [], []
    for position, job in enumerate(jobs,1):
        name = snapshot_name(job['repository'],job['commit'])
        result = output / name
        print(f'{position}/{len(jobs)} {job["repository"]}', flush=True)
        try:
            marker = result / 'summary.json'
            if not marker.exists() and not args.collect_only:
                if result.exists():
                    raise ValueError('Incomplete output already exists; preserve it and choose a new output root')
                repo = (args.snapshots / job['repository'].replace('/','__') / job['commit']).resolve()
                if output == repo or repo in output.parents:
                    raise ValueError('Output must be outside the analyzed checkout')
                command = [sys.executable,'-B','-m','mist','--repo',str(repo),'--commit',job['commit'],
                           '--repository',job['repository'],'--model-ids',str(vocabulary),'--output',str(result)]
                if args.full:
                    command.append('--full')
                with (output / (name+'.log')).open('w') as log:
                    env = dict(os.environ, PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1')
                    subprocess.run(command, check=True, env=env, stdout=log, stderr=subprocess.STDOUT)
            summary = json.loads(marker.read_text())
            if (summary['repository'],summary['commit']) != (job['repository'],job['commit']):
                raise ValueError('Result repository or commit does not match')
            if summary['model_id_vocabulary']['sha256'] != expected:
                raise ValueError('Existing result used a different vocabulary; supply the original file or use a new output directory')
            if args.full and not (result/'binding_graph.csv').exists():
                raise ValueError('Full graph output is missing')
            cases = {k:r for k,r in datasets[job['dataset']].items() if r['repository']==job['repository']}
            predictions.extend(match_decisions(cases, read_csv(result/'decisions.csv')))
        except (OSError,ValueError,KeyError,subprocess.CalledProcessError) as error:
            failures.append(dict(repository=job['repository'],commit=job['commit'],reason=str(error)))
            print(f'  Not completed: {error}', flush=True)
    if failures:
        (output/'failures.json').write_text(json.dumps(failures,indent=2)+'\n')
        raise SystemExit(f'{len(failures)} snapshots incomplete. No complete prediction file written; failures are not negative predictions.')
    path = output / 'mist_predictions.csv'
    with path.open('x',newline='',encoding='utf-8') as handle:
        writer=csv.DictWriter(handle,fieldnames=['case_id','prediction'])
        writer.writeheader()
        writer.writerows(predictions)
    print(f'{len(predictions)} predictions: {path}')


if __name__ == '__main__':
    main()
