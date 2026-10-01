"""Plot PTM change types and their visibility in the existing method's counts."""

import argparse
import csv
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import to_rgb
from matplotlib.patches import Circle, Patch, Wedge

ORDER = ('add', 'remove', 'update', 'migration')
LABELS = {'add': 'Addition', 'remove': 'Removal', 'update': 'Update', 'migration': 'Migration'}
COLORS = {'add': '#B0D1A4', 'remove': '#DB7E74', 'update': '#9FB9E1', 'migration': '#B7A3D0'}
TEXT, SECONDARY, PALE = '#24313A', '#4B5963', '#E8E8E8'


def point(radius, degrees):
    angle = math.radians(degrees)
    return radius * math.cos(angle), radius * math.sin(angle)


def plot(rows, output):
    categories = {row['category']: row for row in rows}
    if len(categories) != len(rows) or set(categories) != set(ORDER):
        raise ValueError('Expected one row for each of the four change types')
    for row in rows:
        counts = [row[state] for state in ('fully_visible', 'partly_visible', 'not_visible')]
        if any(count < 0 for count in counts) or sum(counts) != row['changes']:
            raise ValueError('Visibility counts do not sum to the change count')
    total = sum(row['changes'] for row in rows)
    full = sum(row['fully_visible'] for row in rows)
    if total <= 0:
        raise ValueError('No changes to plot')
    plt.rcParams.update({'font.family': 'sans-serif',
        'font.sans-serif': ['Carlito', 'Calibri', 'DejaVu Sans'],
        'font.size': 8.5, 'text.color': TEXT, 'pdf.fonttype': 42,
        'ps.fonttype': 42, 'svg.fonttype': 'none', 'svg.hashsalt': 'ptm-event-visibility'})
    figure = plt.figure(figsize=(7.5, 4.2), facecolor='white')
    ax = figure.add_axes((0.01, 0.105, 0.98, 0.82))
    hole, inner = 0.50, 1.03
    # Equal ring areas keep the inner and outer proportions comparable.
    outer = math.sqrt(2 * inner**2 - hole**2)
    callouts = {'add': (1.47, 1.17, 'left'), 'remove': (-1.51, -0.72, 'right'),
                'update': (-1.49, 0.57, 'right'), 'migration': (-0.43, 1.75, 'center')}
    plurals = {'add': 'additions', 'remove': 'removals', 'update': 'updates', 'migration': 'migrations'}
    angle, anchors = 90.0, {}
    for key in ORDER:
        row = categories[key]
        if not row['changes']:
            continue
        end = angle - 360 * row['changes'] / total
        ax.add_patch(Wedge((0, 0), inner, end, angle, width=inner-hole,
                          facecolor=COLORS[key], edgecolor='white', linewidth=0.8))
        child_start = angle
        for visible, count in [(True, row['fully_visible'] + row['partly_visible']),
                               (False, row['not_visible'])]:
            if not count:
                continue
            child_end = child_start - 360 * count / total
            color = tuple(c * 0.56 for c in to_rgb(COLORS[key])) if visible else PALE
            ax.add_patch(Wedge((0, 0), outer, child_end, child_start, width=outer-inner,
                              facecolor=color, edgecolor='none'))
            if visible:
                anchors[key] = point(outer-0.02, (child_start+child_end)/2)
            child_start = child_end
        if not math.isclose(child_start, end, abs_tol=1e-9):
            raise ValueError('Visibility sectors do not match their change type')
        start, finish = point(hole, angle), point(outer, angle)
        ax.plot([start[0], finish[0]], [start[1], finish[1]], color='white', linewidth=0.7)
        middle = (angle + end) / 2
        x, y = point(0.775, middle)
        ax.text(x, y, f"{LABELS[key]}\n{row['changes']/total:.1%}", ha='center', va='center',
                fontsize=7.8 if key == 'migration' else 8.8 if key == 'update' else 10,
                rotation=middle % 360 if key == 'migration' else 0,
                rotation_mode='anchor', linespacing=1.05)
        anchors.setdefault(key, point(outer-0.02, middle))
        angle = end
    ax.add_patch(Circle((0, 0), inner, fill=False, edgecolor='white', linewidth=0.8))
    ax.text(0, 0.13, f'{total:,}\nMIST changes', ha='center', va='center',
            fontsize=11, fontweight='bold', linespacing=1.05)
    ax.text(0, -0.22, f'{full:,} fully visible\n({full/total:.1%})', ha='center', va='center',
            fontsize=8.7, linespacing=1.03, color=SECONDARY)
    for key in ORDER:
        row = categories[key]
        if not row['changes']:
            continue
        x, y, alignment = callouts[key]
        label = (f"{row['fully_visible']:,}/{row['changes']:,} fully visible\n"
                 f"{row['fully_visible']/row['changes']:.1%} of {plurals[key]}")
        if row['partly_visible']:
            label = (f"{row['fully_visible']:,}/{row['changes']:,} {plurals[key]} fully visible "
                     f"({row['fully_visible']/row['changes']:.1%})\n+ {row['partly_visible']:,} partly visible")
        ax.annotate(label, xy=anchors[key], xytext=(x, y), ha=alignment, va='center', fontsize=9,
                    linespacing=1.12, annotation_clip=False,
                    arrowprops={'arrowstyle': '-', 'color': SECONDARY, 'linewidth': 0.65,
                                'shrinkA': 2, 'shrinkB': 0})
    ax.set_aspect('equal')
    ax.set(xlim=(-2.3, 2.5), ylim=(-1.46, 1.98))
    ax.axis('off')
    figure.text(0.5, 0.975, 'Inner ring: MIST change types   |   Outer ring: visibility in existing counts',
                ha='center', va='top', fontsize=9.6)
    ax.legend(handles=[Patch(facecolor='#53636D', label='Visible'), Patch(facecolor=PALE, label='Not visible')],
              loc='lower left', bbox_to_anchor=(1.42, -1.28), bbox_transform=ax.transData,
              borderaxespad=0, frameon=False, fontsize=9.1, handlelength=1.4, labelspacing=0.4)
    for suffix in ('.pdf', '.svg'):
        metadata = {'CreationDate': None, 'ModDate': None} if suffix == '.pdf' else {'Date': None}
        figure.savefig(output.with_suffix(suffix), bbox_inches='tight', pad_inches=0.045, metadata=metadata)
    plt.close(figure)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--counts', type=Path, required=True, help='change_types.csv from analyze.py')
    parser.add_argument('--output', type=Path, required=True, help='Output path without an extension')
    args = parser.parse_args()
    with args.counts.open(encoding='utf-8', newline='') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for key in ('changes', 'fully_visible', 'partly_visible', 'not_visible'):
            row[key] = int(row[key])
    if any(args.output.with_suffix(ext).exists() for ext in ('.pdf', '.svg')):
        raise FileExistsError('Plot output already exists')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    plot(rows, args.output)
