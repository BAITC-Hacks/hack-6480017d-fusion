"""Small, deterministic packets from the existing analysis, without API access."""
from __future__ import annotations

import hashlib
import json

from backend.analysis_config import RULES_VERSION
from backend.analysis_service import AnalysisService
from backend.ai_models import validate_analyst

MAX_EDGES = 8

# Exact source fields or implementation references, never inferred attributes.
FACT_FIELDS = (
    ('gid', 'Идентификатор участника', '', 'nodes.parquet:gid'),
    ('depth', 'Глубина наблюдения', 'колено', 'nodes.parquet:depth'),
    ('is_seed', 'Исходный узел seed', '', 'nodes.parquet:is_seed'),
    ('role', 'Рассчитанная гипотеза о роли', '', 'backend/roles.py:classify_node'),
    ('role_score', 'Эвристическая поддержка правила роли', '0–1', 'backend/roles.py:classify_node'),
    ('priority_score', 'Приоритет проверки', '0–1', 'backend/roles.py:assign_roles_and_priority'),
    ('cluster_id', 'Номер сообщества', '', 'backend/pipeline.py:calculate'),
    ('rule_id', 'Применённое правило роли', '', 'backend/roles.py:classify_node'),
    ('evidence', 'Обоснование детерминированного правила', '', 'backend/roles.py:classify_node'),
    ('in_deg', 'Наблюдаемых плательщиков', 'узлов', 'backend/features.py:compute_features'),
    ('out_deg', 'Наблюдаемых получателей', 'узлов', 'backend/features.py:compute_features'),
    ('counterparties', 'Уникальных контрагентов без самого узла', 'узлов', 'backend/features.py:compute_features'),
    ('in_kzt', 'Сумма входящих в выгрузке', 'KZT', 'edges.parquet:sum_kzt; сумма по dst'),
    ('out_kzt', 'Сумма исходящих в выгрузке', 'KZT', 'edges.parquet:sum_kzt; сумма по src'),
    ('in_tx', 'Число входящих переводов в выгрузке', 'переводов', 'edges.parquet:n_tx; сумма по dst'),
    ('out_tx', 'Число исходящих переводов в выгрузке', 'переводов', 'edges.parquet:n_tx; сумма по src'),
    ('pagerank', 'Взвешенный PageRank', '', 'backend/features.py:compute_features'),
    ('pass_through', 'Наблюдаемое отношение out/in', '', 'backend/features.py:compute_features'),
    ('ratio_usable', 'Отношение out/in допускается правилом', '', 'backend/features.py:compute_features'),
    ('seed_reach', 'Число seed, от которых достижим узел за ≤4 перехода', 'узлов', 'backend/features.py:compute_features'),
    ('direct_seed_senders', 'Непосредственных отправителей seed', 'узлов', 'backend/features.py:compute_features'),
    ('is_isolated', 'Изолирован в выгрузке', '', 'backend/features.py:compute_features'),
    ('has_self_loop', 'Есть перевод самому себе', '', 'backend/features.py:compute_features'),
    ('truncated_by_depth', 'Граница наблюдения depth=4: исходящие за ней неизвестны', '', 'backend/features.py:compute_features'),
)


def build_evidence(service: AnalysisService, gid: str) -> dict:
    node = service.node(gid)
    fingerprint = hashlib.sha256(json.dumps({
        'input_hashes': service.input_hashes, 'rules_version': RULES_VERSION,
    }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    incident = [edge for edge in service.edges if gid in (edge['src'], edge['dst'])]
    incident.sort(key=lambda edge: (-edge['sum_kzt'], int(edge['src']), int(edge['dst'])))
    displayed = incident[:MAX_EDGES]

    def counterparties(edges: list[dict]) -> set[str]:
        return {endpoint for edge in edges for endpoint in (edge['src'], edge['dst']) if endpoint != gid}

    caveats = [
        {'evidence_id': f'caveat_{index}', 'text': text, 'source': 'backend/analysis_service.py:caveats_for; docs/dataset.md'}
        for index, text in enumerate(node['caveats'], start=1)
    ]
    if len(displayed) < len(incident):
        caveats.append({'evidence_id': 'caveat_packet_truncated',
                        'text': 'В AI-пакет включены только наиболее крупные связи; список соседей сокращён и не описывает всё окружение узла.',
                        'source': 'backend/ai_evidence.py:build_evidence'})

    return {
        'version': '2', 'gid': gid, 'data_fingerprint': fingerprint,
        'period': dict(service.summary['period']), 'role': node['role'],
        'facts': [
            {'evidence_id': 'f_' + name, 'label': label, 'value': node[name], 'unit': unit, 'source': source}
            for name, label, unit, source in FACT_FIELDS
        ],
        'neighbors': [
            {'evidence_id': f'edge_{index}', **edge, 'source': 'edges.parquet:src,dst,sum_kzt,n_tx'}
            for index, edge in enumerate(displayed, start=1)
        ],
        'total_neighbors': len(counterparties(incident)),
        'returned_neighbors': len(counterparties(displayed)),
        'truncated': len(displayed) < len(incident),
        'caveats': caveats,
    }


def local_fallback(packet: dict) -> dict:
    facts = {fact['evidence_id']: fact['value'] for fact in packet['facts']}
    report = {
        'hypothesis': f"По локальному правилу предложена роль {packet['role']}. Это гипотеза для проверки аналитиком.",
        'claims': [{
            'claim_id': 'c1', 'text': facts['f_evidence'],
            'evidence_ids': ['f_evidence', 'f_rule_id', 'f_role'],
        }],
        'limitations': [item['text'] for item in packet['caveats']],
        'next_checks': [
            'Проверить полный входящий и исходящий поток за сопоставимый период, включая переводы вне этой выборки.',
            'Сопоставить структурную гипотезу с документами и назначением операций до принятия решения.',
        ],
    }
    return validate_analyst(report, packet)
