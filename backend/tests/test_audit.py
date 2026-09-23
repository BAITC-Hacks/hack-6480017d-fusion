"""Regressions for data loss and incomplete checks in the organizer's starter."""

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from backend.audit import EXPECTED_TYPES, assert_valid_audit, audit_data


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        # A four-hop directed chain plus an isolated seed; IDs exceed 2**53.
        self.ids = [2**53 + offset for offset in range(1, 7)]
        self.rows = {
            "nodes": {
                "gid": self.ids, "depth": [0, 1, 2, 3, 4, 0],
                "is_seed": [True, False, False, False, False, True],
            },
            "edges": {
                "src": self.ids[:4], "dst": self.ids[1:5],
                "sum_kzt": [10000.0, 5000.0, 5000.0, 5000.0],
                "n_tx": [2, 1, 1, 1], "depth": [1, 2, 3, 4],
            },
            "transactions": {
                "src": [self.ids[0], *self.ids[:4]],
                "dst": [self.ids[1], *self.ids[1:5]],
                "sum_kzt": [5000.0] * 5, "date": [date(2026, 7, 4)] * 5,
            },
        }

    def write_inputs(self) -> None:
        for name, data in self.rows.items():
            schema = pa.schema(list(EXPECTED_TYPES[name].items()))
            pq.write_table(pa.Table.from_pydict(data, schema=schema), self.data_dir / f"{name}.parquet")

    def test_duplicates_isolated_seed_and_boundary_are_preserved(self) -> None:
        self.write_inputs()
        report = audit_data(self.data_dir)
        assert_valid_audit(report)
        self.assertEqual(report["summary"]["node_count"], 6)
        self.assertEqual(report["summary"]["transaction_count"], 5)
        self.assertEqual(report["statistics"]["edge_n_tx_total"], 5)
        self.assertEqual(report["statistics"]["transaction_sum_kzt"], 25000.0)
        self.assertEqual(report["statistics"]["duplicate_transaction_rows_excluding_first"], 1)
        self.assertEqual(report["statistics"]["isolated_seed_nodes"], 1)
        self.assertEqual(report["statistics"]["weak_components_all_nodes"], 2)
        self.assertEqual(report["statistics"]["boundary_without_outgoing"], 1)
        self.assertEqual(report["statistics"]["min_gid"], str(self.ids[0]))

    def test_wrong_pair_sums_fail_even_when_overall_total_matches(self) -> None:
        self.rows["edges"]["sum_kzt"][:2] = [9999.0, 5001.0]
        self.write_inputs()
        report = audit_data(self.data_dir)
        with self.assertRaisesRegex(ValueError, "сумма совпадает у каждой пары"):
            assert_valid_audit(report)
        self.assertTrue(next(item["passed"] for item in report["checks"]
                             if item["name"] == "Агрегация: общий оборот совпадает"))

    def test_wrong_pair_counts_fail_even_when_overall_count_matches(self) -> None:
        self.rows["edges"]["n_tx"][:2] = [1, 2]
        self.write_inputs()
        report = audit_data(self.data_dir)
        with self.assertRaisesRegex(ValueError, "n_tx совпадает у каждой пары"):
            assert_valid_audit(report)
        self.assertTrue(next(item["passed"] for item in report["checks"]
                             if item["name"] == "Агрегация: общее число переводов совпадает"))


if __name__ == "__main__":
    unittest.main()
