"""Locate exact quoted PTM IDs in downloaded candidate files."""

import ast
import gzip
import io
import tokenize
from bisect import bisect_right
from collections import defaultdict

from reproduce import read_csv

FIELDS = ['occurrence_id', 'repository', 'path', 'blob_sha', 'model_id',
          'search_string', 'search_string_type', 'quote_style', 'line_no',
          'column_start', 'column_end', 'lexical_context', 'ast_context', 'line_text']


def find_occurrences(text, specs):
    """Return character locations, including quotes, and their code context."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in {tokenize.COMMENT, tokenize.STRING}:
                kind = 'comment' if token.type == tokenize.COMMENT else 'string_literal'
                ranges.append((kind, token.start, token.end))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    docs = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            body = getattr(node, 'body', None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                # AST columns use bytes. Export character columns, like tokenize.
                start = len(lines[first.lineno - 1].encode('utf-8')[:first.col_offset].decode('utf-8'))
                end = len(lines[first.end_lineno - 1].encode('utf-8')[:first.end_col_offset].decode('utf-8'))
                docs.append(((first.lineno, start), (first.end_lineno, end)))
    for seed, style in sorted(set(specs)):
        mark = '"' if style == 'double' else "'"
        needle = mark + seed + mark
        position = text.find(needle)
        while position >= 0:
            line_index = bisect_right(offsets, position) - 1
            end_index = min(bisect_right(offsets, position + len(needle)) - 1, len(lines) - 1)
            point = (line_index + 1, position - offsets[line_index])
            kind, context = 'code_or_unknown', 'not_token_string_or_comment'
            if any(start <= point < end for start, end in docs):
                kind, context = 'docstring', 'ast_docstring'
            else:
                for token_kind, start, end in ranges:
                    if start <= point < end:
                        kind, context = token_kind, 'tokenized'
                        break
            yield {'search_string': seed, 'quote_style': style, 'line_no': point[0],
                   'column_start': point[1], 'column_end': position + len(needle) - offsets[end_index],
                   'lexical_context': kind, 'ast_context': context,
                   'line_text': lines[line_index].rstrip('\r\n')}
            position = text.find(needle, position + 1)


def collect_occurrences(inputs):
    """Export locations from saved downloads without making more API requests."""
    targets = {}
    for row in read_csv(inputs / 'literal_checks.csv.gz'):
        if row['occurrence_extraction_status'] == 'extracted':
            key = row['github_repo_id'], row['file_path'], row['file_sha'], row['model_id']
            targets[key] = int(row['occurrence_count'])
    queries = {row['query_row_id']: row for row in read_csv(inputs / 'query_splits.csv.gz')}
    files = defaultdict(lambda: defaultdict(set))
    for row in read_csv(inputs / 'search_matches.csv.gz'):
        query = queries[row['query_row_id']]
        key = row['github_repo_id'], row['file_path'], row['file_sha'], query['model_id']
        if key in targets:
            file_key = row['github_repo_id'], row['repository_full_name'], row['file_path'], row['file_sha']
            files[file_key][query['model_id']].add((query['search_seed'], query['quote_style']))
    index = 0
    for (repo_id, repo, path, blob), models in sorted(files.items()):
        content = gzip.decompress((inputs.parent / 'source_files' / repo / f'{blob}.py.gz').read_bytes())
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            text = content.decode('latin-1')
        specs = {spec for model_specs in models.values() for spec in model_specs}
        locations = list(find_occurrences(text, specs))
        for model, model_specs in sorted(models.items()):
            matches = [row for row in locations if (row['search_string'], row['quote_style']) in model_specs]
            if len(matches) != targets[repo_id, path, blob, model]:
                raise ValueError(f'Occurrence count differs from downloaded file: {repo}/{path} ({model})')
            for row in matches:
                index += 1
                yield dict(row, occurrence_id=f'occurrence_{index:08d}', repository=repo,
                           path=path, blob_sha=blob, model_id=model,
                           search_string_type='canonical_model_id' if row['search_string'] == model
                           else 'namespace_free_model_id')
