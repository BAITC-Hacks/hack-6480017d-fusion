"""Run at most six real AI checks against the already running API.

Usage: .venv/bin/python -m backend.validate_ai --limit 1
No credentials are read by this script: only the backend uses its runtime keys.
OpenAI is required; NVIDIA is checked only when explicitly enabled on the server.
Successful schema/reference validation is not a factual-quality assessment.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from backend.ai_evidence import build_evidence
from backend.analysis_service import AnalysisService
from backend.audit import DEFAULT_DATA_DIR, assert_valid_audit, audit_data

API = 'http://127.0.0.1:8000'
OUTPUT = Path(__file__).resolve().parent.parent / 'artifacts' / 'ai_validation.json'
JOB_TIMEOUT_SECONDS = 60


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_samples(service: AnalysisService) -> list[dict]:
    """Stable samples from computed roles, including the two boundary cases."""
    predicates = (
        ('consolidator', lambda row: row['role'] == 'consolidator'),
        ('coordinator', lambda row: row['role'] == 'coordinator'),
        ('transit', lambda row: row['role'] == 'transit'),
        ('seed_incomplete_input', lambda row: row['is_seed'] and not row['is_isolated']),
        ('depth_4', lambda row: row['truncated_by_depth']),
        ('isolated_seed', lambda row: row['is_isolated'] and row['is_seed']),
    )
    selected = []
    seen = set()
    for category, predicate in predicates:
        gid = next((gid for gid in service.ranked_ids
                    if gid not in seen and predicate(service.nodes[gid])), None)
        if gid is None:
            raise ValueError('В данных отсутствует обязательный тип примера для проверки')
        seen.add(gid)
        selected.append({'category': category, 'gid': gid, 'role': service.nodes[gid]['role']})
    return selected


def save(report: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(OUTPUT)


def run(limit: int) -> int:
    report = {'started_at': timestamp(), 'finished_at': None, 'status': 'running',
              'requested_samples': limit, 'samples': [], 'input_hashes': {},
              'configuration': None, 'manual_quality_review': 'pending',
              'note': 'Реальные ответы API. Проверка структуры и ссылок не доказывает корректность интерпретации.'}
    save(report)
    try:
        audit = audit_data(DEFAULT_DATA_DIR)
        assert_valid_audit(audit)
        service = AnalysisService(Path(DEFAULT_DATA_DIR), audit)
        report['input_hashes'] = dict(service.input_hashes)
        samples = select_samples(service)[:limit]
        with httpx.Client(base_url=API, timeout=5, follow_redirects=False, trust_env=False,
                          transport=httpx.HTTPTransport(retries=0, trust_env=False)) as client:
            response = client.get('/api/ai/status')
            response.raise_for_status()
            configuration = response.json()
            # Only public settings are persisted. Never store request headers.
            report['configuration'] = {
                name: {'configured': configuration[name]['configured'], 'model': configuration[name]['model']}
                for name in ('openai', 'nvidia')
            }
            reviewer_enabled = configuration.get('reviewer_enabled', False)
            report['configuration']['reviewer_enabled'] = reviewer_enabled
            required = ('openai', 'nvidia') if reviewer_enabled else ('openai',)
            if not all(configuration[name]['configured'] for name in required):
                report.update(status='not_configured', finished_at=timestamp())
                save(report)
                print('NOT_CONFIGURED: обязательный AI-провайдер не настроен на сервере', flush=True)
                return 1
            for sample in samples:
                started = time.monotonic()
                entry = {**sample, 'started_at': timestamp(), 'status': 'running', 'elapsed_ms': None,
                         'job': None, 'data_match': None, 'manual_review': None}
                report['samples'].append(entry)
                save(report)
                # refresh guarantees this run checks real providers rather than old cache.
                response = client.post('/api/ai/analyses', json={'gid': sample['gid'], 'refresh': True})
                response.raise_for_status()
                job = response.json()
                entry['job'] = job
                deadline = started + JOB_TIMEOUT_SECONDS
                while job['status'] != 'completed':
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        entry['status'] = 'timeout'
                        break
                    time.sleep(min(0.5, remaining))
                    response = client.get('/api/ai/analyses/' + job['job_id'], timeout=min(5, max(0.1, remaining)))
                    response.raise_for_status()
                    job = response.json()
                    entry['job'] = job
                entry['elapsed_ms'] = round((time.monotonic() - started) * 1000)
                expected_fingerprint = build_evidence(service, sample['gid'])['data_fingerprint']
                entry['data_match'] = job['evidence_packet']['data_fingerprint'] == expected_fingerprint
                expected_reviewer_status = 'success' if reviewer_enabled else 'skipped'
                successful = (job['status'] == 'completed' and entry['data_match']
                              and job['openai']['status'] == 'success'
                              and job['nvidia']['status'] == expected_reviewer_status
                              and job['fallback'] is None)
                entry['status'] = 'success' if successful else 'failed'
                save(report)
                stages = ', '.join(f'{name}={job[name]["status"]}' +
                                   (f'/{job[name]["error"]["code"]}' if job[name].get('error') else '')
                                   for name in ('openai', 'nvidia'))
                print(f'{entry["status"].upper()} {sample["category"]} gid={sample["gid"]}: {stages}', flush=True)
                if not successful:
                    report.update(status='failed', finished_at=timestamp())
                    save(report)
                    return 1
        report.update(status='success', finished_at=timestamp())
        save(report)
        print(f'SUCCESS: {len(samples)}/{limit}; ручная оценка качества ещё требуется', flush=True)
        return 0
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
        report.update(status='failed', finished_at=timestamp(), error='Локальная проверка или API недоступны; повторов не было.')
        save(report)
        print('FAILED: локальная проверка или API недоступны; детали сохранены без секретов', flush=True)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Реальная проверка OpenAI на 1–6 узлах; NVIDIA проверяется, если включена на сервере')
    parser.add_argument('--limit', type=int, choices=range(1, 7), default=6)
    args = parser.parse_args()
    return run(args.limit)


if __name__ == '__main__':
    raise SystemExit(main())
