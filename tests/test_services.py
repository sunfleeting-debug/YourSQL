import io
import json
from pathlib import Path
from urllib.request import Request, urlopen

from yoursql.engine.database import Database
from yoursql.engine.http import HTTPService
from yoursql.engine.ssh import SSHStdioServer


def test_http_health_metrics_and_sql(tmp_path: Path) -> None:
    with Database(tmp_path / "http.db") as database, HTTPService(database) as service:
        base = f"http://{service.address[0]}:{service.address[1]}"
        with urlopen(base + "/health") as response:
            assert json.load(response)["status"] == "ok"
        payload = json.dumps({"sql": "CREATE TABLE t(id INT);"}).encode()
        request = Request(base + "/sql", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request) as response:
            assert json.load(response)["message"] == "CREATE TABLE t"
        with urlopen(base + "/metrics") as response:
            assert json.load(response)["catalog"]["tables"] == 1


def test_ssh_stdio_json_lines(tmp_path: Path) -> None:
    output = io.StringIO()
    with Database(tmp_path / "ssh.db") as database:
        code = SSHStdioServer(database, io.StringIO("CREATE TABLE t(id INT);\nSHOW TABLES;\n"), output).run()
    assert code == 0
    lines = [json.loads(line) for line in output.getvalue().splitlines()]
    assert lines[1]["rows"] == [["t"]]
