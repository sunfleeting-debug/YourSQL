"""HTTP integration checks using an isolated database."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from database_system.engine.database import Database
from database_system.web.server import STATIC, make_server


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
        self.assertEqual(data["results"][0]["error"]["line"], 1)
        self.assertGreaterEqual(data["results"][0]["error"]["column"], 1)
        debug = data["results"][0]["debug"]
        self.assertTrue(debug["lexer"]["tokens"])
        self.assertEqual(debug["lexer"]["status"], "passed")
        self.assertEqual(debug["parser"]["status"], "passed")
        self.assertEqual(debug["semantic"]["status"], "error")
        self.assertEqual(debug["planner"]["status"], "skipped")
        self.request("/api/execute", {"sql": "CREATE TABLE t (id INT);"})
        result = self.request("/api/execute", {"sql": "EXPLAIN SELECT * FROM t;"})["results"][0]
        self.assertTrue(result["plan_before"])
        self.assertTrue(result["plan_after"])
        self.assertEqual(result["debug"]["optimizer"]["status"], "passed")
        self.assertEqual(result["debug"]["execute"]["status"], "skipped")

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

    def test_frontend_demo_case_picker(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        styles = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn('id="test-case"', html)
        self.assertIn('id="results-panel"', html)
        self.assertIn('id="toggle-results"', html)
        self.assertIn('id="results-resize-handle"', html)
        for view_id in ("tokens", "ast", "semantic", "plan", "optimizer", "execute"):
            self.assertIn(f'data-view="{view_id}"', html)
        self.assertIn("setResultsDrawer", script)
        self.assertIn("resizeResults", script)
        self.assertIn("renderDebugStage", script)
        self.assertIn('"debug": pipeline_debug(r)', (Path(__file__).parents[1] / "web" / "server.py").read_text(encoding="utf-8"))
        self.assertIn("setResultsDrawer(window.innerWidth>700)", script)
        self.assertIn("errorRange(source)", script)
        self.assertIn("executionOrigin(editor,raw,start)", script)
        self.assertIn("translateErrorPositions(data.results,origin)", script)
        self.assertIn("sql-error", script)
        self.assertIn("sql-error-lens", script)
        self.assertIn("editor-error-line", script)
        self.assertIn(".code-editor{flex:1 1 0;min-height:0;overflow:hidden;resize:none}", styles)
        self.assertIn(".editor-stack #editor{min-height:0;overflow-y:scroll", styles)
        self.assertIn("overflow-y:scroll", styles)
        self.assertIn("html,body{height:100%;overflow:hidden}", styles)
        self.assertIn(".workspace{height:calc(100vh - 106px);min-height:0;overflow:hidden}", styles)
        self.assertIn("scrollbar-gutter:stable", styles)
        for case_id in (
            "crud", "types", "query", "plan", "error_lexer",
            "error_syntax", "error_semantic", "error_type", "lifecycle",
        ):
            self.assertIn(f'value="{case_id}"', html)
            self.assertIn(f"{case_id}:{{name:", script)


if __name__ == "__main__":
    unittest.main()
