import json
import unittest

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from backend.serialization import Gid, SafeJSONResponse, id_to_string


class Edge(BaseModel):
    src: Gid
    dst: Gid


class SerializationTests(unittest.TestCase):
    def test_nested_int64_ids_survive_actual_api_response(self):
        application = FastAPI(default_response_class=SafeJSONResponse)

        @application.get("/ids")
        def ids():
            return {"nodes": [{"gid": 2**53 + 1}], "edge": {"src": 2**63 - 1, "dst": 2**53 + 3}}

        with TestClient(application) as client:
            body = client.get("/ids").json()
        self.assertEqual(body["nodes"][0]["gid"], "9007199254740993")
        self.assertEqual(body["edge"], {"src": "9223372036854775807", "dst": "9007199254740995"})

    def test_numpy_scalar_ids_stay_exact_and_counts_stay_numeric(self):
        response = SafeJSONResponse({"gid": np.int64(2**53 + 1), "count": np.int64(2)})
        self.assertEqual(json.loads(response.body), {"gid": "9007199254740993", "count": 2})

    def test_rejects_already_rounded_or_invalid_ids(self):
        for value in (float(2**53 + 1), np.float64(2**53), True, None, "1.0", "1e16", 2**63):
            with self.subTest(value=value), self.assertRaises(ValueError):
                id_to_string(value)

    def test_pydantic_contract_keeps_exact_id_strings(self):
        edge = Edge(src=np.int64(2**53 + 1), dst="9223372036854775807")
        self.assertEqual(json.loads(edge.model_dump_json())["src"], "9007199254740993")
        with self.assertRaises(ValidationError):
            Edge(src=float(2**53), dst="1")

    def test_int64_boundaries(self):
        self.assertEqual(id_to_string(-(2**63)), "-9223372036854775808")
        self.assertEqual(id_to_string(2**63 - 1), "9223372036854775807")


if __name__ == "__main__":
    unittest.main()
