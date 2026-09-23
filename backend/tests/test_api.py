import tempfile
import unittest
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.audit import DEFAULT_DATA_DIR


class APITests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
