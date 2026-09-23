import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.ai_config import AISettings
from backend.ai_evidence import local_fallback
from backend.ai_providers import ProviderError, ProviderReply
from backend.ai_service import AIService
from backend.analysis_service import AnalysisService
from backend.app import create_app
from backend.audit import audit_data
from backend.tests.test_api import write_small_dataset

GID = str(2**53 + 1)
OTHER_GID = str(2**53 + 2)
ISOLATED_GID = str(2**53 + 3)
SETTINGS = AISettings(openai_api_key='test-openai-secret', nvidia_api_key='test-nvidia-secret', nvidia_enabled=True)


class FakeProviders:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.invalid = None
        self.wait = None
        self.closed = False

    async def analyst(self, packet):
        self.calls.append('openai')
        if self.wait:
            await self.wait.wait()
        if self.fail == 'openai':
            raise ProviderError('timeout', 'Таймаут тестового провайдера')
        report = local_fallback(packet)
        if self.invalid == 'openai':
            report['claims'][0]['evidence_ids'] = ['invented_fact']
        return ProviderReply(report, 'test-analyst', 10, {'input_tokens': 5, 'output_tokens': 5, 'total_tokens': 10})

    async def reviewer(self, packet, report):
        self.calls.append('nvidia')
        if self.fail == 'nvidia':
            raise ProviderError('rate_limit', 'Тестовый лимит')
        review = {'issues': [], 'alternative_explanations': [], 'missing_information': []}
        if self.invalid == 'nvidia':
            review['issues'] = [{'claim_id': 'c6', 'kind': 'unsupported_claim',
                                 'reason': 'Неверная ссылка', 'evidence_ids': ['f_gid']}]
        return ProviderReply(review, 'test-reviewer', 20, {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12})

    async def aclose(self):
        self.closed = True


class AIServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        write_small_dataset(Path(self.directory.name))
        self.analysis = AnalysisService(Path(self.directory.name), audit_data(Path(self.directory.name)))
        self.providers = FakeProviders()
        self.service = AIService(self.analysis, SETTINGS, self.providers)

    async def asyncTearDown(self):
        await self.service.aclose()
        self.directory.cleanup()

    async def complete(self, gid=GID, refresh=False):
        job = self.service.start(gid, refresh)
        await self.service.tasks[job['job_id']]
        return self.service.get(job['job_id'])

    async def test_success_cache_refresh_and_exports_unchanged(self):
        before = copy.deepcopy(self.analysis.exports)
        nodes_before = copy.deepcopy(self.analysis.nodes)
        job = await self.complete()
        self.assertEqual(self.providers.calls, ['openai', 'nvidia'])
        self.assertEqual([job[p]['status'] for p in ('openai', 'nvidia')], ['success', 'success'])
        self.assertEqual(job['status'], 'completed')
        self.assertIsNone(job['fallback'])
        cached = await self.complete()
        self.assertEqual([cached[p]['status'] for p in ('openai', 'nvidia')], ['cached', 'cached'])
        self.assertTrue(cached['openai']['cached_at'])
        self.assertEqual(len(self.providers.calls), 2)
        await self.complete(refresh=True)
        self.assertEqual(self.providers.calls, ['openai', 'nvidia'] * 2)
        self.assertEqual(self.analysis.exports, before)
        self.assertEqual(self.analysis.nodes, nodes_before)

    async def test_analyst_failure_skips_reviewer_and_returns_local_facts(self):
        self.providers.fail = 'openai'
        job = await self.complete()
        self.assertEqual(self.providers.calls, ['openai'])
        self.assertEqual(job['openai']['error']['code'], 'timeout')
        self.assertEqual(job['nvidia']['status'], 'skipped')
        self.assertIsNotNone(job['fallback'])
        self.assertFalse(self.service.cache)

    async def test_bad_analyst_refs_rejected_but_known_usage_preserved(self):
        self.providers.invalid = 'openai'
        job = await self.complete()
        self.assertEqual(job['openai']['error']['code'], 'invalid_response')
        self.assertEqual(job['openai']['usage']['total_tokens'], 10)
        self.assertEqual(self.providers.calls, ['openai'])
        self.assertIsNone(job['openai']['result'])

    async def test_reviewer_failure_preserves_analyst_then_retries_only_reviewer(self):
        self.providers.fail = 'nvidia'
        job = await self.complete()
        self.assertEqual(job['openai']['status'], 'success')
        self.assertEqual(job['nvidia']['status'], 'error')
        self.assertIsNone(job['fallback'])
        self.providers.fail = None
        second = await self.complete()
        self.assertEqual(self.providers.calls, ['openai', 'nvidia', 'nvidia'])
        self.assertEqual(second['openai']['status'], 'cached')
        self.assertEqual(second['nvidia']['status'], 'success')

    async def test_reviewer_invalid_claim_reference_rejected(self):
        self.providers.invalid = 'nvidia'
        job = await self.complete()
        self.assertEqual(job['nvidia']['error']['code'], 'invalid_response')
        self.assertIsNone(job['nvidia']['result'])
        self.assertEqual(job['openai']['status'], 'success')

    async def test_missing_keys_make_no_paid_requests(self):
        self.service.settings = AISettings()
        job = await self.complete(ISOLATED_GID)
        self.assertEqual(self.providers.calls, [])
        self.assertEqual(job['openai']['error']['code'], 'missing_key')
        self.assertIsNotNone(job['fallback'])
        self.assertIn('нет связей', json.dumps(job['fallback'], ensure_ascii=False))

    async def test_disabled_reviewer_makes_only_one_request_without_warning(self):
        self.service.settings = AISettings(openai_api_key='test-openai-secret', nvidia_api_key='still-present')
        job = await self.complete()
        self.assertEqual(self.providers.calls, ['openai'])
        self.assertEqual(job['openai']['status'], 'success')
        self.assertEqual(job['nvidia']['status'], 'skipped')
        self.assertFalse(job['reviewer_enabled'])
        self.assertIsNone(job['warning'])
        self.assertIsNone(job['nvidia']['error'])
        self.assertIsNone(job['fallback'])

    async def test_duplicate_click_joins_job_and_concurrency_is_bounded(self):
        self.providers.wait = asyncio.Event()
        first = self.service.start(GID)
        duplicate = self.service.start(GID, refresh=True)
        self.assertEqual(first['job_id'], duplicate['job_id'])
        self.service.start(OTHER_GID)
        with self.assertRaises(RuntimeError):
            self.service.start(ISOLATED_GID)
        self.providers.wait.set()
        await asyncio.gather(*list(self.service.tasks.values()))
        self.assertEqual(self.providers.calls.count('openai'), 2)
        self.assertEqual(self.providers.calls.count('nvidia'), 2)

    async def test_cache_depends_on_input_facts(self):
        await self.complete()
        self.analysis.nodes[GID]['priority_score'] = 0.1234
        job = await self.complete()
        self.assertEqual(job['openai']['status'], 'success')
        self.assertEqual(len(self.providers.calls), 4)

    async def test_cancellation_finishes_stages_and_releases_slots(self):
        self.providers.wait = asyncio.Event()
        job = self.service.start(GID)
        await asyncio.sleep(0)
        await self.service.aclose()
        result = self.service.get(job['job_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['openai']['error']['code'], 'cancelled')
        self.assertEqual(result['nvidia']['status'], 'skipped')
        self.assertFalse(self.service.active)
        self.assertFalse(self.service.tasks)

    async def test_public_status_and_snapshots_do_not_expose_or_mutate_state(self):
        config = self.service.configuration()
        self.assertNotIn('secret', json.dumps(config))
        job = await self.complete()
        job['openai']['result']['hypothesis'] = 'changed outside'
        self.assertNotEqual(self.service.get(job['job_id'])['openai']['result']['hypothesis'], 'changed outside')


class AIEndpointTests(unittest.TestCase):
    def test_request_validation_missing_keys_and_safe_status(self):
        with tempfile.TemporaryDirectory() as directory:
            write_small_dataset(Path(directory))
            with TestClient(create_app(Path(directory), ai_settings=AISettings())) as client:
                self.assertEqual(client.get('/api/ai/status').json()['openai']['configured'], False)
                self.assertEqual(client.post('/api/ai/analyses', json={'gid': int(GID)}).status_code, 422)
                self.assertEqual(client.post('/api/ai/analyses', json={'gid': '999'}).status_code, 404)
                self.assertEqual(client.get('/api/ai/analyses/missing').status_code, 404)
                response = client.post('/api/ai/analyses', json={'gid': GID})
                self.assertEqual(response.status_code, 202)
                job_id = response.json()['job_id']
                job = client.get(f'/api/ai/analyses/{job_id}').json()
                self.assertEqual(job['status'], 'completed')
                self.assertEqual(job['nvidia']['status'], 'skipped')
                self.assertIsNotNone(job['fallback'])


if __name__ == '__main__':
    unittest.main()
