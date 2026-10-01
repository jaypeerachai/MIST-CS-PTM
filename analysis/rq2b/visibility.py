"""Match PTM changes to available increases and decreases in PTM-ID counts."""

from collections import deque
from dataclasses import dataclass


@dataclass
class Edge:
    target: int
    reverse: int
    capacity: int


def add_edge(graph, source, target, capacity):
    index = len(graph[source])
    graph[source].append(Edge(target, len(graph[target]), capacity))
    graph[target].append(Edge(source, index, 0))
    return index


def max_flow(graph, source, sink):
    # Find a maximum allocation, so an early match cannot block two later ones.
    while True:
        levels = [-1] * len(graph)
        levels[source] = 0
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for edge in graph[node]:
                if edge.capacity and levels[edge.target] < 0:
                    levels[edge.target] = levels[node] + 1
                    queue.append(edge.target)
        if levels[sink] < 0:
            return
        cursors = [0] * len(graph)

        def send(node, amount):
            if node == sink:
                return amount
            while cursors[node] < len(graph[node]):
                edge = graph[node][cursors[node]]
                if edge.capacity and levels[edge.target] == levels[node] + 1:
                    pushed = send(edge.target, min(amount, edge.capacity))
                    if pushed:
                        edge.capacity -= pushed
                        graph[edge.target][edge.reverse].capacity += pushed
                        return pushed
                cursors[node] += 1
            return 0

        while send(source, sum(edge.capacity for edge in graph[source])):
            pass


def full_replacements(events, increases, decreases):
    if not events:
        return set()
    events = sorted(events, key=lambda row: row['change_id'])
    old_ids = sorted({row['old_ptm_id'] for row in events})
    new_ids = sorted({row['new_ptm_id'] for row in events})
    source = 0
    event_start = 1 + len(old_ids)
    new_start = event_start + len(events)
    sink = new_start + len(new_ids)
    graph = [[] for _ in range(sink + 1)]
    old_nodes = {ptm: 1 + i for i, ptm in enumerate(old_ids)}
    new_nodes = {ptm: new_start + i for i, ptm in enumerate(new_ids)}
    for ptm, node in old_nodes.items():
        add_edge(graph, source, node, decreases[ptm])
    for ptm, node in new_nodes.items():
        add_edge(graph, node, sink, increases[ptm])
    event_edges = {}
    for i, event in enumerate(events):
        old_node = old_nodes[event['old_ptm_id']]
        edge = add_edge(graph, old_node, event_start + i, 1)
        add_edge(graph, event_start + i, new_nodes[event['new_ptm_id']], 1)
        event_edges[event['change_id']] = (old_node, edge)
    max_flow(graph, source, sink)
    selected = {key for key, (node, edge) in event_edges.items()
                if graph[node][edge].capacity == 0}
    for event in events:
        if event['change_id'] in selected:
            decreases[event['old_ptm_id']] -= 1
            increases[event['new_ptm_id']] -= 1
    return selected


def allocate(events, old_counts, new_counts):
    increases = new_counts - old_counts
    decreases = old_counts - new_counts
    events = sorted(events, key=lambda row: row['change_id'])
    replacements = [row for row in events if row['category'] in ('update', 'migration')]
    selected = full_replacements(replacements, increases, decreases)
    labels = {key: 'fully_visible' for key in selected}

    for row in replacements:
        key = row['change_id']
        if key in labels:
            continue
        if decreases[row['old_ptm_id']] > 0:
            decreases[row['old_ptm_id']] -= 1
            labels[key] = 'partly_visible'
        elif increases[row['new_ptm_id']] > 0:
            increases[row['new_ptm_id']] -= 1
            labels[key] = 'partly_visible'
        else:
            labels[key] = 'not_visible'

    for category, counts, field in [('add', increases, 'new_ptm_id'),
                                     ('remove', decreases, 'old_ptm_id')]:
        for row in events:
            if row['category'] != category:
                continue
            if counts[row[field]] > 0:
                counts[row[field]] -= 1
                labels[row['change_id']] = 'fully_visible'
            else:
                labels[row['change_id']] = 'not_visible'
    return labels
