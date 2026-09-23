"""Dataset switching is atomic across cards, graphs, downloads and AI state."""
import csv
import io
import tempfile
import time
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from backend.ai_config import AISettings
from backend.app import create_app
from backend.audit import EXPECTED_TYPES
from backend.tests.test_api import write_small_dataset


TABLES = ('nodes', 'edges', 'transactions')
BASE_ID = 2**53 + 1


def write_uploaded_dataset(directory: Path, *, offset: int = 100,
                           first_amount: float = 25000.0,
                           first_date: date = date(2026, 7, 12)) -> list[str]:
    """A two-hop chain, two transfers on its second edge and an isolated seed."""
    directory.mkdir(parents=True, exist_ok=True)
    source, middle, target, isolated = [BASE_ID + offset + index for index in range(4)]
    rows = {
        'nodes': {'gid': [source, middle, target, isolated], 'depth': [0, 1, 2, 0],
                  'is_seed': [True, False, False, True]},
        'edges': {'src': [source, middle], 'dst': [middle, target],
                  'sum_kzt': [first_amount, 12000.0], 'n_tx': [1, 2], 'depth': [1, 2]},
        'transactions': {'src': [source, middle, middle], 'dst': [middle, target, target],
                         'sum_kzt': [first_amount, 6000.0, 6000.0],
                         'date': [first_date, first_date, first_date + timedelta(days=1)]},
    }
    for name in TABLES:
        schema = pa.schema(list(EXPECTED_TYPES[name].items()))
        pq.write_table(pa.Table.from_pydict(rows[name], schema=schema), directory / f'{name}.parquet')
    return [str(gid) for gid in (source, middle, target, isolated)]


def multipart(directory: Path):
    return [('files', (f'{name}.parquet', (directory / f'{name}.parquet').read_bytes(),
                       'application/octet-stream')) for name in TABLES]


def zip_upload(entries: list[tuple[str, bytes]]) -> list:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return [('files', ('network.zip', output.getvalue(), 'application/zip'))]


class DatasetAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original = self.root / 'original'
        self.original.mkdir()
        write_small_dataset(self.original)
        self.original_bytes = {name: (self.original / f'{name}.parquet').read_bytes() for name in TABLES}
        self.upload = self.root / 'upload'
        self.ids = write_uploaded_dataset(self.upload)
        self.store = self.root / 'store'
        self.settings = AISettings()
        self.app = create_app(self.original, ai_settings=self.settings, dataset_store_dir=self.store)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()

    def tearDown(self):
        try:
            for name, expected in self.original_bytes.items():
                self.assertEqual((self.original / f'{name}.parquet').read_bytes(), expected,
                                 f'Original {name} was modified')
        finally:
            self.context.__exit__(None, None, None)
            self.temporary.cleanup()

    def snapshot(self) -> dict:
        return {
            'metadata': self.client.get('/api/datasets/current').json(),
            'analysis': self.client.get('/api/analysis').json(),
            'summary': self.client.get('/api/summary').json(),
            'csv': {name: self.client.get(f'/api/exports/{name}.csv').content
                    for name in ('nodes_roles', 'clusters', 'top_nodes')},
        }

    def assert_rejected_without_switch(self, files: list, status: int = 422):
        before = self.snapshot()
        old_analysis, old_ai = self.app.state.analysis, self.app.state.ai
        response = self.client.post('/api/datasets', files=files)
        self.assertEqual(response.status_code, status, response.text)
        self.assertIsInstance(response.json().get('detail'), str)
        self.assertEqual(self.snapshot(), before)
        self.assertIs(self.app.state.analysis, old_analysis)
        self.assertIs(self.app.state.ai, old_ai)
        return response

    def test_default_metadata_describes_the_active_fixture(self):
        response = self.client.get('/api/datasets/current')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['is_default'])
        self.assertEqual((data['nodes'], data['edges'], data['transactions']), (3, 1, 1))
        self.assertEqual(data['period'], {'start': '2026-07-12', 'end': '2026-07-12'})
        for key in ('id', 'name', 'loaded_at'):
            self.assertIsInstance(data[key], str)
            self.assertTrue(data[key])

    def test_three_parquet_files_replace_analysis_card_graph_and_full_csv(self):
        response = self.client.post('/api/datasets', files=multipart(self.upload))
        self.assertEqual(response.status_code, 200, response.text)
        metadata = self.client.get('/api/datasets/current').json()
        self.assertFalse(metadata['is_default'])
        self.assertEqual((metadata['nodes'], metadata['edges'], metadata['transactions']), (4, 2, 3))
        analysis = self.client.get('/api/analysis').json()
        self.assertEqual((analysis['nodes'], analysis['edges'], analysis['transactions']), (4, 2, 3))
        self.assertEqual(analysis['total_kzt'], 37000.0)
        self.assertEqual({item['gid'] for item in analysis['top_nodes']}, set(self.ids))
        card = self.client.get(f'/api/nodes/{self.ids[1]}').json()
        self.assertEqual((card['gid'], card['in_kzt'], card['out_kzt']), (self.ids[1], 25000.0, 12000.0))
        graph = self.client.get('/api/graph', params={'gid': self.ids[1]}).json()
        self.assertEqual({node['gid'] for node in graph['nodes']}, set(self.ids[:3]))
        self.assertEqual(graph['total_edges'], 2)
        self.assertTrue(all(isinstance(edge[key], str) for edge in graph['edges'] for key in ('src', 'dst')))
        export = self.client.get('/api/exports/nodes_roles.csv')
        rows = list(csv.DictReader(io.StringIO(export.text)))
        self.assertEqual({row['gid'] for row in rows}, set(self.ids))
        self.assertEqual(len(rows), 4)
        self.assertEqual(self.client.get(f'/api/nodes/{BASE_ID}').status_code, 404)

    def test_zip_with_nested_data_directory_is_supported(self):
        files = zip_upload([(f'provided/data/{name}.parquet', content[1])
                            for name, (_, content) in zip(TABLES, multipart(self.upload))])
        response = self.client.post('/api/datasets', files=files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/analysis').json()['nodes'], 4)
        self.assertFalse(self.client.get('/api/datasets/current').json()['is_default'])

    def test_zip_macos_metadata_and_starter_are_ignored_without_execution(self):
        marker = self.root / 'starter-was-executed'
        entries = [(f'provided/data/{name}.parquet',
                    (self.upload / f'{name}.parquet').read_bytes()) for name in TABLES]
        entries.extend([
            ('__MACOSX/provided/data/._nodes.parquet', b'not a parquet: macOS resource fork'),
            ('provided/.DS_Store', b'macOS Finder metadata'),
            ('provided/starter.py',
             f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n'.encode()),
            ('provided/case.md', b'Additional case description'),
        ])
        response = self.client.post('/api/datasets', files=zip_upload(entries))
        self.assertEqual(response.status_code, 200, response.text)
        metadata = response.json()
        self.assertEqual((metadata['nodes'], metadata['edges'], metadata['transactions']), (4, 2, 3))
        installed = self.store / 'datasets' / metadata['id']
        self.assertEqual({item.relative_to(installed).as_posix() for item in installed.rglob('*')},
                         {f'{name}.parquet' for name in TABLES})
        for name in TABLES:
            self.assertEqual((installed / f'{name}.parquet').read_bytes(),
                             (self.upload / f'{name}.parquet').read_bytes())
        self.assertFalse(marker.exists(), 'Archive starter code must never be executed')

    def test_uploaded_data_can_use_another_month(self):
        write_uploaded_dataset(self.upload, first_date=date(2026, 8, 8))
        response = self.client.post('/api/datasets', files=multipart(self.upload))
        self.assertEqual(response.status_code, 200, response.text)
        expected = {'start': '2026-08-08', 'end': '2026-08-09'}
        for route in ('/api/datasets/current', '/api/summary', '/api/analysis'):
            self.assertEqual(self.client.get(route).json()['period'], expected)

    def test_bad_aggregates_and_truncated_parquet_preserve_last_uploaded_data(self):
        response = self.client.post('/api/datasets', files=multipart(self.upload))
        self.assertEqual(response.status_code, 200, response.text)
        invalid = self.root / 'invalid'
        invalid.mkdir()
        write_small_dataset(invalid, edge_amount=6000.0)
        self.assert_rejected_without_switch(multipart(invalid))
        broken = multipart(self.upload)
        broken[0] = ('files', ('nodes.parquet', b'PAR1truncated', 'application/octet-stream'))
        self.assert_rejected_without_switch(broken)
        self.assertEqual(self.client.get('/api/analysis').json()['total_kzt'], 37000.0)

    def test_missing_and_duplicate_direct_parquet_files_are_rejected(self):
        files = multipart(self.upload)
        for invalid in (files[:2], [files[0], files[0], files[2]]):
            with self.subTest(names=[item[1][0] for item in invalid]):
                self.assert_rejected_without_switch(invalid)

    def test_zip_missing_duplicate_and_traversal_entries_are_rejected(self):
        contents = {name: (self.upload / f'{name}.parquet').read_bytes() for name in TABLES}
        valid = [(f'data/{name}.parquet', contents[name]) for name in TABLES]
        cases = {
            'missing': valid[:2],
            'duplicate_basename': valid + [('other/nodes.parquet', contents['nodes'])],
            'traversal': [('../nodes.parquet', contents['nodes'])] + valid[1:],
            'absolute_path': [('/nodes.parquet', contents['nodes'])] + valid[1:],
        }
        for name, entries in cases.items():
            with self.subTest(name=name):
                self.assert_rejected_without_switch(zip_upload(entries))
        self.assertFalse((self.root / 'nodes.parquet').exists())

    def test_upload_and_uncompressed_size_limits_reject_without_switch(self):
        with patch('backend.datasets.MAX_UPLOAD_BYTES', 32):
            self.assert_rejected_without_switch(multipart(self.upload), status=413)
        entries = [(f'data/{name}.parquet', (self.upload / f'{name}.parquet').read_bytes()) for name in TABLES]
        with patch('backend.datasets.MAX_UNCOMPRESSED_BYTES', 32):
            self.assert_rejected_without_switch(zip_upload(entries), status=413)

    def test_row_limit_rejects_without_switch(self):
        with patch('backend.datasets.MAX_ROWS', {'nodes': 3, 'edges': 100, 'transactions': 100}):
            self.assert_rejected_without_switch(multipart(self.upload), status=413)

    def test_busy_dataset_switch_rejects_both_mutations_and_preserves_snapshot(self):
        before = self.snapshot()
        lock = self.app.state.dataset_lock
        self.client.portal.call(lock.acquire)
        try:
            upload = self.client.post('/api/datasets', files=multipart(self.upload))
            reset = self.client.post('/api/datasets/reset')
            self.assertEqual(upload.status_code, 409, upload.text)
            self.assertEqual(reset.status_code, 409, reset.text)
            self.assertEqual(self.snapshot(), before)
        finally:
            self.client.portal.call(lock.release)

    def test_shared_provider_stays_open_until_app_shutdown(self):
        class FakeProvider:
            close_count = 0

            async def aclose(self):
                self.close_count += 1

            async def analyst(self, *_args):
                raise AssertionError('Dataset switching must not call an AI provider')

            async def reviewer(self, *_args):
                raise AssertionError('Dataset switching must not call an AI provider')

        provider = FakeProvider()
        app = create_app(self.original, ai_settings=self.settings, ai_providers=provider,
                         dataset_store_dir=self.root / 'shared-provider-store')
        with TestClient(app) as client:
            response = client.post('/api/datasets', files=multipart(self.upload))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(provider.close_count, 0)
            self.assertIs(app.state.ai.providers, provider)
            response = client.post('/api/datasets/reset')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(provider.close_count, 0)
            self.assertIs(app.state.ai.providers, provider)
        self.assertEqual(provider.close_count, 1)

    def test_old_ai_cleanup_failure_keeps_successful_import_active_and_persisted(self):
        previous_ai = self.app.state.ai
        with (patch.object(previous_ai, 'aclose', side_effect=RuntimeError('cleanup failed')) as close,
              self.assertLogs('backend.app', level='WARNING')):
            response = self.client.post('/api/datasets', files=multipart(self.upload))
        close.assert_awaited_once()
        self.assertEqual(response.status_code, 200, response.text)
        metadata = response.json()
        self.assertFalse(metadata['is_default'])
        self.assertEqual(self.client.get('/api/datasets/current').json(), metadata)
        self.assertIsNot(self.app.state.ai, previous_ai)
        self.assertEqual(self.client.get('/api/analysis').json()['total_kzt'], 37000.0)
        expected_csv = self.client.get('/api/exports/nodes_roles.csv').content
        reloaded_app = create_app(self.original, ai_settings=self.settings, dataset_store_dir=self.store)
        with TestClient(reloaded_app) as reloaded:
            self.assertEqual(reloaded.get('/api/datasets/current').json()['id'], metadata['id'])
            self.assertEqual(reloaded.get('/api/exports/nodes_roles.csv').content, expected_csv)
            self.assertEqual(reloaded.get(f'/api/nodes/{self.ids[0]}').json()['out_kzt'], 25000.0)

    def test_reset_restores_original_counts_cards_and_exports(self):
        original = self.snapshot()
        self.assertEqual(self.client.post('/api/datasets', files=multipart(self.upload)).status_code, 200)
        response = self.client.post('/api/datasets/reset')
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.snapshot()
        self.assertTrue(restored['metadata']['is_default'])
        self.assertEqual(restored['analysis'], original['analysis'])
        self.assertEqual(restored['summary'], original['summary'])
        self.assertEqual(restored['csv'], original['csv'])
        self.assertEqual(self.client.get(f'/api/nodes/{self.ids[0]}').status_code, 404)
        self.assertEqual(self.client.get(f'/api/nodes/{BASE_ID}').status_code, 200)

    def test_explicit_store_restores_uploaded_dataset_in_a_new_app(self):
        response = self.client.post('/api/datasets', files=multipart(self.upload))
        self.assertEqual(response.status_code, 200, response.text)
        uploaded = self.snapshot()
        new_app = create_app(self.original, ai_settings=self.settings, dataset_store_dir=self.store)
        with TestClient(new_app) as reloaded:
            metadata = reloaded.get('/api/datasets/current').json()
            self.assertEqual(metadata['id'], uploaded['metadata']['id'])
            self.assertFalse(metadata['is_default'])
            self.assertEqual(reloaded.get('/api/analysis').json(), uploaded['analysis'])
            self.assertEqual(reloaded.get('/api/exports/nodes_roles.csv').content, uploaded['csv']['nodes_roles'])

    def test_same_gid_new_flows_replaces_ai_snapshot_and_discards_old_jobs(self):
        old_analysis, old_ai = self.app.state.analysis, self.app.state.ai
        old_job = self.client.post('/api/ai/analyses', json={'gid': str(BASE_ID)}).json()
        for _ in range(10):
            if self.client.get('/api/ai/analyses/' + old_job['job_id']).json()['status'] == 'completed':
                break
        else:
            self.fail('Offline fallback job did not finish')
        old_ai.cache['previous-dataset-sentinel'] = (time.monotonic(), {})
        ids = write_uploaded_dataset(self.upload, offset=0, first_amount=45000.0)
        response = self.client.post('/api/datasets', files=multipart(self.upload))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNot(self.app.state.analysis, old_analysis)
        self.assertIsNot(self.app.state.ai, old_ai)
        self.assertIs(self.app.state.ai.analysis, self.app.state.analysis)
        self.assertFalse(self.app.state.ai.cache)
        self.assertFalse(self.app.state.ai.jobs)
        self.assertEqual(self.client.get('/api/ai/analyses/' + old_job['job_id']).status_code, 404)
        card = self.client.get(f'/api/nodes/{ids[0]}').json()
        self.assertEqual(card['out_kzt'], 45000.0)
        formerly_isolated = self.client.get(f'/api/nodes/{ids[2]}').json()
        self.assertFalse(formerly_isolated['is_isolated'])
        self.assertFalse(formerly_isolated['is_seed'])
        self.assertEqual(formerly_isolated['in_kzt'], 12000.0)


if __name__ == '__main__':
    unittest.main()
