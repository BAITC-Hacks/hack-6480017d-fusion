"""Ordered, conservative hypotheses and transparent support/priority scores."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from backend import analysis_config as c


def classify_node(f: dict, pr_threshold: float) -> tuple[str, float, str, str]:
    """Return role, heuristic support, rule_id and <=200 character explanation."""
    incoming, outgoing = f['in_deg'], f['out_deg']
    if f['is_isolated']:
        return 'peripheral', 0.10, 'isolated', 'Связей 0; наблюдаемых переводов нет. Данных для гипотезы о роли недостаточно.'
    if f['truncated_by_depth']:
        return 'peripheral', 0.15, 'boundary', (
            f"Глубина {c.MAX_OBSERVED_DEPTH}: плательщиков {incoming}, исходящих 0. Граница выгрузки; конечный получатель не установлен.")
    caveat = ' Вход seed неполон.' if f['is_seed'] else ' Только выгрузка.'
    if (incoming >= c.MIN_COORDINATOR_SENDERS and outgoing >= c.MIN_COORDINATOR_RECEIVERS
            and f['seed_reach'] >= c.MIN_COORDINATOR_SEEDS and f['pagerank'] >= pr_threshold):
        support = 0.55 + 0.20 * min(f['seed_reach'] / 5, 1) + 0.15 * min(incoming / 10, 1)
        return 'coordinator', round(support, 6), 'coordinator', (
            f"Гипотеза связующего узла: {incoming}→{outgoing} контрагентов; достижим от {f['seed_reach']} seed; "
            f"PageRank {f['pagerank']:.4g} ≥ P{c.COORDINATOR_PR_QUANTILE * 100:g} {pr_threshold:.4g}." + caveat)
    if outgoing >= c.MIN_DISTRIBUTOR_RECEIVERS:
        support = 0.55 + 0.35 * min(outgoing / 30, 1)
        return 'distributor', round(support, 6), 'distributor', (
            f"Признаки распределения: получателей {outgoing} ≥ {c.MIN_DISTRIBUTOR_RECEIVERS}; исходящих переводов {f['out_tx']}; "
            f"сумма {f['out_kzt']:.0f} KZT." + caveat)
    if (f['ratio_usable'] and incoming > 0 and outgoing > 0
            and c.TRANSIT_RATIO_LOW <= f['pass_through'] <= c.TRANSIT_RATIO_HIGH):
        support = 0.60 + 0.25 * max(0, 1 - abs(f['pass_through'] - 1) / (c.TRANSIT_RATIO_HIGH - 1))
        return 'transit', round(support, 6), 'transit', (
            f"Гипотеза транзита: {incoming}→{outgoing} контрагентов; out/in={f['pass_through']:.3f} "
            f"в [{c.TRANSIT_RATIO_LOW:g};{c.TRANSIT_RATIO_HIGH:g}]; вход {f['in_kzt']:.0f} KZT. Время удержания не установлено.")
    if (f['ratio_usable'] and incoming >= c.MIN_CONSOLIDATOR_SENDERS
            and f['pass_through'] < c.TRANSIT_RATIO_LOW):
        support = 0.55 + 0.20 * min(incoming / 10, 1) + 0.15 * (1 - f['pass_through'] / c.TRANSIT_RATIO_LOW)
        return 'consolidator', round(support, 6), 'consolidator', (
            f"Признаки консолидации: плательщиков {incoming} ≥ {c.MIN_CONSOLIDATOR_SENDERS}; вход {f['in_kzt']:.0f} KZT; "
            f"out/in={f['pass_through']:.3f} < {c.TRANSIT_RATIO_LOW:g}. Только потоки выгрузки.")
    if (not f['is_seed'] and not f['has_self_loop'] and incoming > 0 and outgoing == 0
            and f['depth'] < c.MAX_OBSERVED_DEPTH):
        return 'terminal', 0.55, 'terminal', (
            f"Кандидат в конечные получатели: глубина {f['depth']} < {c.MAX_OBSERVED_DEPTH}; вход {f['in_kzt']:.0f} KZT; "
            'исходящих 0. Только в наблюдаемой сети, не доказательство удержания.')
    return 'peripheral', 0.20, 'no_rule', (
        f"Плательщиков {incoming}, получателей {outgoing}; ни одно специальное правило не выполнено." + caveat)


def _log_normalized(values: pd.Series) -> pd.Series:
    logged = np.log1p(values.astype(float))
    maximum = float(logged.max())
    return logged / maximum if maximum > 0 else logged * 0


def assign_roles_and_priority(features: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    frame = features.copy()
    eligible = frame.loc[~frame.is_isolated & ~frame.truncated_by_depth, 'pagerank']
    threshold = float(eligible.quantile(c.COORDINATOR_PR_QUANTILE)) if len(eligible) else math.inf
    classifications = [classify_node(row, threshold) for row in frame.to_dict('records')]
    frame[['role', 'role_score', 'rule_id', 'evidence']] = pd.DataFrame(classifications, index=frame.index)
    frame['role_score'] = frame['role_score'].astype(float)
    # Ignore isolated PageRank in normalization: teleportation alone is no evidence.
    observed_pr = frame.pagerank.where(~frame.is_isolated, 0)
    pr_max = float(observed_pr.max())
    normalized = {
        'pagerank': observed_pr / pr_max if pr_max > 0 else observed_pr * 0,
        'turnover': _log_normalized(frame.in_kzt + frame.out_kzt),
        'seed_reach': _log_normalized(frame.seed_reach),
        'counterparties': _log_normalized(frame.counterparties),
        'transactions': _log_normalized(frame.in_tx + frame.out_tx),
    }
    score = pd.Series(0.0, index=frame.index)
    for feature, weight in c.PRIORITY_WEIGHTS.items():
        component = normalized[feature] * weight
        frame['priority_' + feature] = component
        score += component
    frame['priority_factor'] = np.where(frame.truncated_by_depth, c.BOUNDARY_PRIORITY_FACTOR, 1.0)
    frame.loc[frame.is_isolated, 'priority_factor'] = 0.0
    frame['priority_score'] = (score * frame.priority_factor).clip(0, 1).round(6)
    return frame, threshold
