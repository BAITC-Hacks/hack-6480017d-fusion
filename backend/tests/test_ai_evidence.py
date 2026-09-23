"""Evidence quality checks run entirely offline, including schema failure cases."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from backend.ai_evidence import build_evidence, local_fallback
from backend.ai_models import AnalystReport, ReviewerReport, validate_analyst, validate_reviewer
from backend.analysis_service import AnalysisService


BASE_ID = 9007199254740993


def make_service(nodes, edges):
    """Initialize the real service; replace only parquet loading and file hashes."""
    node_frame = pd.DataFrame(nodes, columns=['gid', 'depth', 'is_seed'])
    edge_frame = pd.DataFrame(edges, columns=['src', 'dst', 'sum_kzt', 'n_tx'])
    hashes = {'nodes': 'n', 'edges': 'e', 'transactions': 't'}
    audit = {
        'hashes': hashes,
        'summary': {'period': {'start': '2026-07-01', 'end': '2026-07-31'},
                    'transaction_count': int(edge_frame.n_tx.sum()),
                    'seed_count': int(node_frame.is_seed.sum())},
        'statistics': {'transaction_sum_kzt': float(edge_frame.sum_kzt.sum())},
    }
    with patch('backend.analysis_service.pd.read_parquet', side_effect=[node_frame, edge_frame]), \
            patch('backend.analysis_service._sha256', side_effect=lambda path: hashes[path.stem]):
        return AnalysisService(Path('test-fixture'), audit)


def sample_service():
    return make_service(
        [(BASE_ID, 0, True), (BASE_ID + 1, 1, False),
         (BASE_ID + 2, 4, False), (BASE_ID + 3, 0, True)],
        [(BASE_ID, BASE_ID + 1, 10000.0, 2), (BASE_ID + 1, BASE_ID + 2, 5000.0, 1)],
    )


class EvidencePacketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = sample_service()

    def test_boundary_is_not_a_terminal_claim_and_keeps_limitation(self):
        packet = build_evidence(self.service, str(BASE_ID + 2))
        facts = {fact['evidence_id']: fact['value'] for fact in packet['facts']}
        self.assertEqual(packet['role'], 'peripheral')
        self.assertTrue(facts['f_truncated_by_depth'])
        self.assertEqual(facts['f_in_kzt'], 5000.0)
        self.assertEqual(facts['f_out_kzt'], 0.0)
        self.assertIn('Граница выгрузки', ' '.join(item['text'] for item in packet['caveats']))
        fallback = local_fallback(packet)
        self.assertIn('Граница выгрузки', ' '.join(fallback['limitations']))
        self.assertEqual(fallback, validate_analyst(fallback, packet))

    def test_isolated_seed_preserved_with_no_invented_edges(self):
        packet = build_evidence(self.service, str(BASE_ID + 3))
        facts = {fact['evidence_id']: fact['value'] for fact in packet['facts']}
        self.assertEqual(packet['neighbors'], [])
        self.assertEqual((packet['total_neighbors'], packet['returned_neighbors']), (0, 0))
        self.assertFalse(packet['truncated'])
        self.assertTrue(facts['f_is_seed'])
        self.assertTrue(facts['f_is_isolated'])
        self.assertEqual(facts['f_priority_score'], 0)
        self.assertIsNone(facts['f_pass_through'])
        fallback = local_fallback(packet)
        self.assertIn('нет связей', ' '.join(fallback['limitations']))
        self.assertIn('seed', ' '.join(fallback['limitations']))

    def test_ids_remain_exact_strings_and_packet_does_not_modify_analysis(self):
        original = copy.deepcopy(self.service.nodes)
        packet = build_evidence(self.service, str(BASE_ID))
        self.assertIsInstance(packet['gid'], str)
        gid_fact = next(f for f in packet['facts'] if f['evidence_id'] == 'f_gid')
        self.assertEqual(gid_fact['value'], str(BASE_ID))
        edge = packet['neighbors'][0]
        self.assertEqual((edge['src'], edge['dst']), (str(BASE_ID), str(BASE_ID + 1)))
        local_fallback(packet)
        self.assertEqual(self.service.nodes, original)
        self.assertEqual(len(packet['facts']), 24)
        all_ids = [item['evidence_id'] for section in ('facts', 'neighbors', 'caveats') for item in packet[section]]
        self.assertEqual(len(all_ids), len(set(all_ids)))
        json.dumps(packet, allow_nan=False)

    def test_bounded_edges_count_unique_counterparties_and_exclude_self(self):
        nodes = [(BASE_ID, 0, True)] + [(BASE_ID + i, 1, False) for i in range(1, 13)]
        edges = [(BASE_ID, BASE_ID, 1000000.0, 1),
                 (BASE_ID, BASE_ID + 1, 200000.0, 1), (BASE_ID + 1, BASE_ID, 199000.0, 1)]
        edges += [(BASE_ID, BASE_ID + i, 170000.0 - i * 10000.0, 1) for i in range(2, 13)]
        service = make_service(nodes, edges)
        packet = build_evidence(service, str(BASE_ID))
        self.assertEqual(packet['total_neighbors'], 12)
        self.assertEqual(packet['returned_neighbors'], 6)
        self.assertEqual(len(packet['neighbors']), 8)
        self.assertTrue(packet['truncated'])
        self.assertIn('caveat_packet_truncated', {c['evidence_id'] for c in packet['caveats']})
        self.assertIn('список соседей сокращён', ' '.join(local_fallback(packet)['limitations']))
        self.assertEqual(packet['neighbors'][0]['sum_kzt'], 1000000.0)
        service.edges.reverse()
        self.assertEqual(packet, build_evidence(service, str(BASE_ID)))

    def test_fingerprint_changes_with_input_or_rules(self):
        original = build_evidence(self.service, str(BASE_ID))['data_fingerprint']
        service = copy.deepcopy(self.service)
        service.input_hashes['transactions'] = 'new-transactions'
        self.assertNotEqual(build_evidence(service, str(BASE_ID))['data_fingerprint'], original)
        with patch('backend.ai_evidence.RULES_VERSION', 'new-rules'):
            self.assertNotEqual(build_evidence(self.service, str(BASE_ID))['data_fingerprint'], original)


class ResponseValidationTests(unittest.TestCase):
    def setUp(self):
        self.packet = build_evidence(sample_service(), str(BASE_ID + 2))
        self.report = local_fallback(self.packet)

    def test_accepts_review_of_known_claim_or_overall_and_no_issues(self):
        payload = {'issues': [], 'alternative_explanations': [], 'missing_information': []}
        self.assertEqual(validate_reviewer(payload, self.packet, self.report), payload)
        payload['issues'] = [{
            'claim_id': 'overall', 'kind': 'missing_caveat',
            'reason': 'Нужна оговорка о границе выгрузки.', 'evidence_ids': ['f_truncated_by_depth'],
        }]
        self.assertEqual(validate_reviewer(payload, self.packet, self.report), payload)

    def test_analyst_rejects_foreign_reference_unknown_id_and_changed_role_field(self):
        for mutation in (
            lambda report: report['claims'][0].update(evidence_ids=['nonexistent_fact']),
            lambda report: report.update(hypothesis='Проверить узел 999999999999999999'),
            lambda report: report.update(hypothesis='Проверить узел 999999999999999999.'),
            lambda report: report.update(role='coordinator'),
            lambda report: report.update(priority_score=1.0),
            lambda report: report['claims'].append(copy.deepcopy(report['claims'][0])),
            lambda report: report.update(hypothesis=' '),
            lambda report: report['claims'][0].update(text=42),
        ):
            report = copy.deepcopy(self.report)
            mutation(report)
            with self.subTest(report=report):
                with self.assertRaises(ValueError):
                    validate_analyst(report, self.packet)

    def test_known_gid_and_long_decimal_pagerank_are_accepted(self):
        self.report['claims'][0]['text'] = f"Узел {self.packet['gid']}; PageRank 0.12345678901234567."
        self.assertEqual(validate_analyst(self.report, self.packet), self.report)

    def test_review_cannot_reference_absent_claim_or_fact(self):
        base = {'issues': [{
            'claim_id': 'c1', 'kind': 'overstatement', 'reason': 'Нужна проверка.',
            'evidence_ids': ['f_evidence'],
        }], 'alternative_explanations': [], 'missing_information': []}
        for changes in ({'claim_id': 'c2'}, {'evidence_ids': ['ghost']}, {'kind': 'approved'},
                        {'reason': 'Узел 999999999999999999'}, {'claim_id': None}):
            payload = copy.deepcopy(base)
            payload['issues'][0].update(changes)
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    validate_reviewer(payload, self.packet, self.report)

    def test_all_schema_object_fields_are_required_and_forbid_extras(self):
        for model in (AnalystReport, ReviewerReport):
            schema = model.model_json_schema()
            for shape in (schema, *schema.get('$defs', {}).values()):
                if shape.get('type') == 'object':
                    self.assertEqual(set(shape['required']), set(shape['properties']))
                    self.assertFalse(shape['additionalProperties'])


if __name__ == '__main__':
    unittest.main()
