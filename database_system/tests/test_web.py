"""HTTP integration checks using an isolated database."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from database_system.engine.database import Database
from database_system.web.server import make_server


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / "web.db"))
        self.server = make_server(self.db, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.token = self.request("/api/state")["token"]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.db.close()
        self.temp.cleanup()

    def request(self, path, payload=None, token=None):
        request = Request(self.url + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json",
                     "X-YourSQL-Token": token or getattr(self, "token", "")})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_crud_schema_and_persistence(self):
        data = self.request("/api/execute", {"sql":
            "CREATE TABLE t (id INT, name VARCHAR(30));"
            "INSERT INTO t VALUES (1, 'Alice'); SELECT * FROM t;"})
        self.assertTrue(all(r["ok"] for r in data["results"]))
        self.assertEqual(data["results"][-1]["rows"], [[1, "Alice"]])
        self.assertEqual(data["tables"][0]["columns"][1]["type"], "VARCHAR(30)")
        self.request("/api/execute", {"sql": "UPDATE t SET name = 'Bob' WHERE id = 1;"})
        with Database(self.db.path) as reopened:
            self.assertEqual(reopened.execute("SELECT * FROM t;")[0].rows, [[1, "Bob"]])
        self.request("/api/execute", {"sql": "DELETE FROM t; DROP TABLE t;"})
        self.assertEqual(self.request("/api/state")["tables"], [])

    def test_error_and_plan(self):
        data = self.request("/api/execute", {"sql": "SELECT * FROM missing;"})
        self.assertFalse(data["results"][0]["ok"])
        self.assertEqual(data["results"][0]["stage"], "Semantic")
        self.request("/api/execute", {"sql": "CREATE TABLE t (id INT);"})
        result = self.request("/api/execute", {"sql": "EXPLAIN SELECT * FROM t;"})["results"][0]
        self.assertTrue(result["plan_before"])
        self.assertTrue(result["plan_after"])

    def test_reject_invalid_requests(self):
        for payload in ({"sql": ""}, {"sql": 123}, ["SELECT"]):
            with self.assertRaises(HTTPError) as error:
                self.request("/api/execute", payload)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/execute", {"sql": "CREATE TABLE t (id INT);"}, token="wrong")
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.request("/api/state")["tables"], [])

    def test_static_routes_and_host(self):
        for path in ("/", "/style.css", "/app.js"):
            with urlopen(self.url + path, timeout=5) as response:
                self.assertEqual(response.status, 200)
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.url + "/api/state", headers={"Host": "example.com"}), timeout=5)
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
