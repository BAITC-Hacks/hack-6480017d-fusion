"""Validated, in-memory analysis snapshot shared by graph, cards and CSV downloads.

The snapshot is rebuilt on application startup from parquet, never from stale
artifacts. Restart the application after replacing its input data.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from backend.analysis_config import PRIORITY_WEIGHTS, ROLES
from backend.audit import _sha256
from backend.pipeline import PRIORITY_NAMES, calculate
from backend.serialization import to_json_safe

EXPORT_FILENAMES = frozenset({'nodes_roles.csv', 'clusters.csv', 'top_nodes.csv'})
GRAPH_NODE_COLUMNS = (
    'gid', 'role', 'priority_score', 'cluster_id', 'is_seed', 'depth',
    'truncated_by_depth', 'is_isolated',
)


def frame_records(frame: pd.DataFrame) -> list[dict]:
    """Preserve int64 IDs; only undefined numeric metrics become JSON null."""
    records = []
    for values in frame.itertuples(index=False, name=None):
        record = dict(zip(frame.columns, values))
        for key, value in record.items():
            if isinstance(value, float) and not math.isfinite(value):
                record[key] = None
        records.append(to_json_safe(record))
    return records


def caveats_for(node: dict) -> list[str]:
    caveats = []
    if node['truncated_by_depth']:
        caveats.append('Граница выгрузки на глубине 4: отсутствие исходящих не доказывает удержание денег.')
    if node['is_seed']:
        caveats.append('Входящий поток seed неполон; out/in нельзя считать полным балансом или доказательством транзита.')
    if node['is_isolated']:
        caveats.append('В выгрузке нет связей этого узла; данных для специальной гипотезы о роли недостаточно.')
    if node['has_self_loop']:
        caveats.append('Есть перевод на тот же узел; он входит в суммы, но не добавляет нового контрагента.')
    caveats.extend([
        'Известны только переводы внутри банка от 5 000 KZT за период выгрузки; потоки вне выборки не видны.',
        'Роль — гипотеза для проверки. Скор роли отражает поддержку правила, а не вероятность виновности.',
    ])
    return caveats


def priority_why(node: dict) -> str:
    """Use the same contribution names/order as CSV, separate from role evidence."""
    contributions = sorted(PRIORITY_WEIGHTS, key=lambda name: (-node['priority_' + name], name))[:2]
    parts = ', '.join(f'{PRIORITY_NAMES[name]}={node["priority_" + name]:.3f}' for name in contributions)
    return (f"Приоритет {node['priority_score']:.3f}: основные слагаемые {parts}; "
            f"множитель полноты {node['priority_factor']:.1f}.")


class AnalysisService:
    def __init__(self, data_dir: Path, audit: dict):
        nodes = pd.read_parquet(data_dir / 'nodes.parquet')
        edges = pd.read_parquet(data_dir / 'edges.parquet')
        roles, clusters, top, metadata = calculate(nodes, edges)
        # Do not combine a previous audit with newly replaced source files.
        for name, digest in audit['hashes'].items():
            if _sha256(data_dir / f'{name}.parquet') != digest:
                raise ValueError('Исходные данные изменились во время расчёта API')

        self.input_hashes = dict(audit['hashes'])

        self.nodes = {row['gid']: row for row in frame_records(roles)}
        self.ranked_ids = sorted(self.nodes, key=lambda gid: (-self.nodes[gid]['priority_score'], int(gid)))
        self.priority_ranks = {gid: rank for rank, gid in enumerate(self.ranked_ids, start=1)}
        self.edges = frame_records(edges.sort_values(['src', 'dst'])[['src', 'dst', 'sum_kzt', 'n_tx']])
        self.cluster_ids: dict[int, set[str]] = {}
        self.neighbors = {gid: set() for gid in self.nodes}
        self.incoming_edges: dict[str, list[dict]] = {gid: [] for gid in self.nodes}
        self.outgoing_edges: dict[str, list[dict]] = {gid: [] for gid in self.nodes}
        for gid, row in self.nodes.items():
            self.cluster_ids.setdefault(row['cluster_id'], set()).add(gid)
        for edge in self.edges:
            self.neighbors[edge['src']].add(edge['dst'])
            self.neighbors[edge['dst']].add(edge['src'])
            self.incoming_edges[edge['dst']].append(edge)
            self.outgoing_edges[edge['src']].append(edge)
        # Full directed flows are independent of the graph's display limit.
        # A self-transfer correctly contributes to both incoming and outgoing.
        for index in (self.incoming_edges, self.outgoing_edges):
            for flows in index.values():
                flows.sort(key=lambda edge: (-edge['sum_kzt'], int(edge['src']), int(edge['dst'])))

        cluster_records = frame_records(clusters)
        for row in cluster_records:
            row['top_gids'] = json.loads(row['top_gids'])
        source_summary = audit['summary']
        self.summary = {
            'nodes': len(nodes), 'edges': len(edges),
            'transactions': source_summary['transaction_count'],
            'seed_nodes': source_summary['seed_count'], 'period': source_summary['period'],
            'clusters_count': len(clusters), 'total_kzt': audit['statistics']['transaction_sum_kzt'],
            'role_counts': {role: metadata['role_counts'].get(role, 0) for role in ROLES},
            'top_nodes': frame_records(top), 'clusters': cluster_records,
        }
        # Same format and complete rows as the command-line pipeline, independent
        # of graph filters. Serving a download never writes into artifacts/.
        self.exports = {
            filename: frame.to_csv(index=False, lineterminator='\n', float_format='%.12g').encode('utf-8')
            for filename, frame in (
                ('nodes_roles.csv', roles), ('clusters.csv', clusters), ('top_nodes.csv', top),
            )
        }

    def node(self, gid: str) -> dict:
        row = self.nodes[gid]
        return {
            **row, 'caveats': caveats_for(row),
            'priority_rank': self.priority_ranks[gid],
            'priority_total': len(self.ranked_ids),
            'priority_base': math.fsum(row['priority_' + name] for name in PRIORITY_WEIGHTS),
            'priority_why': priority_why(row),
            'incoming_edges': [dict(edge) for edge in self.incoming_edges[gid]],
            'outgoing_edges': [dict(edge) for edge in self.outgoing_edges[gid]],
        }

    def graph(self, cluster_id: int | None = None, gid: str | None = None, limit: int = 250) -> dict:
        if gid is not None:
            selected_scope = self.neighbors[gid] | {gid}
            scope = f'Узел {gid} и его непосредственные входящие и исходящие соседи'
        else:
            if cluster_id is None:
                cluster_id = min(self.cluster_ids, key=lambda key: (-len(self.cluster_ids[key]), key))
            selected_scope = self.cluster_ids[cluster_id]
            scope = f'Кластер {cluster_id}: связи между его участниками'
        ranked = [node_id for node_id in self.ranked_ids if node_id in selected_scope and node_id != gid]
        selected_ids = ([gid] if gid is not None else []) + ranked[:limit - (gid is not None)]
        selected_set = set(selected_ids)
        scope_edges = [edge for edge in self.edges if edge['src'] in selected_scope and edge['dst'] in selected_scope]
        visible_edges = [edge for edge in scope_edges if edge['src'] in selected_set and edge['dst'] in selected_set]
        return {
            'nodes': [{key: self.nodes[node_id][key] for key in GRAPH_NODE_COLUMNS} for node_id in selected_ids],
            'edges': visible_edges,
            'total_nodes': len(selected_scope), 'returned_nodes': len(selected_ids),
            'total_edges': len(scope_edges), 'returned_edges': len(visible_edges),
            'truncated': len(selected_ids) < len(selected_scope), 'scope': scope,
        }
