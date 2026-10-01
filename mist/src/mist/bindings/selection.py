"""Choose which reachable sinks to validate for an occurrence."""

import networkx as nx

from mist.io import parse_int

SINK_MODES = ('shortest', 'confirmed-all', 'fallback', 'all')


def check_sinks(graph, source, primary_path, sinks, validate, mode):
    """Keep one representative path per sink, not every possible graph route."""
    if mode not in SINK_MODES:
        raise ValueError(f'Unknown sink mode: {mode}')
    if not primary_path:
        return []
    primary = next(sink for sink in sinks if sink.sink_node_id == primary_path[-1])
    first = (primary, primary_path, validate(primary))
    checks = [first]
    if mode == 'shortest' or mode == 'confirmed-all' and first[2].state != 'clear':
        return checks
    if mode == 'fallback' and first[2].state == 'clear':
        return checks
    ordered = sorted(sinks, key=lambda sink: (
        sink.loader_row.get('file_path', ''),
        parse_int(sink.loader_row.get('line_number')),
        sink.loader_row.get('visible_call_chain', ''),
        sink.loader_row.get('loader_candidate_id', ''),
    ))
    seen = {primary.sink_node_id}
    for sink in ordered:
        if sink.sink_node_id in seen:
            continue
        seen.add(sink.sink_node_id)
        try:
            path = nx.shortest_path(graph, source=source, target=sink.sink_node_id)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        checked = (sink, path, validate(sink))
        checks.append(checked)
        if mode == 'fallback' and checked[2].state == 'clear':
            break
    return checks


def selected_check(checks):
    """Prefer a confirmed route; otherwise retain uncertainty over a proven mock."""
    for state in ('clear', 'unresolved'):
        for checked in checks:
            if checked[2].state == state:
                return checked
    return checks[0]
