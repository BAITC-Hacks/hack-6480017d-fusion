"""Node detail ranks and complete flows must not depend on the drawn graph."""
import csv
import io
import math
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from backend.ai_config import AISettings
from backend.analysis_config import PRIORITY_WEIGHTS
from backend.app import create_app
from backend.audit import DEFAULT_DATA_DIR, EXPECTED_TYPES


def write_flow_dataset(directory: Path) -> dict:
    """28 nodes, parallel transaction rows, equal-valued edges, a loop, and depth=4."""
    gid = lambda offset: 2**53 + offset
    center, sender1, sender2 = gid(10), gid(8), gid(9)
    targets = [gid(offset) for offset in range(11, 31)]
    chain = [gid(offset) for offset in (31, 32, 33)]
    isolated = [gid(offset) for offset in (34, 35)]
    depths = {center: 0, sender1: 0, sender2: 0, **dict.fromkeys(targets, 1),
              **dict(zip(chain, (2, 3, 4))), **dict.fromkeys(isolated, 0)}
    transfers = [(sender1, center, 5000.0), (sender1, center, 5000.0),
                 (sender2, center, 5000.0), (sender2, center, 5000.0),
                 (center, center, 5000.0), (center, center, 5000.0)]
    for index, target in enumerate(targets):
        transfers.extend((center, target, 5000.0) for _ in range(index % 3 + 1))
    transfers.extend(((targets[0], chain[0], 6000.0), (chain[0], chain[1], 7000.0),
                      (chain[1], chain[2], 8000.0)))
    transactions = pd.DataFrame(transfers, columns=['src', 'dst', 'sum_kzt'])
    transactions['date'] = date(2026, 7, 12)
    edges = transactions.groupby(['src', 'dst'], as_index=False).agg(sum_kzt=('sum_kzt', 'sum'), n_tx=('sum_kzt', 'size'))
    edges['depth'] = edges.src.map(depths) + 1
    nodes = pd.DataFrame([{'gid': key, 'depth': depth, 'is_seed': depth == 0} for key, depth in depths.items()])
    for name, frame in (('nodes', nodes), ('edges', edges), ('transactions', transactions)):
        schema = pa.schema(list(EXPECTED_TYPES[name].items()))
        pq.write_table(pa.Table.from_pandas(frame, schema=schema, preserve_index=False), directory / f'{name}.parquet')
    return {'center': str(center), 'boundary': str(chain[-1]),
            'isolated': [str(key) for key in isolated], 'transactions': transactions}


def assert_flow_totals(case: unittest.TestCase, node: dict, transactions: pd.DataFrame) -> None:
    gid = int(node['gid'])
    for direction, endpoint, amount_field, count_field in (
        ('incoming_edges', 'dst', 'in_kzt', 'in_tx'),
        ('outgoing_edges', 'src', 'out_kzt', 'out_tx'),
    ):
        flows = node[direction]
        raw = transactions.loc[transactions[endpoint].eq(gid)]
        case.assertEqual(sum(edge['n_tx'] for edge in flows), len(raw))
        case.assertEqual(sum(edge['n_tx'] for edge in flows), node[count_field])
        case.assertTrue(math.isclose(math.fsum(edge['sum_kzt'] for edge in flows), float(raw.sum_kzt.sum()), abs_tol=1e-6))
        case.assertTrue(math.isclose(math.fsum(edge['sum_kzt'] for edge in flows), node[amount_field], abs_tol=1e-6))
        case.assertEqual(flows, sorted(flows, key=lambda edge: (-edge['sum_kzt'], int(edge['src']), int(edge['dst']))))
        case.assertEqual(len(flows), len({(edge['src'], edge['dst']) for edge in flows}))
        for edge in flows:
            case.assertEqual(set(edge), {'src', 'dst', 'sum_kzt', 'n_tx'})
            case.assertIsInstance(edge['src'], str)
            case.assertIsInstance(edge['dst'], str)
            case.assertEqual(edge[endpoint], node['gid'])


class NodeDetailsAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.fixture = write_flow_dataset(Path(cls.temporary.name))
        cls.context = TestClient(create_app(Path(cls.temporary.name), ai_settings=AISettings()))
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        cls.temporary.cleanup()

    def test_complete_flows_survive_graph_limit_and_include_loop_in_both_directions(self):
        gid = self.fixture['center']
        before = self.client.get('/api/nodes/' + gid).json()
        limited = self.client.get('/api/graph', params={'gid': gid, 'limit': 1}).json()
        after = self.client.get('/api/nodes/' + gid).json()
        self.assertEqual(before, after)
        self.assertTrue(limited['truncated'])
        self.assertEqual(limited['returned_nodes'], 1)
        self.assertEqual(limited['returned_edges'], 1)  # Only the self-transfer is drawn.
        self.assertEqual(len(after['incoming_edges']), 3)
        self.assertEqual(len(after['outgoing_edges']), 21)
        self.assertEqual((after['in_deg'], after['out_deg']), (2, 20))
        loop = {'src': gid, 'dst': gid, 'sum_kzt': 10000.0, 'n_tx': 2}
        self.assertIn(loop, after['incoming_edges'])
        self.assertIn(loop, after['outgoing_edges'])
        assert_flow_totals(self, after, self.fixture['transactions'])

    def test_each_nodes_flows_reconcile_with_original_transactions(self):
        all_gids = self.client.app.state.analysis.ranked_ids
        for gid in all_gids:
            with self.subTest(gid=gid):
                response = self.client.get('/api/nodes/' + gid)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['gid'], gid)
                assert_flow_totals(self, response.json(), self.fixture['transactions'])

    def test_full_rank_matches_export_and_resolves_isolated_ties_by_exact_gid(self):
        rows = list(csv.DictReader(io.StringIO(self.client.get('/api/exports/nodes_roles.csv').text)))
        ranked = sorted(rows, key=lambda row: (-float(row['priority_score']), int(row['gid'])))
        self.assertEqual(len(ranked), 28)
        top = self.client.get('/api/analysis').json()['top_nodes']
        self.assertEqual(len(top), 20)
        for expected_rank, row in enumerate(ranked, start=1):
            node = self.client.get('/api/nodes/' + row['gid']).json()
            self.assertEqual(node['priority_rank'], expected_rank)
            self.assertEqual(node['priority_total'], 28)
        isolated_cards = [self.client.get('/api/nodes/' + gid).json() for gid in self.fixture['isolated']]
        self.assertEqual([node['priority_rank'] for node in isolated_cards], [27, 28])
        self.assertNotIn(self.fixture['isolated'][0], {row['gid'] for row in top})
        for node in isolated_cards:
            self.assertEqual((node['priority_base'], node['priority_score'], node['priority_factor']), (0, 0, 0))
            self.assertEqual(node['incoming_edges'], [])
            self.assertEqual(node['outgoing_edges'], [])

    def test_boundary_exposes_base_factor_and_rank_without_changing_role(self):
        node = self.client.get('/api/nodes/' + self.fixture['boundary']).json()
        self.assertTrue(node['truncated_by_depth'])
        self.assertNotEqual(node['role'], 'terminal')
        self.assertEqual(node['priority_factor'], 0.5)
        self.assertEqual(node['priority_base'], math.fsum(node['priority_' + name] for name in PRIORITY_WEIGHTS))
        self.assertEqual(node['priority_score'], round(node['priority_base'] * 0.5, 6))
        self.assertEqual(len(node['incoming_edges']), 1)
        self.assertEqual(node['outgoing_edges'], [])
        self.assertIn('множитель полноты 0.5', node['priority_why'])

    def test_priority_explanation_matches_existing_csv_reasoning(self):
        for top in self.client.get('/api/analysis').json()['top_nodes']:
            node = self.client.get('/api/nodes/' + top['gid']).json()
            self.assertEqual(top['why'], node['evidence'] + ' ' + node['priority_why'])
            self.assertEqual(top['rank'], node['priority_rank'])

    def test_callers_cannot_mutate_snapshot_through_flow_details(self):
        service = self.client.app.state.analysis
        gid = self.fixture['center']
        before = service.node(gid)
        changed = service.node(gid)
        changed['incoming_edges'][0]['sum_kzt'] = -1
        changed['outgoing_edges'].clear()
        self.assertEqual(service.node(gid), before)


class RealNodeDetailsAPITests(unittest.TestCase):
    def test_real_high_degree_node_keeps_full_flows_rank_and_precise_ids(self):
        transactions = pd.read_parquet(DEFAULT_DATA_DIR / 'transactions.parquet')
        edges = pd.read_parquet(DEFAULT_DATA_DIR / 'edges.parquet')
        counts = pd.concat([edges.src, edges.dst]).value_counts()
        gid = str(counts.idxmax())
        with TestClient(create_app(ai_settings=AISettings())) as client:
            graph = client.get('/api/graph', params={'gid': gid, 'limit': 1}).json()
            node = client.get('/api/nodes/' + gid).json()
            self.assertTrue(graph['truncated'])
            self.assertEqual(node['gid'], gid)
            self.assertGreater(len(node['incoming_edges']) + len(node['outgoing_edges']), 20)
            assert_flow_totals(self, node, transactions)
            exported = list(csv.DictReader(io.StringIO(client.get('/api/exports/nodes_roles.csv').text)))
            ranked = sorted(exported, key=lambda row: (-float(row['priority_score']), int(row['gid'])))
            self.assertEqual(node['priority_total'], len(ranked))
            self.assertEqual(node['priority_rank'], next(i for i, row in enumerate(ranked, 1) if row['gid'] == gid))
            expected = edges.loc[edges.src.eq(int(gid)) | edges.dst.eq(int(gid))]
            actual_pairs = {(edge['src'], edge['dst']) for edge in node['incoming_edges'] + node['outgoing_edges']}
            self.assertEqual(actual_pairs, {(str(row.src), str(row.dst)) for row in expected.itertuples()})


if __name__ == '__main__':
    unittest.main()
