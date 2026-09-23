"""Behavioral checks for graph motifs and export invariants, not ground-truth labels."""
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

import networkx as nx
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.audit import DEFAULT_DATA_DIR, EXPECTED_TYPES
from backend.features import build_graph, compute_features, undirected_projection
from backend.pipeline import assign_clusters, calculate, run_pipeline, validate_outputs
from backend.roles import classify_node


def motif(edges, attributes=None):
    graph = nx.DiGraph()
    graph.add_weighted_edges_from(edges, weight='sum_kzt')
    for gid in graph:
        graph.nodes[gid].update(depth=1, is_seed=False)
    for src, dst in graph.edges:
        graph[src][dst]['n_tx'] = 1
    for gid, attrs in (attributes or {}).items():
        if gid not in graph:
            graph.add_node(gid, depth=1, is_seed=False)
        graph.nodes[gid].update(attrs)
    return graph


def classified(graph, gid, threshold=1.0):
    row = next(row for row in compute_features(graph).to_dict('records') if row['gid'] == gid)
    return classify_node(row, threshold)


class MotifTests(unittest.TestCase):
    def test_isolated_and_truncated_are_not_terminal(self):
        graph = motif([(1, 2, 100)], {2: {'depth': 4}, 3: {'is_seed': True, 'depth': 0}})
        self.assertEqual(classified(graph, 2)[2], 'boundary')
        self.assertEqual(classified(graph, 3)[2], 'isolated')
        self.assertIn('Граница', classified(graph, 2)[3])

    def test_transit_requires_non_seed_and_balanced_observed_flows(self):
        graph = motif([(1, 2, 100), (2, 3, 100)])
        self.assertEqual(classified(graph, 2)[0], 'transit')
        graph.nodes[2]['is_seed'] = True
        self.assertEqual(classified(graph, 2)[0], 'peripheral')
        graph.nodes[2]['is_seed'] = False
        graph[2][3]['sum_kzt'] = 250
        self.assertNotEqual(classified(graph, 2)[0], 'transit')

    def test_consolidation_distributor_and_terminal_motifs(self):
        graph = motif([(1, 4, 100), (2, 4, 100), (3, 4, 100), (4, 5, 30)])
        self.assertEqual(classified(graph, 4)[0], 'consolidator')
        self.assertEqual(classified(graph, 5)[0], 'terminal')
        fanout = motif([(1, i, 100) for i in range(2, 12)])
        self.assertEqual(classified(fanout, 1)[0], 'distributor')

    def test_coordinator_needs_multiple_seed_paths_and_high_pagerank(self):
        graph = motif([(1, 4, 100), (2, 4, 100), (3, 4, 100), (4, 5, 50), (4, 6, 50)],
                      {1: {'is_seed': True}, 2: {'is_seed': True}})
        self.assertEqual(classified(graph, 4, threshold=0.0)[0], 'coordinator')
        self.assertNotEqual(classified(graph, 4, threshold=1.0)[0], 'coordinator')
        graph.nodes[2]['is_seed'] = False
        self.assertNotEqual(classified(graph, 4, threshold=0.0)[0], 'coordinator')

    def test_reciprocal_projection_sums_both_directions(self):
        projected = undirected_projection(motif([(1, 2, 100), (2, 1, 50)]))
        self.assertEqual(projected[1][2]['weight'], 150)

    def test_seed_reach_is_directional_bounded_and_deduplicated(self):
        graph = motif([(1, 2, 1), (1, 3, 1), (2, 4, 1), (3, 4, 1),
                       (4, 5, 1), (5, 6, 1), (6, 7, 1), (4, 1, 1)],
                      {1: {'is_seed': True}})
        rows = compute_features(graph).set_index('gid')
        self.assertEqual(rows.loc[4, 'seed_reach'], 1)
        self.assertEqual(rows.loc[1, 'seed_reach'], 0)
        self.assertEqual(rows.loc[7, 'seed_reach'], 0)

    def test_self_loop_is_not_a_counterparty_or_transit(self):
        graph = motif([(1, 1, 100)])
        self.assertEqual(classified(graph, 1)[0], 'peripheral')
        self.assertEqual(compute_features(graph).iloc[0]['counterparties'], 0)
        self.assertEqual(assign_clusters(graph), {1: 0})


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nodes = pd.read_parquet(DEFAULT_DATA_DIR / 'nodes.parquet')
        cls.edges = pd.read_parquet(DEFAULT_DATA_DIR / 'edges.parquet')
        cls.roles, cls.clusters, cls.top, cls.report = calculate(cls.nodes, cls.edges)

    def test_all_nodes_scores_evidence_and_boundary_coverage(self):
        self.assertEqual(set(self.roles.gid), set(self.nodes.gid))
        self.assertEqual(self.roles.gid.dtype, np.dtype('int64'))
        self.assertTrue(self.roles.evidence.str.len().between(1, 200).all())
        self.assertEqual(self.roles.is_isolated.sum(), 19)
        self.assertEqual(self.roles.truncated_by_depth.sum(), 444)
        self.assertTrue(self.roles.loc[self.roles.truncated_by_depth, 'role'].eq('peripheral').all())
        self.assertTrue(self.roles.loc[self.roles.is_isolated, 'priority_score'].eq(0).all())
        for name in ('role_score', 'priority_score'):
            self.assertTrue(np.isfinite(self.roles[name]).all())
            self.assertTrue(self.roles[name].between(0, 1).all())

    def test_row_order_does_not_change_exports(self):
        shuffled = calculate(self.nodes.sample(frac=1, random_state=2), self.edges.sample(frac=1, random_state=7))
        for actual, expected in zip(shuffled[:3], (self.roles, self.clusters, self.top)):
            pd.testing.assert_frame_equal(actual.reset_index(drop=True), expected.reset_index(drop=True))

    def test_cluster_membership_turnover_and_top_ids(self):
        membership = dict(zip(self.roles.gid, self.roles.cluster_id))
        expected = {}
        for row in self.edges.itertuples(index=False):
            if membership[row.src] == membership[row.dst]:
                key = membership[row.src]
                expected[key] = expected.get(key, 0) + row.sum_kzt
        for row in self.clusters.itertuples(index=False):
            self.assertAlmostEqual(row.sum_kzt_internal, expected.get(row.cluster_id, 0), places=5)
            self.assertTrue(all(isinstance(gid, str) and membership[int(gid)] == row.cluster_id
                                for gid in json.loads(row.top_gids)))
        self.assertEqual(self.clusters.n_nodes.sum(), len(self.nodes))
        self.assertEqual(self.clusters.n_seed.sum(), self.nodes.is_seed.sum())

    def test_priority_is_reconstructible_from_exported_contributions(self):
        names = [col for col in self.roles if col.startswith('priority_') and col not in ('priority_score', 'priority_factor')]
        expected = (self.roles[names].sum(axis=1) * self.roles.priority_factor).round(6)
        np.testing.assert_allclose(expected, self.roles.priority_score, atol=1e-6)

    def test_csv_roundtrip_int64_and_deterministic_bytes(self):
        with tempfile.TemporaryDirectory() as out:
            report = run_pipeline(out_dir=Path(out))
            original = {name: (Path(out) / name).read_bytes() for name in report['outputs']}
            for name in ('nodes_roles.csv', 'top_nodes.csv'):
                loaded = pd.read_csv(Path(out) / name, dtype={'gid': 'int64'})
                expected = self.roles if name == 'nodes_roles.csv' else self.top
                self.assertEqual(loaded.gid.tolist(), expected.gid.tolist())
            run_pipeline(out_dir=Path(out))
            self.assertEqual(original, {name: (Path(out) / name).read_bytes() for name in original})

    def test_invalid_scores_and_missing_node_rejected(self):
        bad = self.roles.copy()
        bad.loc[0, 'role_score'] = np.nan
        with self.assertRaises(ValueError):
            validate_outputs(self.nodes, bad, self.clusters, self.top)
        with self.assertRaises(ValueError):
            validate_outputs(self.nodes, self.roles.iloc[1:], self.clusters, self.top)

    def test_small_valid_parquet_pipeline_does_not_hardcode_size(self):
        big = 100000003684369100
        rows = {
            'nodes': [{'gid': big, 'depth': 0, 'is_seed': True},
                      {'gid': big+1, 'depth': 1, 'is_seed': False},
                      {'gid': big+2, 'depth': 0, 'is_seed': True}],
            'edges': [{'src': big, 'dst': big+1, 'sum_kzt': 10000., 'n_tx': 2, 'depth': 1}],
            'transactions': [{'src': big, 'dst': big+1, 'date': date(2026, 7, 1), 'sum_kzt': 5000.}] * 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name, records in rows.items():
                schema = pa.schema(list(EXPECTED_TYPES[name].items()))
                pq.write_table(pa.Table.from_pylist(records, schema=schema), path / f'{name}.parquet')
            result = run_pipeline(path, path / 'out')
            self.assertEqual(result['nodes'], 3)
            self.assertEqual(result['outputs']['top_nodes.csv']['rows'], 3)
            self.assertEqual(result['isolated_nodes'], 1)
            loaded = pd.read_csv(path / 'out/nodes_roles.csv', dtype={'gid': 'int64'})
            self.assertEqual(loaded.gid.tolist(), [big, big+1, big+2])
            # Reject inconsistent data without replacing an existing successful export.
            before = (path / 'out/nodes_roles.csv').read_bytes()
            rows['edges'][0]['sum_kzt'] = 15000.
            pq.write_table(pa.Table.from_pylist(rows['edges'], schema=pa.schema(list(EXPECTED_TYPES['edges'].items()))), path / 'edges.parquet')
            with self.assertRaises(ValueError):
                run_pipeline(path, path / 'out')
            self.assertEqual((path / 'out/nodes_roles.csv').read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
