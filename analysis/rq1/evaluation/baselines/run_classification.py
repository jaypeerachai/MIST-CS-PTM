"""Run one file-local baseline on the annotated PTM ID occurrences."""

import argparse
from collections import defaultdict
import os
from pathlib import Path

from common import EVALUATION, checkout, load_cases, select_cases, source_file, write_csv


def analyze(method, cases, repositories, codebook):
    if method == 'peatmoss':
        import peatmoss as adapter
        adapter.install_upstream_mnode()
        analyzer = adapter.load_extract_source_class()
        rules = adapter.group_rules_by_origin(adapter.load_peatmoss_rules(codebook))
    else:
        import tse as adapter
        upstream = adapter.load_upstream_module()
        rules = adapter.group_rules_by_origin(adapter.load_rules(codebook))
    if not rules:
        raise ValueError('The reuse codebook has no loader rules')

    grouped = defaultdict(list)
    for case in cases:
        grouped[case['repository'], case['commit'], case['file_path']].append(case)
    results = []
    for (repository, commit, relative), selected in grouped.items():
        repo = repositories[repository, commit]
        source_file(repo, relative)
        if method == 'peatmoss':
            matches, status = adapter.run_peatmoss_on_file(
                extract_source_cls=analyzer, repo_path=repo, rel_path=relative, rules_by_origin=rules)
        else:
            matches, status = adapter.run_tse_on_file(
                analyzer_cls=upstream.PTMStaticAnalyzer, repo_path=repo, rel_path=relative,
                rules_by_origin=rules, unwrap_quotes=upstream._unwrap_quotes,
                looks_like_local_path=upstream._looks_like_local_path,
                non_result_cache={str(v).lower() for v in upstream.NON_RESULT_CACHE})
        for case in selected:
            if method == 'peatmoss':
                relevant = [match for match in matches for value in adapter.extracted_model_arg_values(match)
                            if adapter.model_matches_arg(case['ptm_id'], value)]
                lines = [str(match.line_number) for match in relevant]
            else:
                relevant = [match for match in matches
                            if adapter.model_matches_param(case['ptm_id'], match.normalized_param_value)]
                lines = [str(match.call_line_number) for match in relevant]
            results.append(dict(case_id=case['case_id'], prediction='real_reuse' if relevant else 'non_reuse',
                                status=status, matched_call_lines='|'.join(sorted(set(lines), key=int))))
        print(f'{repository}: {relative} ({status})', flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--method', choices=['peatmoss', 'tse'], required=True)
    parser.add_argument('--dataset', choices=['benchmark', 'unseen_holdout', 'all'], default='all')
    parser.add_argument('--annotations', type=Path, help='workbook with an annotations sheet in the public format')
    parser.add_argument('--reuse-codebook', type=Path, default=EVALUATION.parent / 'codebooks/reuse_codebook.xlsx')
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--snapshots', type=Path, help='root containing owner__repo/commit checkouts')
    inputs.add_argument('--checkout', type=Path, help='one checkout, used with --repository')
    parser.add_argument('--repository', help='owner/repository')
    parser.add_argument('--case-id', action='append', help='run only this case; repeat to select more')
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    args = parser.parse_args()
    if args.checkout and not args.repository:
        parser.error('--checkout requires --repository')
    cases = select_cases(load_cases(args.dataset, args.annotations), args.repository, args.case_id)
    repositories = {(r, c): checkout(args.snapshots, r, c, args.checkout)
                    for r, c in sorted({(row['repository'], row['commit']) for row in cases})}
    output = args.output.resolve()
    codebook = args.reuse_codebook.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # The released PeaTMOSS module creates a log in its working directory.
    previous = Path.cwd()
    try:
        os.chdir(output)
        rows = analyze(args.method, cases, repositories, codebook)
        write_csv(output / f'{args.method}_predictions.csv', rows)
    finally:
        os.chdir(previous)
    print(f'Wrote {len(rows)} predictions to {output}')


if __name__ == '__main__':
    main()
