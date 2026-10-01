"""Summarize PTM changes and their visibility in the existing method's counts."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import sqlite3

from visibility import allocate

HERE = Path(__file__).resolve().parent
CATEGORIES = ('add', 'remove', 'update', 'migration')
STATES = ('fully_visible', 'partly_visible', 'not_visible')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_database(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        releases = {r['release_id']: dict(r) for r in connection.execute('''
            SELECT r.release_id,r.tag,r.release_line,s.repository_id,p.name AS repository
            FROM releases r JOIN snapshots s USING(snapshot_id)
            JOIN repositories p USING(repository_id)
        ''')}
        pairs = [dict(r) for r in connection.execute('SELECT * FROM release_pairs ORDER BY pair_id')]
        changes = [dict(r) for r in connection.execute('SELECT * FROM ptm_changes ORDER BY change_id')]
    if not releases or not pairs:
        raise ValueError('The database has no release history')
    for pair in pairs:
        old, new = releases[pair['old_release_id']], releases[pair['new_release_id']]
        if (old['repository_id'], old['release_line']) != (new['repository_id'], new['release_line']):
            raise ValueError('Release pair crosses repositories or release lines: ' + pair['pair_id'])
    return releases, pairs, changes


def read_counts(path, releases):
    counts = defaultdict(Counter)
    with path.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['release_id', 'ptm_id', 'count']:
            raise ValueError('Expected release_id,ptm_id,count columns')
        for row in reader:
            release, ptm = row['release_id'], row['ptm_id']
            if release not in releases:
                raise ValueError('Count refers to a release outside this database. Use the full population database: ' + release)
            count = int(row['count'])
            if not ptm or count <= 0 or ptm in counts[release]:
                raise ValueError('Empty PTM ID, nonpositive count, or duplicate count row')
            counts[release][ptm] = count
    # The supplied count inputs cover every study release, including zero results.
    return {release: counts[release] for release in releases}


def share(count, total):
    return count / total if total else 0.0


def calculate(releases, pairs, changes, counts):
    pair_ids = {row['pair_id'] for row in pairs}
    if len(pair_ids) != len(pairs) or len({r['change_id'] for r in changes}) != len(changes):
        raise ValueError('Duplicate pair or change IDs')
    if set(counts) != set(releases):
        raise ValueError('Counts must cover every release')
    grouped = defaultdict(list)
    for row in changes:
        category = row['category']
        if category not in CATEGORIES or row['pair_id'] not in pair_ids:
            raise ValueError('Unknown change category or release pair')
        if (bool(row['old_ptm_id']) != (category != 'add') or
                bool(row['new_ptm_id']) != (category != 'remove')):
            raise ValueError('Change endpoints do not match its category')
        if category in ('update', 'migration') and row['old_ptm_id'] == row['new_ptm_id']:
            raise ValueError('A replacement must have different PTM IDs')
        grouped[row['pair_id']].append(row)

    events, pair_rows = [], []
    for pair in sorted(pairs, key=lambda row: row['pair_id']):
        key = pair['pair_id']
        old_id, new_id = pair['old_release_id'], pair['new_release_id']
        old, new = releases[old_id], releases[new_id]
        labels = allocate(grouped[key], counts[old_id], counts[new_id])
        for row in sorted(grouped[key], key=lambda row: row['change_id']):
            label = labels[row['change_id']]
            saved = row.get('existing_method_visibility')
            if saved is not None and saved != label:
                raise ValueError('Calculated visibility differs from the database: ' + row['change_id'])
            events.append(dict(change_id=row['change_id'], pair_id=key, repository=old['repository'],
                category=row['category'], old_ptm_id=row['old_ptm_id'], new_ptm_id=row['new_ptm_id'],
                existing_method_visibility=label))
        pair_rows.append(dict(pair_id=key, repository=old['repository'], old_tag=old['tag'],
            new_tag=new['tag'], mist_changes=len(grouped[key]),
            existing_method_changed=int(counts[old_id] != counts[new_id])))

    total = len(events)
    states = Counter(row['existing_method_visibility'] for row in events)
    categories = []
    for category in CATEGORIES:
        selected = [row for row in events if row['category'] == category]
        visible = Counter(row['existing_method_visibility'] for row in selected)
        categories.append(dict(category=category, changes=len(selected),
            change_share=share(len(selected), total),
            **{state: visible[state] for state in STATES},
            **{state + '_share': share(visible[state], len(selected)) for state in STATES},
            share_among_fully_visible=share(visible['fully_visible'], states['fully_visible'])))
    mist_pairs = {row['pair_id'] for row in pair_rows if row['mist_changes']}
    existing_pairs = {row['pair_id'] for row in pair_rows if row['existing_method_changed']}
    replacements = [row for row in events if row['category'] in ('update', 'migration')]
    replacement_states = Counter(row['existing_method_visibility'] for row in replacements)
    summary = dict(
        scope=dict(repositories=len({r['repository_id'] for r in releases.values()}),
            releases=len(releases),
            release_lines=len({(r['repository_id'], r['release_line']) for r in releases.values()}),
            release_pairs=len(pairs)),
        changes=total,
        visibility={state: dict(changes=states[state], share=share(states[state], total)) for state in STATES},
        replacements=dict(changes=len(replacements),
            **{state: replacement_states[state] for state in STATES},
            fully_visible_share=share(replacement_states['fully_visible'], len(replacements))),
        release_pairs=dict(total=len(pairs), mist_changed=len(mist_pairs),
            existing_changed=len(existing_pairs), overlap=len(mist_pairs & existing_pairs),
            mist_only=len(mist_pairs - existing_pairs), existing_only=len(existing_pairs - mist_pairs),
            with_fully_visible_change=len({row['pair_id'] for row in events
                                          if row['existing_method_visibility'] == 'fully_visible'})))
    return dict(change_types=categories, change_visibility=events, release_pairs=pair_rows), summary


def write_csv(path, rows, fields=None):
    with path.open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True, help='Full validation-population database')
    parser.add_argument('--counts', type=Path, default=HERE / 'inputs/existing_method_counts.csv')
    parser.add_argument('--changes', type=Path, help='Regenerated ptm_changes.csv; otherwise use database changes')
    parser.add_argument('--output', type=Path, required=True, help='New or empty output directory')
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError('Output directory must be new or empty')
    hashes = dict(database_sha256=sha256(args.database), existing_method_counts_sha256=sha256(args.counts))
    releases, pairs, changes = read_database(args.database)
    if args.changes:
        hashes['changes_sha256'] = sha256(args.changes)
        with args.changes.open(encoding='utf-8', newline='') as stream:
            reader = csv.DictReader(stream)
            required = {'change_id', 'pair_id', 'category', 'old_ptm_id', 'new_ptm_id'}
            if not required <= set(reader.fieldnames or []):
                raise ValueError('Change CSV is missing required columns')
            changes = list(reader)
    tables, summary = calculate(releases, pairs, changes, read_counts(args.counts, releases))
    if not summary['changes']:
        raise ValueError('No PTM changes to summarize')
    after = dict(database_sha256=sha256(args.database), existing_method_counts_sha256=sha256(args.counts))
    if args.changes:
        after['changes_sha256'] = sha256(args.changes)
    if hashes != after:
        raise ValueError('Inputs changed during analysis')
    args.output.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        write_csv(args.output / (name + '.csv'), rows)
    with (args.output / 'summary.json').open('x', encoding='utf-8') as stream:
        json.dump(summary, stream, indent=2)
        stream.write('\n')
    from plot import plot
    plot(tables['change_types'], args.output / 'event_visibility')
    print(f"Wrote {summary['changes']:,} PTM changes across {summary['release_pairs']['mist_changed']:,} release pairs.")


if __name__ == '__main__':
    main()
