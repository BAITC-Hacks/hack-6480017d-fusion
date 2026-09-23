"""Directed graph features, preserving every int64 identifier and isolated node."""
from __future__ import annotations

import networkx as nx
import pandas as pd

from backend.analysis_config import (MAX_OBSERVED_DEPTH, PAGERANK_ALPHA,
    PAGERANK_MAX_ITER, PAGERANK_TOL, SEED_REACH_HOPS)


def build_graph(nodes: pd.DataFrame, edges: pd.DataFrame) -> nx.DiGraph:
    graph = nx.DiGraph()
    for row in nodes.sort_values('gid').itertuples(index=False):
        graph.add_node(int(row.gid), depth=int(row.depth), is_seed=bool(row.is_seed))
    for row in edges.sort_values(['src', 'dst']).itertuples(index=False):
        src, dst = int(row.src), int(row.dst)
        if src not in graph or dst not in graph:
            raise ValueError('Конец ребра отсутствует в nodes')
        if graph.has_edge(src, dst):
            raise ValueError('Пары edges должны быть уникальными')
        graph.add_edge(src, dst, sum_kzt=float(row.sum_kzt), n_tx=int(row.n_tx))
    return graph


def compute_features(graph: nx.DiGraph) -> pd.DataFrame:
    if not graph:
        raise ValueError('Граф узлов пуст')
    pr = nx.pagerank(graph, alpha=PAGERANK_ALPHA, weight='sum_kzt',
                     tol=PAGERANK_TOL, max_iter=PAGERANK_MAX_ITER)
    seed_ids = {gid for gid, attrs in graph.nodes(data=True) if attrs['is_seed']}
    seed_reach = dict.fromkeys(graph, 0)
    for seed in sorted(seed_ids):
        for gid in nx.single_source_shortest_path_length(graph, seed, cutoff=SEED_REACH_HOPS):
            if gid != seed:
                seed_reach[gid] += 1
    rows = []
    for gid, attrs in graph.nodes(data=True):
        # Counterparty counts exclude self-transfers. Weighted volumes include them.
        predecessors = set(graph.predecessors(gid)) - {gid}
        successors = set(graph.successors(gid)) - {gid}
        in_kzt = float(graph.in_degree(gid, weight='sum_kzt'))
        out_kzt = float(graph.out_degree(gid, weight='sum_kzt'))
        rows.append({
            'gid': gid, 'depth': attrs['depth'], 'is_seed': attrs['is_seed'],
            'in_deg': len(predecessors), 'out_deg': len(successors),
            'counterparties': len(predecessors | successors),
            'in_kzt': in_kzt, 'out_kzt': out_kzt,
            'in_tx': int(graph.in_degree(gid, weight='n_tx')),
            'out_tx': int(graph.out_degree(gid, weight='n_tx')),
            'pagerank': float(pr[gid]),
            'pass_through': out_kzt / in_kzt if in_kzt > 0 else None,
            'ratio_usable': not attrs['is_seed'] and in_kzt > 0 and not graph.has_edge(gid, gid),
            'seed_reach': seed_reach[gid],
            'direct_seed_senders': len(predecessors & seed_ids),
            'is_isolated': graph.degree(gid) == 0,
            'has_self_loop': graph.has_edge(gid, gid),
            'truncated_by_depth': attrs['depth'] == MAX_OBSERVED_DEPTH and graph.out_degree(gid) == 0,
        })
    frame = pd.DataFrame(rows)
    frame['gid'] = frame['gid'].astype('int64')
    return frame


def undirected_projection(graph: nx.DiGraph) -> nx.Graph:
    """Sum reciprocal amounts instead of letting to_undirected overwrite a weight."""
    projected = nx.Graph()
    projected.add_nodes_from(sorted(graph))
    for src, dst, attrs in sorted(graph.edges(data=True)):
        if src == dst:  # Self-transfers do not connect distinct participants.
            continue
        previous = projected.get_edge_data(src, dst, {}).get('weight', 0.0)
        projected.add_edge(src, dst, weight=previous + attrs['sum_kzt'])
    return projected
