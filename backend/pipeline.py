"""One-command deterministic parquet -> roles, communities, ranked CSV exports."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import tempfile
import time

import networkx as nx
import numpy as np
import pandas as pd

from backend import analysis_config as c
from backend.audit import DEFAULT_DATA_DIR, PROJECT_ROOT, _sha256, assert_valid_audit, audit_data
from backend.features import build_graph, compute_features, undirected_projection
from backend.roles import assign_roles_and_priority

NODE_COLUMNS = ['gid', 'role', 'role_score', 'cluster_id', 'priority_score', 'evidence']
CLUSTER_COLUMNS = ['cluster_id', 'n_nodes', 'n_seed', 'sum_kzt_internal', 'top_gids', 'hypothesis']
TOP_COLUMNS = ['rank', 'gid', 'role', 'priority_score', 'why']
PRIORITY_NAMES = {'pagerank': 'PageRank', 'turnover': 'оборот', 'seed_reach': 'связь с seed',
                  'counterparties': 'контрагенты', 'transactions': 'переводы'}
ROLE_NAMES = {'consolidator': 'консолидации', 'transit': 'транзита',
              'distributor': 'распределения', 'terminal': 'получения',
              'coordinator': 'связующего узла', 'peripheral': 'периферии'}


def assign_clusters(graph: nx.DiGraph) -> dict[int, int]:
    projected = undirected_projection(graph)
    isolated = sorted(nx.isolates(projected))
    isolated_ids = set(isolated)
    connected = projected.subgraph([gid for gid in projected if gid not in isolated_ids]).copy()
    groups = nx.community.louvain_communities(
        connected, weight='weight', resolution=c.LOUVAIN_RESOLUTION, seed=c.LOUVAIN_SEED,
    ) if connected.number_of_edges() else []
    groups.extend({gid} for gid in isolated)
    # Stable labels after community discovery, irrespective of set iteration order.
    groups.sort(key=lambda group: (-len(group), min(group)))
    return {gid: cluster_id for cluster_id, group in enumerate(groups) for gid in sorted(group)}


def calculate(nodes: pd.DataFrame, edges: pd.DataFrame, top_n: int = c.DEFAULT_TOP_N):
    graph = build_graph(nodes, edges)
    features = compute_features(graph)
    roles, threshold = assign_roles_and_priority(features)
    membership = assign_clusters(graph)
    roles['cluster_id'] = roles.gid.map(membership).astype('int64')
    roles = roles[NODE_COLUMNS + [col for col in roles.columns if col not in NODE_COLUMNS]]
    ranked = roles.sort_values(['priority_score', 'gid'], ascending=[False, True], kind='stable')
    internal = Counter()
    for src, dst, attrs in graph.edges(data=True):
        if membership[src] == membership[dst]:
            internal[membership[src]] += attrs['sum_kzt']
    clusters = []
    for cluster_id, group in ranked.groupby('cluster_id', sort=True):
        counts = Counter(group.role)
        dominant = sorted(counts, key=lambda role: (-counts[role], role))[0]
        isolated = len(group) == 1 and bool(group.iloc[0]['is_isolated'])
        hypothesis = ('Изолированный узел: связей 0, назначение неизвестно.' if isolated else
            f"Группа по плотности денежных связей: {len(group)} узлов, {int(group.is_seed.sum())} seed; "
            f"чаще признаки {ROLE_NAMES[dominant]} ({counts[dominant]}). Совместная деятельность не установлена.")
        clusters.append({
            'cluster_id': int(cluster_id), 'n_nodes': len(group),
            'n_seed': int(group.is_seed.sum()), 'sum_kzt_internal': float(internal[cluster_id]),
            'top_gids': json.dumps([str(gid) for gid in group.gid.head(5)], ensure_ascii=False),
            'hypothesis': hypothesis,
        })
    top = ranked.head(min(max(top_n, c.DEFAULT_TOP_N), len(roles)))[
        ['gid', 'role', 'priority_score']].copy()
    top.insert(0, 'rank', range(1, len(top) + 1))
    explanation = {}
    for row in ranked.to_dict('records'):
        contributions = sorted(c.PRIORITY_WEIGHTS, key=lambda name: (-row['priority_' + name], name))[:2]
        parts = ', '.join(f'{PRIORITY_NAMES[name]}={row["priority_" + name]:.3f}' for name in contributions)
        explanation[row['gid']] = (f"{row['evidence']} Приоритет {row['priority_score']:.3f}: "
                                   f"основные слагаемые {parts}; множитель полноты {row['priority_factor']:.1f}.")
    top['why'] = top.gid.map(explanation)
    cluster_frame = pd.DataFrame(clusters, columns=CLUSTER_COLUMNS)
    validate_outputs(nodes, roles, cluster_frame, top)
    metadata = {
        'rules_version': c.RULES_VERSION, 'nodes': len(roles), 'edges': graph.number_of_edges(),
        'clusters': len(clusters), 'weak_components': nx.number_weakly_connected_components(graph),
        'isolated_nodes': int(roles.is_isolated.sum()),
        'boundary_nodes': int(roles.truncated_by_depth.sum()),
        'role_counts': dict(sorted(Counter(roles.role).items())),
        'coordinator_pagerank_threshold': threshold if math.isfinite(threshold) else None,
        'louvain_seed': c.LOUVAIN_SEED, 'louvain_resolution': c.LOUVAIN_RESOLUTION,
        'priority_weights': c.PRIORITY_WEIGHTS,
    }
    return roles, cluster_frame, top, metadata


def validate_outputs(nodes, roles, clusters, top):
    """Explicit errors also under python -O; validate mandatory output contracts."""
    for frame, columns in [(roles, NODE_COLUMNS), (clusters, CLUSTER_COLUMNS), (top, TOP_COLUMNS)]:
        if any(col not in frame for col in columns) or frame[columns].isna().any().any():
            raise ValueError('Выходная схема неполна или обязательные поля содержат пропуски')
    if not roles.gid.is_unique or set(roles.gid) != set(nodes.gid) or len(roles) != len(nodes):
        raise ValueError('Потеря или дублирование входных узлов')
    if not roles.gid.dtype == np.dtype('int64') or not top.gid.dtype == np.dtype('int64'):
        raise ValueError('gid должны остаться int64')
    if not roles.role.isin(c.ROLES).all():
        raise ValueError('Неизвестная роль')
    for name in ('role_score', 'priority_score'):
        if not np.isfinite(roles[name]).all() or not roles[name].between(0, 1).all():
            raise ValueError('Некорректный скор')
    if not roles.evidence.str.len().between(1, 200).all():
        raise ValueError('evidence должен содержать 1–200 символов')
    if (roles.loc[roles.truncated_by_depth, 'role'] == 'terminal').any():
        raise ValueError('Граница выгрузки не должна обозначаться terminal')
    if not clusters.cluster_id.is_unique or set(roles.cluster_id) != set(clusters.cluster_id):
        raise ValueError('Неполное разбиение на кластеры')
    sizes = roles.groupby('cluster_id').size().to_dict()
    seeds = roles.groupby('cluster_id').is_seed.sum().to_dict()
    for row in clusters.itertuples(index=False):
        if row.n_nodes != sizes[row.cluster_id] or row.n_seed != seeds[row.cluster_id]:
            raise ValueError('Размер или число seed кластера неверны')
        if not row.hypothesis.strip() or not math.isfinite(row.sum_kzt_internal) or row.sum_kzt_internal < 0:
            raise ValueError('Некорректная гипотеза или оборот кластера')
    expected = roles.sort_values(['priority_score', 'gid'], ascending=[False, True]).head(len(top))
    if (len(top) < min(c.DEFAULT_TOP_N, len(nodes)) or top.gid.tolist() != expected.gid.tolist()
            or top['rank'].tolist() != list(range(1, len(top) + 1))
            or top.role.tolist() != expected.role.tolist()
            or top.priority_score.tolist() != expected.priority_score.tolist()
            or not top.why.str.strip().ne('').all()):
        raise ValueError('Топ-лист не соответствует ранжированию узлов')


def run_pipeline(data_dir: Path = DEFAULT_DATA_DIR, out_dir: Path = PROJECT_ROOT / 'artifacts',
                 top_n: int = c.DEFAULT_TOP_N) -> dict:
    started = time.perf_counter()
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    if data_dir.resolve() == out_dir.resolve():
        raise ValueError('Выходные файлы должны находиться отдельно от входных')
    audit = audit_data(data_dir)
    assert_valid_audit(audit)
    nodes = pd.read_parquet(data_dir / 'nodes.parquet')
    edges = pd.read_parquet(data_dir / 'edges.parquet')
    roles, clusters, top, metadata = calculate(nodes, edges, top_n)
    for name, digest in audit['hashes'].items():
        if _sha256(data_dir / f'{name}.parquet') != digest:
            raise ValueError('Исходные данные изменились во время расчёта')
    metadata.update({'input_hashes': audit['hashes'], 'audit_checks': len(audit['checks']),
                     'period': audit['summary']['period'],
                     'versions': audit['versions'], 'outputs': {}})
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.fusion-', dir=out_dir) as temporary:
        staging = Path(temporary)
        for filename, frame in [('nodes_roles.csv', roles), ('clusters.csv', clusters), ('top_nodes.csv', top)]:
            destination = staging / filename
            frame.to_csv(destination, index=False, encoding='utf-8', lineterminator='\n', float_format='%.12g')
            metadata['outputs'][filename] = {'rows': len(frame), 'sha256': _sha256(destination)}
        metadata['elapsed_seconds'] = round(time.perf_counter() - started, 6)
        (staging / 'run_report.json').write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        # Only publish fully computed/validated exports; no intermediate empty CSVs.
        for filename in (*metadata['outputs'], 'run_report.json'):
            (staging / filename).replace(out_dir / filename)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description='Fusion: проверка parquet, роли, кластеры и 3 CSV без API')
    parser.add_argument('--data', type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument('--out', type=Path, default=PROJECT_ROOT / 'artifacts')
    parser.add_argument('--top', type=int, default=c.DEFAULT_TOP_N)
    args = parser.parse_args()
    try:
        report = run_pipeline(args.data, args.out, args.top)
    except (OSError, ValueError, nx.NetworkXException) as error:
        print(f'Расчёт остановлен: {error}')
        return 1
    print(f"Fusion: {report['nodes']} узлов, {report['clusters']} кластеров; {report['elapsed_seconds']:.3f} с")
    print('Роли: ' + ', '.join(f'{role}={count}' for role, count in report['role_counts'].items()))
    print(f'Выгрузки: {args.out.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
