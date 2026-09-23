"""Two-stage AI jobs. Computed roles and scores are never modified here."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from backend.ai_config import AISettings
from backend.ai_evidence import build_evidence, local_fallback
from backend.ai_models import validate_analyst, validate_reviewer
from backend.ai_providers import AIProviders, PROMPT_FINGERPRINT, ProviderError
from backend.analysis_service import AnalysisService

POLICY_VERSION = 'fusion-ai-jobs-v2-single-default'
CACHE_TTL_SECONDS = 3600
MAX_CACHED_RESULTS = 256
MAX_JOBS = 128


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def stage() -> dict:
    return {'status': 'pending', 'model': None, 'elapsed_ms': None, 'usage': None,
            'error': None, 'result': None, 'cached_at': None}


class AIService:
    def __init__(self, analysis: AnalysisService, settings: AISettings,
                 providers: AIProviders | None = None, *, max_running: int = 2):
        self.analysis = analysis
        self.settings = settings
        self.providers = providers or AIProviders(settings)
        self.max_running = max_running
        self.jobs: OrderedDict[str, dict] = OrderedDict()
        self.tasks: dict[str, asyncio.Task] = {}
        self.active: dict[str, str] = {}
        self.cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def configuration(self) -> dict:
        return {
            'openai': {'configured': self.settings.openai_configured, 'model': self.settings.openai_model},
            'nvidia': {'configured': self.settings.nvidia_configured, 'model': self.settings.nvidia_model},
            'reviewer_enabled': self.settings.nvidia_enabled,
            'timeout_seconds': self.settings.timeout_seconds,
        }

    def get(self, job_id: str) -> dict:
        return copy.deepcopy(self.jobs[job_id])

    def _cached(self, key: str) -> dict | None:
        item = self.cache.get(key)
        if item is None:
            return None
        timestamp, result = item
        if time.monotonic() - timestamp > CACHE_TTL_SECONDS:
            del self.cache[key]
            return None
        self.cache.move_to_end(key)
        return {**copy.deepcopy(result), 'status': 'cached'}

    def _save_cache(self, key: str, result: dict) -> None:
        saved = {**copy.deepcopy(result), 'cached_at': now_iso()}
        self.cache[key] = (time.monotonic(), saved)
        self.cache.move_to_end(key)
        while len(self.cache) > MAX_CACHED_RESULTS:
            self.cache.popitem(last=False)

    def start(self, gid: str, refresh: bool = False) -> dict:
        packet = build_evidence(self.analysis, gid)
        base_key = fingerprint({'packet': packet, 'models': [self.settings.openai_model, self.settings.nvidia_model],
                                'nvidia_url': self.settings.nvidia_base_url,
                                'reviewer_enabled': self.settings.nvidia_enabled,
                                'prompts': PROMPT_FINGERPRINT, 'policy': POLICY_VERSION})
        # A double click, even with refresh, always joins the same running job.
        if base_key in self.active:
            return self.get(self.active[base_key])
        if len(self.active) >= self.max_running:
            raise RuntimeError('Уже выполняются два разбора. Дождитесь их завершения.')
        job_id = uuid.uuid4().hex
        job = {'job_id': job_id, 'gid': gid, 'status': 'running', 'created_at': now_iso(),
               'completed_at': None, 'evidence_packet': packet, 'openai': stage(),
               'nvidia': stage(), 'fallback': None, 'warning': None,
               'reviewer_enabled': self.settings.nvidia_enabled}
        self.jobs[job_id] = job
        self.active[base_key] = job_id
        self.tasks[job_id] = asyncio.create_task(self._run(job, base_key, refresh))
        for old_id in list(self.jobs):
            if len(self.jobs) <= MAX_JOBS:
                break
            if self.jobs[old_id]['status'] == 'completed':
                del self.jobs[old_id]
        return self.get(job_id)

    @staticmethod
    def _error(target: dict, code: str, message: str) -> None:
        target.update(status='error', error={'code': code, 'message': message}, result=None)

    def _fallback(self, job: dict) -> None:
        job['fallback'] = local_fallback(job['evidence_packet'])
        job['nvidia'].update(status='skipped', error={
            'code': 'no_valid_analysis', 'message': 'Нет валидной справки OpenAI; NVIDIA не вызывалась.'})
        job['warning'] = 'AI-разбор не завершён. Показана локальная справка по рассчитанным фактам.'

    async def _call(self, job: dict, provider: str, cache_key: str, refresh: bool) -> bool:
        cached = None if refresh else self._cached(cache_key)
        if cached is not None:
            job[provider] = cached
            return True
        target = job[provider]
        configured = getattr(self.settings, f'{provider}_configured')
        if not configured:
            self._error(target, 'missing_key', f'Не настроены ключ или модель {"OpenAI" if provider == "openai" else "NVIDIA"}. Заполните backend/.env и перезапустите сервер.')
            return False
        target.update(status='running', model=getattr(self.settings, f'{provider}_model'))
        started = time.monotonic()
        try:
            packet = job['evidence_packet']
            if provider == 'openai':
                reply = await self.providers.analyst(packet)
            else:
                reply = await self.providers.reviewer(packet, job['openai']['result'])
            target.update(model=reply.model, elapsed_ms=reply.elapsed_ms, usage=reply.usage)
            result = (validate_analyst(reply.payload, packet) if provider == 'openai'
                      else validate_reviewer(reply.payload, packet, job['openai']['result']))
            target.update(status='success', result=result, model=reply.model,
                          elapsed_ms=reply.elapsed_ms, usage=reply.usage)
            self._save_cache(cache_key, target)
            return True
        except ProviderError as error:
            self._error(target, error.code, error.message)
        except (ValueError, TypeError, KeyError):
            self._error(target, 'invalid_response', 'Ответ не прошёл проверку структуры, ссылок на факты или идентификаторов.')
        finally:
            if target['elapsed_ms'] is None:
                target['elapsed_ms'] = round((time.monotonic() - started) * 1000)
        return False

    async def _run(self, job: dict, base_key: str, refresh: bool) -> None:
        try:
            async with asyncio.timeout(2 * self.settings.timeout_seconds + 5):
                analyst_key = f'openai:{base_key}'
                if not await self._call(job, 'openai', analyst_key, refresh):
                    self._fallback(job)
                    return
                if not self.settings.nvidia_enabled:
                    job['nvidia']['status'] = 'skipped'
                    return
                reviewer_key = f'nvidia:{base_key}:{fingerprint(job["openai"]["result"])}'
                if not await self._call(job, 'nvidia', reviewer_key, refresh):
                    job['warning'] = 'Справка OpenAI доступна. Рецензия NVIDIA не получена; разбор неполный.'
        except TimeoutError:
            active_stage = 'nvidia' if job['openai']['status'] in ('success', 'cached') else 'openai'
            self._error(job[active_stage], 'timeout', 'Истёк общий срок ожидания AI-разбора.')
            if active_stage == 'openai':
                self._fallback(job)
            else:
                job['warning'] = 'Справка OpenAI сохранена; NVIDIA не завершила рецензию вовремя.'
        except asyncio.CancelledError:
            active_stage = 'nvidia' if job['openai']['status'] in ('success', 'cached') else 'openai'
            self._error(job[active_stage], 'cancelled', 'Разбор прерван при остановке сервера.')
            if active_stage == 'openai':
                self._fallback(job)
            else:
                job['warning'] = 'Справка OpenAI сохранена; рецензия прервана при остановке сервера.'
            raise
        except Exception:
            # Never include exception text: transport/config objects may contain secrets.
            active_stage = 'nvidia' if job['openai']['status'] in ('success', 'cached') else 'openai'
            self._error(job[active_stage], 'internal_error', 'Разбор не удалось завершить. Можно повторить запрос.')
            if active_stage == 'openai':
                self._fallback(job)
            else:
                job['warning'] = 'Справка OpenAI сохранена; рецензия NVIDIA недоступна.'
        finally:
            job.update(status='completed', completed_at=now_iso())
            self.active.pop(base_key, None)
            self.tasks.pop(job['job_id'], None)

    async def aclose(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.providers.aclose()
