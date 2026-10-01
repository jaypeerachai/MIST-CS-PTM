"""Match PTM bindings across releases and apply reviewed change decisions."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from common import read_csv, read_jsonl, structural_fingerprint, write_csv
from continuity import DiffHunk, match_same_id_bindings
from replacements import pair_different_id_bindings
from transitions import build_transitions, read_components, write_transitions

HERE = Path(__file__).resolve().parent


def read_inputs(database, inputs):
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        pairs = [dict(row) for row in db.execute('SELECT * FROM release_pairs ORDER BY pair_id')]
        bindings = {row['binding_id']: dict(row) for row in db.execute('''
            SELECT b.binding_id,a.release_id,o.ptm_id,o.path,o.line,b.sink_path,b.sink_line
            FROM bindings b JOIN analyses a USING(analysis_id)
            JOIN occurrences o USING(occurrence_id) WHERE a.analysis_type='release'
        ''')}
    structure = read_jsonl(inputs / 'binding_structure.jsonl.gz')
    inventory = {row['confirmed_binding_key']: row for row in structure}
    if len(inventory) != len(structure) or set(inventory) != set(bindings):
        raise ValueError('Binding structure must cover exactly the historical database bindings')
    by_release = defaultdict(list)
    for key, row in inventory.items():
        recorded = bindings[key]
        actual = (row['release_row_id'], row['canonical_model_id'],
                  row['source_resolved_file_path'], row['source_line_number'],
                  row['loader_file_path'], row['loader_line'])
        expected = tuple(recorded[field] for field in
                         ('release_id', 'ptm_id', 'path', 'line', 'sink_path', 'sink_line'))
        if actual != expected:
            raise ValueError('Binding structure differs from database locations: ' + key)
        by_release[row['release_row_id']].append(row)
    hunk_rows = read_jsonl(inputs / 'diff_hunks.jsonl.gz')
    hunks = {row['pair_id']: tuple(DiffHunk(**hunk) for hunk in row['hunks']) for row in hunk_rows}
    pair_ids = {row['pair_id'] for row in pairs}
    if len(hunks) != len(hunk_rows) or set(hunks) != pair_ids:
        raise ValueError('Git hunk input must cover exactly the release pairs')
    reviewed = read_csv(inputs / 'reviewed_changes.csv')
    if len({row['change_id'] for row in reviewed}) != len(reviewed):
        raise ValueError('Duplicate reviewed change ID')
    decisions = defaultdict(list)
    for row in reviewed:
        if row['pair_id'] not in pair_ids:
            raise ValueError('Reviewed change refers to an unknown release pair')
        decisions[row['pair_id']].append(row)
    transitions = build_transitions(read_components(inputs / 'ptm_id_components.xlsx'))
    return pairs, inventory, by_release, hunks, decisions, transitions


def make_event(pair_id, category, old, new, inventory, change_id=None):
    old, new = sorted(old), sorted(new)
    old_models = {inventory[key]['canonical_model_id'] for key in old}
    new_models = {inventory[key]['canonical_model_id'] for key in new}
    if category not in ('add', 'remove', 'update', 'migration'):
        raise ValueError('Unknown change type')
    if (bool(old) != (category != 'add') or bool(new) != (category != 'remove')
            or len(old_models) > 1 or len(new_models) > 1):
        raise ValueError('Change endpoints do not match its type')
    old_ptm, new_ptm = next(iter(old_models), ''), next(iter(new_models), '')
    if category in ('update', 'migration') and old_ptm == new_ptm:
        raise ValueError('A replacement requires different PTM IDs')
    if change_id is None:
        change_id = structural_fingerprint(dict(pair_id=pair_id, category=category, old=old, new=new))
    return dict(change_id=change_id, pair_id=pair_id, category=category,
                old_ptm_id=old_ptm, new_ptm_id=new_ptm,
                old_binding_ids=';'.join(old), new_binding_ids=';'.join(new))


def detect_pair(pair, before, after, inventory, hunks, decisions, transitions):
    pair_id = pair['pair_id']
    context = dict(old_release_row_id=pair['old_release_id'], new_release_row_id=pair['new_release_id'])
    continuity = match_same_id_bindings(context, before, after, diff_hunks=hunks)
    matches = []
    residuals = [{**inventory[row['confirmed_binding_key']], **row} for row in continuity.residual_bindings]
    reviewed_endpoints = {(side, key) for decision in decisions for side in ('old', 'new')
                          for key in filter(None, decision[side + '_binding_ids'].split(';'))}
    freed_unreviewed = set()
    # Reviewed changes take precedence over automatic continuity matches.
    for match in continuity.matches:
        endpoints = {(side, match[side + '_confirmed_binding_key']) for side in ('old', 'new')}
        if not endpoints & reviewed_endpoints:
            matches.append(match)
            continue
        freed_unreviewed.update(endpoints - reviewed_endpoints)
        for side, key in sorted(endpoints):
            residuals.append({**inventory[key], 'side': side, 'site_turnover_group_id': ''})
    old_residuals = [row for row in residuals if row['side'] == 'old']
    new_residuals = [row for row in residuals if row['side'] == 'new']
    replacement = pair_different_id_bindings(context, old_residuals, new_residuals, diff_hunks=hunks)
    candidates = []
    for row in replacement.pairs:
        key = row['old_model_id'], row['new_model_id']
        if key not in transitions:
            raise ValueError('PTM transition is missing from the codebook: ' + str(key))
        candidates.append(dict(pair_id=pair_id, old_binding_id=row['old_confirmed_binding_key'],
            new_binding_id=row['new_confirmed_binding_key'], category=transitions[key],
            relation=row['relation'], reviewed=0))
    available = {(row['side'], row['confirmed_binding_key']) for row in residuals}
    consumed = set()
    events = []
    for decision in decisions:
        old = decision['old_binding_ids'].split(';') if decision['old_binding_ids'] else []
        new = decision['new_binding_ids'].split(';') if decision['new_binding_ids'] else []
        endpoints = {('old', key) for key in old} | {('new', key) for key in new}
        if len(endpoints) != len(old) + len(new) or not endpoints <= available or endpoints & consumed:
            raise ValueError('Reviewed change has unavailable or repeated endpoints: ' + decision['change_id'])
        event = make_event(pair_id, decision['category'], old, new, inventory, decision['change_id'])
        if event['category'] in ('update', 'migration'):
            if transitions.get((event['old_ptm_id'], event['new_ptm_id'])) != event['category']:
                raise ValueError('Reviewed replacement disagrees with the transition codebook')
        events.append(event)
        consumed.update(endpoints)
    for row in candidates:
        endpoints = {('old', row['old_binding_id']), ('new', row['new_binding_id'])}
        row['reviewed'] = int(endpoints <= consumed)
    for row in replacement.residual_bindings:
        key, side = row['confirmed_binding_key'], row['side']
        if ((side, key) in consumed or (side, key) in freed_unreviewed
                or not row['eligible_for_definite_add_remove']):
            continue
        events.append(make_event(pair_id, 'remove' if side == 'old' else 'add',
                                 [key] if side == 'old' else [], [key] if side == 'new' else [], inventory))
        consumed.add((side, key))
    unmatched = [dict(pair_id=pair_id, side=side, binding_id=key)
                 for side, key in sorted(available - consumed)]
    continuing = [dict(match_id=row['continuity_key'], pair_id=pair_id,
        old_binding_id=row['old_confirmed_binding_key'], new_binding_id=row['new_confirmed_binding_key'])
        for row in matches]
    assigned = [(side, row[side + '_binding_id']) for row in continuing for side in ('old', 'new')]
    assigned.extend(consumed)
    assigned.extend((row['side'], row['binding_id']) for row in unmatched)
    expected = {('old', row['confirmed_binding_key']) for row in before}
    expected |= {('new', row['confirmed_binding_key']) for row in after}
    if len(assigned) != len(set(assigned)) or set(assigned) != expected:
        raise ValueError('Release pair does not account for every binding exactly once: ' + pair_id)
    return events, continuing, candidates, unmatched


def event_signature(row):
    return (row['pair_id'], row['category'], tuple(sorted(filter(None, row['old_binding_ids'].split(';')))),
            tuple(sorted(filter(None, row['new_binding_ids'].split(';')))))


def verify(database, events, matches, unmatched):
    # Verification is separate from detection. Existing IDs are restored only after exact agreement.
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        endpoints = defaultdict(lambda: defaultdict(list))
        for row in db.execute('SELECT * FROM change_bindings'):
            endpoints[row['change_id']][row['side']].append(row['binding_id'])
        expected = {}
        for row in db.execute('SELECT * FROM ptm_changes'):
            row = dict(row)
            for side in ('old', 'new'):
                row[side + '_binding_ids'] = ';'.join(endpoints[row['change_id']][side])
            signature = event_signature(row)
            if signature in expected:
                raise ValueError('Duplicate stored change endpoints')
            expected[signature] = row
        expected_matches = {(r['pair_id'], r['old_binding_id'], r['new_binding_id']): r['match_id']
                            for r in db.execute('SELECT * FROM binding_matches')}
        expected_unmatched = {tuple(row) for row in db.execute('SELECT pair_id,side,binding_id FROM unmatched_bindings')}
    actual = {event_signature(row): row for row in events}
    if len(actual) != len(events) or set(actual) != set(expected):
        missing, extra = set(expected) - set(actual), set(actual) - set(expected)
        raise ValueError(f'Change mismatch: {len(missing)} missing, {len(extra)} extra. '
                         f'Examples: {sorted(missing)[:2]} / {sorted(extra)[:2]}')
    actual_matches = {(r['pair_id'], r['old_binding_id'], r['new_binding_id']) for r in matches}
    if len(actual_matches) != len(matches) or actual_matches != set(expected_matches):
        raise ValueError(f'Continuity mismatch: {len(set(expected_matches) - actual_matches)} missing, '
                         f'{len(actual_matches - set(expected_matches))} extra')
    actual_unmatched = {(r['pair_id'], r['side'], r['binding_id']) for r in unmatched}
    if actual_unmatched != expected_unmatched:
        raise ValueError('Unmatched endpoints differ from the database')
    for signature, row in actual.items():
        saved = expected[signature]
        if (row['old_ptm_id'] or None, row['new_ptm_id'] or None) != (saved['old_ptm_id'], saved['new_ptm_id']):
            raise ValueError('PTM IDs differ despite matching endpoints')
        row['change_id'] = saved['change_id']
    for row in matches:
        row['match_id'] = expected_matches[(row['pair_id'], row['old_binding_id'], row['new_binding_id'])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, default=HERE.parent / 'inputs')
    parser.add_argument('--output', type=Path, required=True, help='New or empty directory')
    parser.add_argument('--verify', action='store_true', help='Compare exact results with the database and retain its IDs')
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError('Output directory must be new or empty')
    pairs, inventory, by_release, hunks, decisions, transitions = read_inputs(args.database, args.inputs)
    events, matches, candidates, unmatched = [], [], [], []
    for pair in pairs:
        results = detect_pair(pair, by_release[pair['old_release_id']], by_release[pair['new_release_id']],
                              inventory, hunks[pair['pair_id']], decisions[pair['pair_id']], transitions)
        for rows, result in zip((events, matches, candidates, unmatched), results):
            rows.extend(result)
    if args.verify:
        verify(args.database, events, matches, unmatched)
    endpoints = [dict(change_id=row['change_id'], side=side, binding_id=key)
                 for row in events for side in ('old', 'new')
                 for key in filter(None, row[side + '_binding_ids'].split(';'))]
    summary = dict(release_pairs=len(pairs), historical_bindings=len(inventory), continuing_binding_pairs=len(matches),
                   changes=len(events), change_types=dict(sorted(Counter(row['category'] for row in events).items())),
                   replacement_candidates=len(candidates),
                   pending_replacement_candidates=sum(not row['reviewed'] for row in candidates),
                   unmatched_binding_endpoints=len(unmatched), verified=args.verify)
    args.output.mkdir(parents=True, exist_ok=True)
    tables = {
        'ptm_changes': (events, ['change_id','pair_id','category','old_ptm_id','new_ptm_id','old_binding_ids','new_binding_ids']),
        'binding_matches': (matches, ['match_id','pair_id','old_binding_id','new_binding_id']),
        'change_bindings': (endpoints, ['change_id','side','binding_id']),
        'replacement_candidates': (candidates, ['pair_id','old_binding_id','new_binding_id','category','relation','reviewed']),
        'unmatched_bindings': (unmatched, ['pair_id','side','binding_id']),
    }
    for name, (rows, fields) in tables.items():
        write_csv(args.output / (name + '.csv'), sorted(rows, key=lambda row: tuple(str(row[field]) for field in fields)), fields)
    write_transitions(args.output / 'model_transition.csv', transitions)
    with (args.output / 'summary.json').open('x', encoding='utf-8') as file:
        json.dump(summary, file, indent=2)
        file.write('\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
