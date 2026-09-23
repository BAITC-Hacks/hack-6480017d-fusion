"""Provider contracts exercised with fake transport only; never reads real .env."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from backend.ai_config import AISettings, NVIDIA_BASE_URL, load_settings
from backend.ai_models import AnalystReport
from backend.ai_providers import AIProviders, ProviderError, MAX_RESPONSE_BYTES


ANALYST = {'hypothesis': 'Структурная гипотеза', 'claims': [
    {'claim_id': 'c1', 'text': 'Есть входящие связи', 'evidence_ids': ['f1']}],
    'limitations': ['Выборка неполная'], 'next_checks': ['Проверить полный период']}
REVIEW = {'issues': [], 'alternative_explanations': [], 'missing_information': []}
PACKET = {'gid': '9100000000000000001', 'facts': [{'evidence_id': 'f1', 'value': 4}]}


def openai_body(payload: dict = ANALYST) -> dict:
    return {'status': 'completed', 'model': 'gpt-5.4-mini',
            'output': [{'type': 'message', 'status': 'completed', 'content': [
                {'type': 'output_text', 'text': json.dumps(payload)}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 200, 'total_tokens': 300}}


def nvidia_body(payload: dict = REVIEW) -> dict:
    return {'model': 'nvidia/nemotron-nano-3-30b-a3b', 'choices': [
        {'finish_reason': 'stop', 'message': {'role': 'assistant',
         'content': json.dumps(payload), 'reasoning_content': 'Do not expose reasoning'}}],
        'usage': {'prompt_tokens': 500, 'completion_tokens': 100, 'total_tokens': 600}}


class SettingsTests(unittest.TestCase):
    def test_env_precedence_and_secrets_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.env'
            path.write_text('OPENAI_API_KEY=not-real-file-key\nNVIDIA_API_KEY=not-real-nvidia\n'
                            'OPENAI_MODEL=from-file\n', encoding='utf-8')
            config = load_settings(path, {'OPENAI_API_KEY': 'not-real-override',
                                          'NVIDIA_API_KEY': ''})
        self.assertEqual(config.openai_api_key, 'not-real-override')
        self.assertEqual(config.openai_model, 'from-file')
        self.assertTrue(config.openai_configured)
        self.assertFalse(config.nvidia_configured)
        self.assertFalse(config.nvidia_enabled)
        self.assertNotIn('not-real', repr(config))

    def test_empty_configuration_does_not_read_project_file(self):
        config = load_settings(None, {})
        self.assertFalse(config.openai_configured)
        self.assertFalse(config.nvidia_configured)
        self.assertEqual(config.nvidia_base_url, NVIDIA_BASE_URL)
        self.assertEqual(config.openai_model, 'gpt-5.4-mini')
        self.assertEqual(config.nvidia_model, 'nvidia/nemotron-nano-3-30b-a3b')
        self.assertEqual(config.timeout_seconds, 20)

    def test_invalid_timeout_rejected(self):
        for timeout in (0, -1, 31, float('inf'), float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                AISettings(timeout_seconds=timeout)

    def test_nvidia_requires_explicit_opt_in_separate_from_key_presence(self):
        default = load_settings(None, {'NVIDIA_API_KEY': 'not-real-key'})
        self.assertTrue(default.nvidia_configured)
        self.assertFalse(default.nvidia_enabled)
        for value in ('true', 'TRUE', ' true '):
            with self.subTest(value=value):
                config = load_settings(None, {'NVIDIA_ENABLED': value})
                self.assertTrue(config.nvidia_enabled)
                self.assertFalse(config.nvidia_configured)
        for value in ('false', '', '1', 'yes'):
            with self.subTest(value=value):
                self.assertFalse(load_settings(None, {'NVIDIA_ENABLED': value}).nvidia_enabled)

    def test_only_retired_nvidia_model_is_migrated_without_editing_env(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.env'
            original = 'NVIDIA_MODEL=nvidia/nemotron-3-nano-30b-a3b\nNVIDIA_API_KEY=not-real\n'
            path.write_text(original, encoding='utf-8')
            config = load_settings(path, {})
            self.assertEqual(config.nvidia_model, 'nvidia/nemotron-nano-3-30b-a3b')
            self.assertEqual(path.read_text(encoding='utf-8'), original)
        other = load_settings(None, {'NVIDIA_MODEL': 'nvidia/nemotron-3.5-lightning-30b-a3b'})
        self.assertEqual(other.nvidia_model, 'nvidia/nemotron-3.5-lightning-30b-a3b')


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **kwargs) -> AISettings:
        return AISettings(openai_api_key='not-real-openai', nvidia_api_key='not-real-nvidia', **kwargs)

    async def call(self, response: httpx.Response, *, nvidia=False):
        calls = []

        async def handle(request):
            calls.append(request)
            return response

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            providers = AIProviders(self.settings(), client)
            try:
                result = await (providers.reviewer(PACKET, ANALYST) if nvidia else providers.analyst(PACKET))
            finally:
                self.assertEqual(len(calls), 1)
            return result, calls[0]

    async def test_openai_schema_store_flag_exact_gid_actual_model_usage(self):
        result, request = await self.call(httpx.Response(200, json=openai_body()))
        data = json.loads(request.content)
        self.assertEqual(str(request.url), 'https://api.openai.com/v1/responses')
        self.assertEqual(request.headers['authorization'], 'Bearer not-real-openai')
        self.assertFalse(data['store'])
        self.assertEqual(data['model'], 'gpt-5.4-mini')
        self.assertEqual(data['reasoning'], {'effort': 'low'})
        self.assertEqual(data['max_output_tokens'], 3000)
        self.assertEqual(data['text']['format']['type'], 'json_schema')
        self.assertTrue(data['text']['format']['strict'])
        self.assertFalse(data['text']['format']['schema']['additionalProperties'])
        citation_schema = data['text']['format']['schema']['$defs']['AnalystClaim']['properties']['evidence_ids']['items']
        self.assertEqual(citation_schema['enum'], ['f1'])
        static_citation_schema = AnalystReport.model_json_schema()['$defs']['AnalystClaim']['properties']['evidence_ids']['items']
        self.assertNotIn('enum', static_citation_schema)
        self.assertEqual(json.loads(data['input'][1]['content'])['evidence_packet']['gid'], PACKET['gid'])
        self.assertEqual(result.payload, ANALYST)
        self.assertEqual(result.model, 'gpt-5.4-mini')
        self.assertEqual(result.usage, {'input_tokens': 100, 'output_tokens': 200, 'total_tokens': 300})
        self.assertGreaterEqual(result.elapsed_ms, 0)

    async def test_nvidia_json_schema_prompt_nonstreaming_reasoning_ignored(self):
        result, request = await self.call(httpx.Response(200, json=nvidia_body()), nvidia=True)
        data = json.loads(request.content)
        self.assertEqual(str(request.url), NVIDIA_BASE_URL + '/chat/completions')
        self.assertFalse(data['stream'])
        self.assertEqual(data['chat_template_kwargs'], {'enable_thinking': False})
        self.assertNotIn('response_format', data)
        self.assertIn('JSON Schema:', data['messages'][0]['content'])
        self.assertEqual(json.loads(data['messages'][1]['content'])['analyst_report'], ANALYST)
        self.assertEqual(result.payload, REVIEW)
        self.assertNotIn('reasoning_content', result.payload)
        self.assertEqual(result.usage, {'input_tokens': 500, 'output_tokens': 100, 'total_tokens': 600})

    async def test_reasoning_parameter_only_for_supported_model_family(self):
        for model, reasoning_expected in (('gpt-4.1-mini', False), ('gpt-5.4-mini-2026-03-17', True)):
            with self.subTest(model=model):
                requests = []

                async def handle(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json=openai_body())

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    provider = AIProviders(self.settings(openai_model=model), client)
                    await provider.analyst(PACKET)
                self.assertEqual(len(requests), 1)
                self.assertEqual('reasoning' in requests[0], reasoning_expected)

    async def test_errors_never_retry_or_expose_raw_body(self):
        for status, code in ((401, 'auth'), (403, 'auth'), (429, 'rate_limit'),
                             (500, 'provider_error'), (404, 'model_unavailable'), (410, 'model_unavailable'),
                             (202, 'provider_error'), (302, 'provider_error')):
            for nvidia in (False, True):
                with self.subTest(status=status, nvidia=nvidia):
                    with self.assertRaises(ProviderError) as caught:
                        await self.call(httpx.Response(status, text='secret-echo-not-real-openai'), nvidia=nvidia)
                    self.assertEqual(caught.exception.code, code)
                    self.assertNotIn('secret-echo', str(caught.exception))

    async def test_missing_keys_and_untrusted_endpoint_make_zero_requests(self):
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(500)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            empty = AIProviders(AISettings(), client)
            with self.assertRaises(ProviderError) as caught:
                await empty.analyst(PACKET)
            self.assertEqual(caught.exception.code, 'missing_key')
            with self.assertRaises(ProviderError) as caught:
                await empty.reviewer(PACKET, ANALYST)
            self.assertEqual(caught.exception.code, 'missing_key')
            bad = AIProviders(self.settings(nvidia_base_url='https://example.com/v1'), client)
            with self.assertRaises(ProviderError) as caught:
                await bad.reviewer(PACKET, ANALYST)
            self.assertEqual(caught.exception.code, 'provider_error')
        self.assertEqual(calls, [])

    async def test_timeout_and_connection_errors_are_sanitized(self):
        for error, code in ((httpx.ReadTimeout('not-real-openai leaked'), 'timeout'),
                            (httpx.ConnectError('not-real-openai leaked'), 'provider_error')):
            calls = []

            async def handle(request):
                calls.append(request)
                raise error

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                provider = AIProviders(self.settings(), client)
                with self.assertRaises(ProviderError) as caught:
                    await provider.analyst(PACKET)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('not-real-openai', str(caught.exception))
            self.assertEqual(len(calls), 1)

    async def test_overall_timeout_even_if_transport_does_not_enforce_it(self):
        calls = []

        async def handle(request):
            calls.append(request)
            await asyncio.sleep(1)
            return httpx.Response(200, json=openai_body())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            provider = AIProviders(self.settings(timeout_seconds=0.01), client)
            with self.assertRaises(ProviderError) as caught:
                await provider.analyst(PACKET)
            self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(len(calls), 1)

    async def test_openai_refusal_incomplete_and_missing_text(self):
        bodies = [({'status': 'incomplete'}, 'incomplete'),
                  ({'status': 'completed', 'output': [{'type': 'message', 'content': [
                      {'type': 'refusal', 'refusal': 'sensitive refusal text'}]}]}, 'refusal'),
                  ({'status': 'completed', 'output': [{'type': 'reasoning', 'summary': []}]}, 'invalid_response'),
                  ({'status': 'completed', 'output': 'wrong'}, 'invalid_response')]
        for body, code in bodies:
            with self.subTest(code=code), self.assertRaises(ProviderError) as caught:
                await self.call(httpx.Response(200, json=body))
            self.assertEqual(caught.exception.code, code)

    async def test_nvidia_refusal_truncation_and_reasoning_without_content(self):
        for finish, message, code in (
            ('length', {'content': '{}'}, 'incomplete'),
            ('content_filter', {'content': ''}, 'refusal'),
            ('stop', {'content': '{}', 'refusal': 'sensitive details'}, 'refusal'),
            ('stop', {'reasoning_content': '{"not": "the final answer"}'}, 'invalid_response'),
        ):
            body = {'model': 'test-model', 'choices': [{'finish_reason': finish, 'message': message}]}
            with self.subTest(finish=finish), self.assertRaises(ProviderError) as caught:
                await self.call(httpx.Response(200, json=body), nvidia=True)
            self.assertEqual(caught.exception.code, code)

    async def test_json_invalid_nonfinite_fences_and_oversize_are_rejected(self):
        for text in ('not-json', '[]', '{"x":NaN}', '```json\n{}\n```'):
            body = openai_body()
            body['output'][0]['content'][0]['text'] = text
            with self.subTest(text=text), self.assertRaises(ProviderError) as caught:
                await self.call(httpx.Response(200, json=body))
            self.assertEqual(caught.exception.code, 'invalid_response')
        with self.assertRaises(ProviderError) as caught:
            await self.call(httpx.Response(200, content=b' ' * (MAX_RESPONSE_BYTES + 1)))
        self.assertEqual(caught.exception.code, 'invalid_response')

    async def test_usage_missing_is_unknown_not_zero(self):
        body = openai_body()
        body.pop('usage')
        result, _ = await self.call(httpx.Response(200, json=body))
        self.assertEqual(result.usage, {'input_tokens': None, 'output_tokens': None, 'total_tokens': None})

    async def test_injected_client_lifetime_remains_with_caller(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
            provider = AIProviders(self.settings(), client)
            await provider.aclose()
            self.assertFalse(client.is_closed)


if __name__ == '__main__':
    unittest.main()
