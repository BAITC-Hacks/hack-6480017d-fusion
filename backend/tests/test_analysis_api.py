"""API contract and integration checks for the actual graph exploration flow."""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.audit import DEFAULT_DATA_DIR
from backend.pipeline import run_pipeline
from backend.tests.test_api import write_small_dataset


class SmallAnalysisAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        write_small_dataset(self.directory)
        self.context = TestClient(create_app(self.directory))
        self.client = self.context.__enter__()
        self.source, self.target, self.isolated = (str(2**53 + offset) for offset in (1, 2, 3))

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.temporary.cleanup()

    def test_summary_covers_all_nodes_and_preserves_large_ids(self):
        response = self.client.get('/api/analysis')
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual((result['nodes'], result['edges'], result['transactions']), (3, 1, 1))
        self.assertEqual(result['seed_nodes'], 2)
        self.assertEqual(result['total_kzt'], 5000.0)
        self.assertEqual(sum(result['role_counts'].values()), 3)
        self.assertEqual(sum(row['n_nodes'] for row in result['clusters']), 3)
        self.assertEqual({row['gid'] for row in result['top_nodes']}, {self.source, self.target, self.isolated})
        self.assertTrue(all(isinstance(gid, str) for cluster in result['clusters'] for gid in cluster['top_gids']))

    def test_card_null_ratio_and_isolated_seed_caveats(self):
        row = self.client.get(f'/api/nodes/{self.isolated}').json()
        self.assertEqual(row['gid'], self.isolated)
        self.assertTrue(row['is_isolated'])
        self.assertTrue(row['is_seed'])
        self.assertEqual(row['priority_score'], 0)
        self.assertIsNone(row['pass_through'])
        self.assertIn('seed', ' '.join(row['caveats']))
        self.assertIn('нет связей', ' '.join(row['caveats']))
        self.assertIn('5 000', ' '.join(row['caveats']))
        self.assertIn('rule_id', row)

    def test_graph_directions_totals_and_selected_low_priority_node(self):
        complete = self.client.get('/api/graph', params={'gid': self.source}).json()
        self.assertEqual(complete['edges'], [{'src': self.source, 'dst': self.target, 'sum_kzt': 5000.0, 'n_tx': 1}])
        self.assertEqual((complete['total_nodes'], complete['returned_nodes']), (2, 2))
        self.assertFalse(complete['truncated'])
        limited = self.client.get('/api/graph', params={'gid': self.source, 'limit': 1}).json()
        self.assertEqual([node['gid'] for node in limited['nodes']], [self.source])
        self.assertEqual((limited['total_nodes'], limited['returned_nodes']), (2, 1))
        self.assertEqual((limited['total_edges'], limited['returned_edges']), (1, 0))
        self.assertTrue(limited['truncated'])
        self.assertEqual(limited, self.client.get('/api/graph', params={'gid': self.source, 'limit': 1}).json())

    def test_isolated_node_and_its_cluster_are_still_visible(self):
        node = self.client.get(f'/api/nodes/{self.isolated}').json()
        for params in ({'gid': self.isolated}, {'cluster_id': node['cluster_id']}):
            with self.subTest(params=params):
                graph = self.client.get('/api/graph', params=params).json()
                self.assertEqual([row['gid'] for row in graph['nodes']], [self.isolated])
                self.assertEqual(graph['edges'], [])
                self.assertEqual(graph['total_edges'], 0)
                self.assertFalse(graph['truncated'])

    def test_invalid_or_unknown_filters_are_explicit(self):
        for gid in ('1.0', 'abc', str(2**63), '001'):
            with self.subTest(gid=gid):
                self.assertEqual(self.client.get(f'/api/nodes/{gid}').status_code, 422)
                self.assertEqual(self.client.get('/api/graph', params={'gid': gid}).status_code, 422)
        for params in ({'limit': 0}, {'limit': 501}, {'limit': 'bad'}, {'cluster_id': -1},
                       {'cluster_id': 0, 'gid': self.source}):
            self.assertEqual(self.client.get('/api/graph', params=params).status_code, 422)
        self.assertEqual(self.client.get('/api/nodes/123').status_code, 404)
        self.assertEqual(self.client.get('/api/graph', params={'gid': '123'}).status_code, 404)
        self.assertEqual(self.client.get('/api/graph', params={'cluster_id': 999}).status_code, 404)

    def test_default_is_largest_cluster_and_exports_are_full_snapshots(self):
        default = self.client.get('/api/graph').json()
        self.assertEqual(default['total_nodes'], 2)
        self.client.get('/api/graph', params={'limit': 1})
        response = self.client.get('/api/exports/nodes_roles.csv')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response.headers['content-type'])
        self.assertEqual(response.headers['content-disposition'], 'attachment; filename="nodes_roles.csv"')
        rows = list(csv.DictReader(io.StringIO(response.text)))
        self.assertEqual({row['gid'] for row in rows}, {self.source, self.target, self.isolated})
        for path in ('run_report.json', 'nodes.parquet', '..%2F.env', '%2e%2e%2fdata%2fnodes.parquet'):
            self.assertEqual(self.client.get('/api/exports/' + path).status_code, 404)

    def test_calculation_is_reused_without_writing_artifacts(self):
        original_files = sorted(path.name for path in self.directory.iterdir())
        with patch('backend.analysis_service.calculate', side_effect=AssertionError('Unexpected recalculation')):
            for route in ('/api/analysis', '/api/graph', f'/api/nodes/{self.isolated}', '/api/exports/clusters.csv'):
                self.assertEqual(self.client.get(route).status_code, 200)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), original_files)


class AnalysisFailureTests(unittest.TestCase):
    def test_corrupt_data_disables_results_but_not_health(self):
        with tempfile.TemporaryDirectory() as directory:
            write_small_dataset(Path(directory), edge_amount=6000)
            with self.assertLogs('backend.app', level='ERROR'):
                with TestClient(create_app(Path(directory))) as client:
                    for route in ('/api/analysis', '/api/graph', '/api/nodes/123', '/api/exports/nodes_roles.csv'):
                        self.assertEqual(client.get(route).status_code, 503)
                    self.assertEqual(client.get('/api/health').status_code, 200)
                    self.assertEqual(client.get('/api/summary').status_code, 503)

    def test_calculation_failure_preserves_valid_basic_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            write_small_dataset(Path(directory))
            with patch('backend.analysis_service.calculate', side_effect=ValueError('calculation failed')):
                with self.assertLogs('backend.app', level='ERROR'):
                    with TestClient(create_app(Path(directory))) as client:
                        self.assertEqual(client.get('/api/health').status_code, 200)
                        self.assertEqual(client.get('/api/summary').status_code, 200)
                        self.assertEqual(client.get('/api/analysis').status_code, 503)


class ActualDataAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.context = TestClient(create_app())
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)

    def test_all_input_nodes_in_exports_and_boundary_is_not_terminal(self):
        inputs = pd.read_parquet(DEFAULT_DATA_DIR / 'nodes.parquet')
        csv_response = self.client.get('/api/exports/nodes_roles.csv')
        rows = list(csv.DictReader(io.StringIO(csv_response.text)))
        self.assertEqual({row['gid'] for row in rows}, {str(gid) for gid in inputs.gid})
        for gid in (str(row['gid']) for row in rows if row['is_isolated'] == 'True'):
            self.assertTrue(self.client.get(f'/api/nodes/{gid}').json()['is_isolated'])
        gid = str(inputs.loc[inputs.depth.eq(4), 'gid'].iloc[0])
        node = self.client.get(f'/api/nodes/{gid}').json()
        self.assertTrue(node['truncated_by_depth'])
        self.assertNotEqual(node['role'], 'terminal')
        self.assertIn('Граница выгрузки', ' '.join(node['caveats']))
        graph = self.client.get('/api/graph', params={'gid': gid, 'limit': 1}).json()
        self.assertEqual(graph['nodes'][0]['gid'], gid)
        self.assertTrue(graph['nodes'][0]['truncated_by_depth'])

    def test_downloads_exactly_match_pipeline_and_top_ranking(self):
        with tempfile.TemporaryDirectory() as directory:
            run_pipeline(out_dir=Path(directory))
            for filename in ('nodes_roles.csv', 'clusters.csv', 'top_nodes.csv'):
                response = self.client.get('/api/exports/' + filename)
                self.assertEqual(response.content, (Path(directory) / filename).read_bytes())
        summary = self.client.get('/api/analysis').json()
        self.assertGreaterEqual(len(summary['top_nodes']), 20)
        scores = [node['priority_score'] for node in summary['top_nodes']]
        self.assertEqual(scores, sorted(scores, reverse=True))
        cluster_csv = self.client.get('/api/exports/clusters.csv').text
        csv_clusters = list(csv.DictReader(io.StringIO(cluster_csv)))
        self.assertEqual(summary['clusters_count'], len(csv_clusters))
        self.assertEqual(summary['clusters'][0]['top_gids'], json.loads(csv_clusters[0]['top_gids']))


if __name__ == '__main__':
    unittest.main()
