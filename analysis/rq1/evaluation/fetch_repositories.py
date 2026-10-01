"""Download clean evaluation checkouts at their recorded commits."""

import argparse
import os
from pathlib import Path
import re
import subprocess

from classification.evaluate import DATASETS, load_repositories


def git(directory, *arguments):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.run(['git','-c',f'core.hooksPath={os.devnull}',*arguments],
                          cwd=directory,env=env,check=True,capture_output=True,text=True,timeout=900)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dataset',choices=[*DATASETS,'all'],default='all')
    parser.add_argument('--repository',help='download only this owner/repository')
    args=parser.parse_args()
    jobs=load_repositories(args.dataset,args.repository)
    if not jobs:
        parser.error('No matching repositories')
    for position,row in enumerate(jobs,1):
        repository,commit=row['repository'],row['commit']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or not re.fullmatch(r'[a-f0-9]{40}',commit):
            raise ValueError('Invalid repository or commit in workbook')
        directory=args.output.resolve()/repository.replace('/','__')/commit
        if directory.exists():
            head=git(directory,'rev-parse','HEAD').stdout.strip()
            status=git(directory,'status','--porcelain','--untracked-files=all').stdout.strip()
            if head!=commit or status:
                raise ValueError(f'Existing checkout differs or has edits: {directory}')
            print(f'{position}/{len(jobs)} already present: {repository}')
            continue
        directory.mkdir(parents=True)
        print(f'{position}/{len(jobs)} fetching {repository} {commit}',flush=True)
        git(directory,'init')
        git(directory,'remote','add','origin',f'https://github.com/{repository}.git')
        git(directory,'fetch','--depth=1','origin',commit)
        git(directory,'checkout','--detach',commit)
    print('Done. Target-project dependencies were not installed.')


if __name__=='__main__':
    main()
