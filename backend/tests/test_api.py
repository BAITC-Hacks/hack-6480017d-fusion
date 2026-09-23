import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.audit import DEFAULT_DATA_DIR, EXPECTED_TYPES


def write_small_dataset(directory: Path, edge_amount: float = 5000.0) -> None:
    """One observed transfer and an isolated seed, with IDs above 2**53."""
    source, target, isolated = (2**53 + offset for offset in (1, 2, 3))
    rows = {
        "nodes": {
            "gid": [source, target, isolated], "depth": [0, 1, 0],
            "is_seed": [True, False, True],
        },
        "edges": {
            "src": [source], "dst": [target], "sum_kzt": [edge_amount],
            "n_tx": [1], "depth": [1],
        },
        "transactions": {
            "src": [source], "dst": [target], "sum_kzt": [5000.0],
            "date": [date(2026, 7, 12)],
        },
    }
    for name, data in rows.items():
        schema = pa.schema(list(EXPECTED_TYPES[name].items()))
        pq.write_table(pa.Table.from_pydict(data, schema=schema), directory / f"{name}.parquet")


class APITests(unittest.TestCase):
    def test_summary_uses_selected_dataset_including_isolated_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            write_small_dataset(Path(directory))
            with TestClient(create_app(Path(directory))) as client:
                response = client.get("/api/summary")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {
                    "nodes": 3, "edges": 1, "transactions": 1, "seed_nodes": 2,
                    "period": {"start": "2026-07-12", "end": "2026-07-12"},
                })

    def test_summary_matches_local_parquet(self):
        nodes = pd.read_parquet(DEFAULT_DATA_DIR / "nodes.parquet")
        edges = pd.read_parquet(DEFAULT_DATA_DIR / "edges.parquet")
        tx = pd.read_parquet(DEFAULT_DATA_DIR / "transactions.parquet")
        dates = pd.to_datetime(tx["date"])
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/api/health").json(), {"status": "ok", "service": "Fusion"})
            response = client.get("/api/summary")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {
                "nodes": len(nodes), "edges": len(edges), "transactions": len(tx),
                "seed_nodes": int(nodes["is_seed"].sum()),
                "period": {"start": dates.min().date().isoformat(), "end": dates.max().date().isoformat()},
            })

    def test_missing_data_is_explicit_503_but_process_is_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs("backend.app", level="ERROR"):
                with TestClient(create_app(Path(directory))) as client:
                    self.assertEqual(client.get("/api/health").status_code, 200)
                    self.assertEqual(client.get("/api/summary").status_code, 503)

    def test_inconsistent_aggregates_are_not_served_as_valid_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            write_small_dataset(Path(directory), edge_amount=6000.0)
            with self.assertLogs("backend.app", level="ERROR"):
                with TestClient(create_app(Path(directory))) as client:
                    self.assertEqual(client.get("/api/health").status_code, 200)
                    response = client.get("/api/summary")
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {
                        "detail": "Данные недоступны или не прошли проверку. Запустите аудит данных.",
                    })


if __name__ == "__main__":
    unittest.main()
