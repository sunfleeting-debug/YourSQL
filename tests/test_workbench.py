"""以真实 HTTP、临时数据库和持久化权限验收工作台。"""

import hashlib
import json
import time
from http.cookiejar import CookieJar
from pathlib import Path
from threading import Event
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pytest

from yoursql.common import AuthorizationError, DatabaseConfig, YourSQLError
from yoursql.common.trace import ExecutionTrace, current_trace
from yoursql.engine.runtime.database import Database
from yoursql.engine.services.http import HTTPService
from yoursql.engine.security.session import Session
from yoursql.engine.services.workbench_sql import redact_sql, split_sql
from yoursql.sql.lexer import tokenize


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def request(self, path: str, body: dict[str, object] | None = None,
                *, method: str | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict]:
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            response = self.opener.open(request, timeout=8)
        except HTTPError as exc:
            response = exc
        with response:
            data = json.load(response)
            assert response.headers["X-Request-ID"]
            if path.startswith("/api/"):
                assert data["request_id"] == response.headers["X-Request-ID"]
                assert data["ok"] == (response.status < 400)
            return response.status, data

    def upload(self, path: str, filename: str, content: bytes) -> tuple[int, dict]:
        """模拟文件选择器提交二进制数据库文件。"""

        request = Request(self.base + path, data=content, method="POST", headers={
            "Content-Type": "application/octet-stream",
            "X-YourSQL-File-Name": filename,
        })
        try:
            response = self.opener.open(request, timeout=8)
        except HTTPError as exc:
            response = exc
        with response:
            data = json.load(response)
            assert response.headers["X-Request-ID"]
            assert data["request_id"] == response.headers["X-Request-ID"]
            assert data["ok"] == (response.status < 400)
            return response.status, data

    def login(self, username: str = "admin", password: str = "admin") -> dict:
        status, body = self.request("/api/auth/login", {"username": username, "password": password})
        assert status == 200, body
        return body["data"]

    def query(self, sql: str, **options: object) -> dict:
        status, response = self.request("/api/queries", {"sql": sql, **options})
        assert status == 202, response
        task_id = response["data"]["id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status, response = self.request(f"/api/queries/{task_id}")
            assert status == 200, response
            if response["data"]["status"] not in {"running", "queued"}:
                data = response["data"]
                data["details"] = [self.request(f"/api/queries/{task_id}/results/{index}")[1]["data"]
                                   for index in range(len(data["results"]))]
                return data
            time.sleep(0.01)
        raise AssertionError("任务未在测试期限内完成")


@pytest.fixture
def service(tmp_path: Path):
    with Database(tmp_path / "workbench.db") as database:
        database.execute("CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR); INSERT INTO student VALUES(1,'Alice'),(2,NULL); CREATE INDEX idx_id ON student(id);")
        database.execute("CREATE TABLE secret(id INT); INSERT INTO secret VALUES (99); CREATE USER reader IDENTIFIED BY 'reader-password'; GRANT SELECT ON student TO USER reader;")
        with HTTPService(database, legacy_anonymous=False, allowed_origins=("http://127.0.0.1:5173",)) as http:
            client = Client(f"http://{http.address[0]}:{http.address[1]}")
            yield database, http, client


def test_login_permissions_and_metadata(service) -> None:
    _, _, client = service
    assert client.request("/api/tables")[0] == 401
    assert client.request("/sql", {"sql": "SHOW TABLES"})[0] == 401
    assert client.request("/api/auth/login", {"username": "admin", "password": "wrong"})[0] == 401
    me = client.login("reader", "reader-password")
    assert me["permissions"] == ["SELECT STUDENT"]
    assert client.request("/api/databases/available")[0] == 403
    assert all(cookie._rest.get("HttpOnly") is None and cookie._rest.get("SameSite") == "Strict" for cookie in client.cookies)
    data = client.request("/api/databases")[1]["data"]
    assert data["single_database"] is True
    assert len(data["databases"]) == 1
    tables = data["databases"][0]["tables"]
    assert [table["name"] for table in tables] == ["student"]
    assert tables[0]["columns"][0]["type"] == "INT"
    assert tables[0]["indexes"][0]["name"] == "idx_id"
    assert tables[0]["create_sql"].startswith("CREATE TABLE student")
    assert client.request("/api/tables/secret")[0] == 403
    assert client.request("/api/storage")[0] == 403
    denied = client.query("SELECT id FROM student WHERE id IN (SELECT id FROM secret)")
    assert denied["error"]["code"] == "AUTHORIZATION_ERROR"
    assert client.query("DESC student")["status"] == "success"
    assert client.query("INSERT INTO student VALUES (3,'x')")["error"]["code"] == "AUTHORIZATION_ERROR"
    assert client.request("/api/auth/logout", {})[0] == 200
    assert client.request("/api/session")[0] == 401


def test_admin_metadata_exposes_system_views_only_to_admin(service) -> None:
    _, _, client = service
    client.login()
    data = client.request("/api/databases")[1]["data"]["databases"][0]
    system_views = [view for view in data["views"] if view.get("system")]
    assert {view["name"] for view in system_views} == {
        "sys_users", "sys_roles", "sys_role_members", "sys_privileges",
    }
    assert all("password_hash" not in {column["name"] for column in view["columns"]} for view in system_views)

    assert client.request("/api/auth/logout", {})[0] == 200
    client.login("reader", "reader-password")
    data = client.request("/api/databases")[1]["data"]["databases"][0]
    assert all(not view.get("system") for view in data["views"])


def test_view_metadata_exposes_definition_to_authorized_workbench_user(service) -> None:
    database, _, client = service
    database.execute("CREATE VIEW student_names AS SELECT id, name FROM student WHERE id > 0;")
    database.execute("GRANT SELECT ON student_names TO USER reader;")
    client.login("reader", "reader-password")

    data = client.request("/api/databases")[1]["data"]["databases"][0]
    assert [view["name"] for view in data["views"]] == ["student_names"]
    assert data["views"][0]["definition_sql"].startswith("SELECT id")
    result = client.query("SELECT name FROM student_names ORDER BY id")
    assert result["details"][0]["rows"] == [["Alice"], [None]]
    assert client.query("INSERT INTO student_names VALUES (3, 'blocked')")["error"]["code"] == "AUTHORIZATION_ERROR"


def test_database_file_selection_rebinds_workbench(tmp_path: Path) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    with Database(first_path) as first:
        first.execute("CREATE TABLE first_table(id INT); INSERT INTO first_table VALUES(1)")
    with Database(second_path, config=DatabaseConfig(page_size=8 * 1024)) as second:
        second.execute("CREATE TABLE second_table(id INT); INSERT INTO second_table VALUES(2)")
    with Database(first_path) as first:
        with HTTPService(first, legacy_anonymous=False) as http:
            client = Client(f"http://{http.address[0]}:{http.address[1]}")
            client.login()
            files = client.request("/api/databases/available")[1]["data"]
            assert files["active"] == "first.db"
            assert [item["name"] for item in files["files"]] == ["first.db", "second.db"]
            same_status, same_response = client.request("/api/databases/select", {"name": "first.db"})
            assert same_status == 200 and not same_response["data"]["changed"]
            assert client.request("/api/session")[0] == 200
            status, response = client.request("/api/databases/select", {"name": "second.db"})
            assert status == 200 and response["data"]["requires_login"]
            assert client.request("/api/session")[0] == 401
            client.login()
            metadata = client.request("/api/databases")[1]["data"]
            assert metadata["databases"][0]["name"] == "second.db"
            assert [table["name"] for table in metadata["databases"][0]["tables"]] == ["second_table"]


def test_database_file_selection_is_available_before_login(tmp_path: Path) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    with Database(first_path) as first:
        first.execute("CREATE TABLE first_table(id INT);")
    with Database(second_path) as second:
        second.execute("CREATE TABLE second_table(id INT);")
    with Database(first_path) as first:
        with HTTPService(first, legacy_anonymous=False) as http:
            client = Client(f"http://{http.address[0]}:{http.address[1]}")
            files_status, files_response = client.request("/api/databases/available-before-login")
            assert files_status == 200
            files = files_response["data"]
            assert files["active"] == "first.db"
            assert [item["name"] for item in files["files"]] == ["first.db", "second.db"]

            status, response = client.request("/api/databases/select-before-login", {"name": "second.db"})
            assert status == 200 and response["data"]["requires_login"]
            assert client.request("/api/session")[0] == 401

            client.login()
            metadata = client.request("/api/databases")[1]["data"]
            assert metadata["databases"][0]["name"] == "second.db"
            assert [table["name"] for table in metadata["databases"][0]["tables"]] == ["second_table"]


def test_database_path_selection_and_creation_with_config(tmp_path: Path) -> None:
    first_path = tmp_path / "first.db"
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    external_path = external_dir / "external.db"
    created_path = external_dir / "created.db"
    with Database(first_path) as first:
        first.execute("CREATE TABLE first_table(id INT);")
    with Database(external_path) as external:
        external.execute("CREATE TABLE external_table(id INT);")

    with Database(first_path) as first:
        with HTTPService(first, legacy_anonymous=False) as http:
            client = Client(f"http://{http.address[0]}:{http.address[1]}")
            client.login()

            status, response = client.request("/api/databases/select", {"path": str(external_path)})
            assert status == 200 and response["data"]["path"].endswith("external.db")
            assert client.request("/api/session")[0] == 401

            client.login()
            status, response = client.request("/api/databases/create", {
                "path": str(created_path), "page_size": 8192, "buffer_pool_size": 32,
                "replacement_policy": "fifo",
            })
            assert status == 201 and response["data"]["config"] == {
                "page_size": 8192, "buffer_pool_size": 32, "replacement_policy": "fifo",
                "payload_codec": "json",
            }
            assert created_path.is_file()
            assert client.request("/api/session")[0] == 401
            assert Database.detect_page_size(created_path) == 8192


def test_database_import_from_file_picker(tmp_path: Path) -> None:
    first_path = tmp_path / "first.db"
    source_path = tmp_path / "source.db"
    with Database(first_path) as first:
        first.execute("CREATE TABLE first_table(id INT);")
    with Database(source_path) as source:
        source.execute("CREATE TABLE imported_table(id INT); INSERT INTO imported_table VALUES (7);")
    source_content = source_path.read_bytes()
    imported_path = tmp_path / "picked.db"

    with Database(first_path) as first:
        with HTTPService(first, legacy_anonymous=False) as http:
            client = Client(f"http://{http.address[0]}:{http.address[1]}")
            client.login()

            status, response = client.upload("/api/databases/import", "picked.db", source_content)
            assert status == 201
            assert response["data"]["database"] == "picked.db"
            assert response["data"]["requires_login"] is True
            assert imported_path.is_file()
            assert client.request("/api/session")[0] == 401

            client.login()
            metadata = client.request("/api/databases")[1]["data"]
            assert metadata["databases"][0]["name"] == "picked.db"
            assert [table["name"] for table in metadata["databases"][0]["tables"]] == ["imported_table"]


def test_database_import_from_file_picker_before_login(tmp_path: Path) -> None:
    first_path = tmp_path / "first.db"
    source_path = tmp_path / "source.db"
    with Database(first_path) as first:
        first.execute("CREATE TABLE first_table(id INT);")
    with Database(source_path) as source:
        source.execute("CREATE TABLE imported_table(id INT);")

    with Database(first_path) as first:
        with HTTPService(first, legacy_anonymous=False) as http:
            client = Client(f"http://{http.address[0]}:{http.address[1]}")
            status, response = client.upload("/api/databases/import-before-login", "picked.db", source_path.read_bytes())
            assert status == 201
            assert response["data"]["database"] == "picked.db"
            assert client.request("/api/session")[0] == 401

            client.login()
            metadata = client.request("/api/databases")[1]["data"]
            assert metadata["databases"][0]["name"] == "picked.db"
            assert [table["name"] for table in metadata["databases"][0]["tables"]] == ["imported_table"]


def test_real_pipeline_result_pagination_and_history(service) -> None:
    _, _, client = service
    client.login()
    task = client.query("SELECT id, name FROM student ORDER BY id", row_limit=1)
    assert task["status"] == "success", task
    result = task["details"][0]
    assert result["rows"] == [[1, "Alice"]]
    assert result["total_rows"] == 2 and result["retained_rows"] == 1 and result["truncated"]
    assert [col["type"] for col in result["columns"]] == ["INT", "VARCHAR"]
    stages = {stage["name"]: stage for stage in result["stages"]}
    assert stages["tokens"]["data"][0]["kind"] == "SELECT"
    assert stages["ast"]["data"]["node"] == "Select"
    assert stages["binding"]["data"]["output_columns"] == ["id", "name"]
    assert stages["logical_plan"]["data"] == result["plan"]
    assert stages["optimized_plan"]["status"] == "success"
    assert stages["physical_plan"]["status"] == "partial"
    assert stages["executor"]["data"]["scans"][0]["operator"] == "SeqScan"
    assert stages["storage"]["data"]["events"]
    assert stages["statistics"]["data"]["returned_rows"] == 2
    assert result["plan_estimate"]["model"] == "optimizer_statistics"
    assert result["plan_estimate"]["rows"] == 2
    assert all(stage["source"]["line"] == 1 for stage in stages.values())
    result2 = client.query("SELECT * FROM student WHERE id = 2")["details"][0]
    assert result2["rows"] == [[2, None]]
    assert next(stage for stage in result2["stages"] if stage["name"] == "executor")["data"]["scans"][0]["operator"] == "IndexScan"
    assert result2["plan_estimate"]["total_cost"] == pytest.approx(0.2)
    assert result2["plan_estimate"]["rows"] == 1
    empty = client.query("SELECT id FROM student WHERE id = 7")["details"][0]
    assert empty["columns"][0]["type"] == "INT" and empty["rows"] == []
    assert client.request(f'/api/queries/{task["id"]}/results/0?offset=1&limit=1')[1]["data"]["rows"] == []
    history = client.request("/api/history")[1]["data"]
    assert history["total"] == 3 and all(item["status"] == "success" for item in history["items"])


def test_errors_positions_script_split_and_secret_redaction(service) -> None:
    _, _, client = service
    client.login()
    sql = "SELECT 'a; b' AS s;\n-- comment ;\n SELECT FROM student;\nINSERT INTO student VALUES(7,'skip');"
    result = client.query(sql)
    assert result["status"] == "error" and len(result["results"]) == 2
    assert result["error"]["line"] == 3 and result["error"]["position_accuracy"] == "token"
    assert client.query("SELECT * FROM student")["details"][0]["total_rows"] == 2
    syntax = client.request("/api/validate", {"sql": "SELECT 'a\nb';\nSELECT FROM student;"})[1]["data"]
    assert syntax["diagnostics"][0]["line"] == 3
    created = client.query("CREATE USER testuser IDENTIFIED BY 'TOP_SECRET_123';")
    assert created["status"] == "success"
    assert "TOP_SECRET_123" not in json.dumps(created)
    client.query("SELECT 'unterminated_SECRET")
    history = json.dumps(client.request("/api/history")[1])
    assert "TOP_SECRET" not in history and "unterminated_SECRET" not in history
    assert "a; b" not in history and "comment" not in history


def test_syntax_diagnostic_suggests_complete_keyword(service) -> None:
    """语法诊断应定位完整拼写词，并对接近的关键字给出轻量建议。"""

    _, _, client = service
    client.login()
    response = client.request("/api/validate", {"sql": "SELECT * FROM student wher id = 1"})
    diagnostic = response[1]["data"]["diagnostics"][0]

    assert diagnostic["line"] == 1 and diagnostic["column"] == 23
    assert diagnostic["suggestion"] == {"kind": "keyword", "replacement": "WHERE"}
    assert "wher" in diagnostic["message"] and "WHERE" in diagnostic["message"]


def test_binder_column_error_reports_referencing_token(service) -> None:
    """Binder 语义错误应定位到不存在列，而不是伪装成语句起点。"""

    _, _, client = service
    client.login()
    result = client.query("SELECT id\nFROM student\nWHERE status121 = 1;")

    assert result["status"] == "error"
    error = result["error"]
    assert error["code"] == "BINDER_ERROR"
    assert error["line"] == 3 and error["column"] == 7
    assert error["position_accuracy"] == "token"
    assert error["position_note"] is None
    binding = next(stage for stage in result["details"][0]["stages"] if stage["name"] == "binding")
    assert binding["error"]["line"] == 3 and binding["error"]["column"] == 7


@pytest.mark.parametrize(
    ("sql", "code", "line", "column"),
    [
        ("INSERT INTO student (missing) VALUES (1);", "BINDER_ERROR", 1, 22),
        ("INSERT INTO student VALUES (NULL, 'x');", "BINDER_ERROR", 1, 28),
        ("UPDATE student SET missing = 1;", "BINDER_ERROR", 1, 20),
        ("CREATE INDEX broken ON student (missing);", "BINDER_ERROR", 1, 33),
        ("CREATE TABLE student(id INT);", "CATALOG_ERROR", 1, 14),
        ("SHOW COLUMNS FROM missing;", "BINDER_ERROR", 1, 19),
        ("GRANT SELECT ON student TO USER missing;", "AUTHORIZATION_ERROR", 1, 33),
        ("SELECT 1 / 0;", "EXECUTION_ERROR", 1, 10),
        ("SELECT DATE('not-a-date');", "EXECUTION_ERROR", 1, 8),
    ],
)
def test_other_sql_errors_report_their_sql_token(service, sql: str, code: str, line: int, column: int) -> None:
    """可归因到 SQL 节点的绑定和执行错误不再回退到语句起点。"""

    _, _, client = service
    client.login()
    result = client.query(sql)

    assert result["status"] == "error"
    assert result["error"]["code"] == code
    assert result["error"]["line"] == line and result["error"]["column"] == column
    assert result["error"]["position_accuracy"] == "token"
    assert result["error"]["position_note"] is None


def test_storage_is_bounded_readonly_and_admin_can_inspect_internal_catalog(service) -> None:
    database, _, client = service
    client.login()
    expected_index_page_ids = set(database.index_manager.get("idx_id").physical_page_ids())
    before_file = hashlib.sha256(database.path.read_bytes()).hexdigest()
    before_buffer = database.buffer_pool.snapshot()
    before_io = database.disk.io_stats()
    storage = client.request("/api/storage?limit=2")[1]["data"]
    assert len(storage["pages"]) == 2 and storage["readonly"]
    assert storage["limitations"]
    page_id = int(database.catalog.get_table("student").page_ids[0])
    page = client.request(f"/api/storage/pages/{page_id}?limit=1")[1]["data"]
    assert page["slots"][0]["row"] == [1, "Alice"]
    assert len(page["slots"]) == 1 and page["total_slots"] == 2
    assert page["slots"][0]["page_offset"] >= page["header_size"]
    assert page["slots"][0]["byte_length"] > 0
    assert page["physical_layout"]["format"] == "double_ended_v2"
    assert page["physical_layout"]["slot_entry_size"] == 6
    assert page["slots"][0]["slot_directory_offset"] >= page["header_size"]
    page_tail = client.request(f"/api/storage/pages/{page_id}?offset=1&limit=1")[1]["data"]
    assert page_tail["slots"][0]["slot_id"] == 1
    first_offset = page["slots"][0]["page_offset"]
    first_end = first_offset + page["slots"][0]["byte_length"]
    second_offset = page_tail["slots"][0]["page_offset"]
    second_end = second_offset + page_tail["slots"][0]["byte_length"]
    assert first_end <= second_offset or second_end <= first_offset
    assert page["raw_payload"]["preview_bytes"] > 0 and page["raw_payload"]["hex"]
    assert page["raw_payload"]["text"] is not None and page["raw_payload"]["encoding"] == "utf-8"
    assert page["raw_page"]["size_bytes"] == page["page_size"]
    assert page["raw_page"]["preview_bytes"] == page["page_size"]
    assert not page["raw_page"]["truncated"]
    catalog = client.request(f'/api/storage/pages/{database.disk.named_page("catalog")}')[1]
    assert "password_hash" in json.dumps(catalog)
    assert database.rbac.users["admin"].password_hash not in json.dumps(catalog)
    assert "raw_page" in catalog["data"]
    assert any(table["name"] == "_sys_privileges" for table in catalog["data"]["catalog"]["system_tables"])
    index = client.request("/api/storage/indexes/idx_id?limit=1")[1]["data"]
    assert index["representation"] == "ordered_leaf_array" and index["entries"][0]["key"] == [1]
    assert set(index["all_page_ids"]) == expected_index_page_ids
    index_page = client.request(f"/api/storage/pages/{min(expected_index_page_ids)}?limit=1")[1]["data"]
    assert index_page["index_name"] == "idx_id"
    assert index_page["table_name"] == "student"
    assert client.request("/api/storage/pages/999999")[0] == 404
    assert client.request("/api/storage/pages/0", {}, method="PUT")[0] == 405
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before_file
    assert database.buffer_pool.snapshot() == before_buffer
    assert database.disk.io_stats() == before_io


def test_storage_masks_internal_permission_pages_for_non_admin(service) -> None:
    database, _, client = service
    client.login()
    assert client.query("CREATE USER inspector IDENTIFIED BY 'inspector-password'")["status"] == "success"
    assert client.query("GRANT SECURITY ON * TO USER inspector")["status"] == "success"
    assert client.query("GRANT SELECT ON * TO USER inspector")["status"] == "success"
    assert client.request("/api/auth/logout", {})[0] == 200
    client.login("inspector", "inspector-password")

    users_table = next(table for table in database.catalog.system_tables() if table.name == "_sys_users")
    page_id = int(users_table.page_ids[0])
    page = client.request(f"/api/storage/pages/{page_id}")[1]["data"]
    assert page["masked"] is True
    assert page["system_table"] is True
    assert page["table_name"] == "MASKED"
    assert "raw_page" not in page and "raw_payload" not in page
    assert page["slots"][0]["row"] == ["MASKED", "MASKED"]


def test_storage_map_mode_defers_table_and_index_labels(service, monkeypatch) -> None:
    """页面地图只取轻量页头；表/索引标签不得在加载路径上计算。"""

    database, _, client = service
    client.login()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("地图请求不应重算索引绑定表或逐页目录归属")

    monkeypatch.setattr("yoursql.engine.services.inspection._index_pages", forbidden)
    monkeypatch.setattr("yoursql.engine.services.inspection._table_for_page", forbidden)
    status, response = client.request("/api/storage?offset=0&limit=500&fields=map")
    assert status == 200
    storage = response["data"]
    assert storage["map_only"] is True and storage["readonly"]
    assert len(storage["pages"]) <= 500
    assert all("table_name" not in page and "index_name" not in page for page in storage["pages"])
    heap_page = next(page for page in storage["pages"] if page["type"] == "heap")
    assert heap_page["slot_count"] >= 1 and heap_page["page_size"] == database.disk.page_size
    # HOW：批大小上限由服务端常量决定，工作台按它以 500 页/批拉取整张地图。
    assert client.request("/api/storage?offset=0&limit=501&fields=map")[0] == 400
    assert len(client.request("/api/storage?offset=0&limit=500&fields=map")[1]["data"]["pages"]) <= 500


def test_storage_default_mode_keeps_page_labels(service) -> None:
    """非轻量模式仍返回表/索引标签，向后兼容旧客户端。"""

    database, _, client = service
    client.login()
    student_page = int(database.catalog.get_table("student").page_ids[0])
    index_page = min(database.index_manager.get("idx_id").physical_page_ids(readonly=True))

    storage = client.request(f"/api/storage?offset={student_page}&limit=1")[1]["data"]
    assert storage["map_only"] is False
    assert storage["pages"][0]["page_id"] == student_page
    assert storage["pages"][0]["table_name"] == "student"
    index_storage = client.request(f"/api/storage?offset={index_page}&limit=1")[1]["data"]
    assert index_storage["pages"][0]["index_name"] == "idx_id"
    assert index_storage["pages"][0]["table_name"] == "student"


def test_storage_refresh_endpoints_are_incremental_and_readonly(service) -> None:
    database, _, client = service
    client.login()
    initial = client.request("/api/storage?limit=2")[1]["data"]
    cache = client.request("/api/storage/cache?limit=2")[1]["data"]
    assert cache["readonly"] and len(cache["buffer_pool"]["frames"]) <= 2
    assert "evictions" in cache["buffer_pool"]["stats"] and cache["note"]

    student_page = int(database.catalog.get_table("student").page_ids[0])
    changes_before = client.request(f"/api/storage/changes?since={initial['storage_revision']}")[1]["data"]
    assert not changes_before["truncated"] and changes_before["pages"] == []
    client.query("INSERT INTO student VALUES (3, 'incremental')")
    changes_after = client.request(f"/api/storage/changes?since={initial['storage_revision']}")[1]["data"]
    assert not changes_after["truncated"] and student_page in changes_after["changed_page_ids"]
    assert any(page["page_id"] == student_page for page in changes_after["pages"])


def test_storage_replacement_policy_can_be_switched_without_resetting_cache(service) -> None:
    database, _, client = service
    client.login()
    before = database.buffer_pool.snapshot()

    status, response = client.request("/api/storage/cache/policy", {"replacement_policy": "fifo"})
    assert status == 200
    data = response["data"]
    assert data["changed"] and data["previous_policy"] == "lru"
    assert data["replacement_policy"] == "fifo"
    assert database.buffer_pool.replacement_policy == "fifo"
    assert data["buffer_pool"]["stats"] == before["stats"]
    assert data["buffer_pool"]["frames"] == before["frames"]

    status, response = client.request("/api/storage/cache/policy", {"replacement_policy": "fifo"})
    assert status == 200 and not response["data"]["changed"]
    assert client.request("/api/storage/cache/policy", {"replacement_policy": "random"})[0] == 400


@pytest.mark.parametrize("route,body", [
    ("/api/queries", {"sql": []}), ("/api/queries", {"sql": "SELECT 1", "row_limit": True}),
    ("/api/queries", {"sql": "SELECT 1", "timeout_seconds": 31}),
    ("/api/queries", {"sql": "SELECT 1;" * 33}), ("/api/queries", {"sql": "x" * 64001}),
    ("/api/storage?offset=-1", None), ("/api/history?limit=501", None),
])
def test_parameter_validation(service, route: str, body: dict | None) -> None:
    _, _, client = service
    client.login()
    assert client.request(route, body)[0] == 400


def test_cors_task_ownership_and_legacy_compatibility(service) -> None:
    _, _, client = service
    client.login()
    assert client.request("/api/session", headers={"Origin": "https://evil.example"})[0] == 403
    assert client.request("/api/session", headers={"Origin": "http://127.0.0.1:5173"})[0] == 200
    task = client.query("SHOW TABLES")
    other = Client(client.base)
    other.login()
    assert other.request(f'/api/queries/{task["id"]}')[0] == 404
    assert other.request(f'/api/queries/{task["id"]}/cancel', {})[0] == 404
    status, result = client.request("/sql", {"sql": "DESC student"})
    assert status == 200 and result["columns"] == ["field", "type", "null", "key", "default"]


def test_trace_cancel_timeout_and_no_default_behavior_change(tmp_path: Path) -> None:
    with Database(tmp_path / "trace.db") as database:
        database.execute("CREATE TABLE t(id INT); INSERT INTO t VALUES(1)")
        event = Event()
        event.set()
        trace = ExecutionTrace(time.monotonic() + 5, event)
        token = current_trace.set(trace)
        try:
            with pytest.raises(YourSQLError, match="CANCELLED"):
                database.execute("SELECT * FROM t")
            event.clear()
            trace.deadline = time.monotonic() - 1
            with pytest.raises(YourSQLError, match="TIMEOUT"):
                database.execute("SELECT * FROM t")
        finally:
            current_trace.reset(token)
        assert database.execute("SELECT * FROM t").rows == [(1,)]


def test_trace_step_guard_can_be_disabled() -> None:
    trace = ExecutionTrace(time.monotonic() + 5, Event(), max_steps=1)
    trace.steps = 2
    with pytest.raises(YourSQLError, match="RESOURCE_LIMIT"):
        trace.check()

    trace.max_steps = None
    trace.check()


def test_subquery_authorization_and_multiline_lexer(tmp_path: Path) -> None:
    with Database(tmp_path / "auth.db") as database:
        database.execute("CREATE TABLE public_data(id INT); CREATE TABLE private_data(id INT); CREATE USER reader IDENTIFIED BY 'p'; GRANT SELECT ON public_data TO USER reader")
        database.session = Session(database.rbac.authenticate("reader", "p"), database.rbac)
        with pytest.raises(AuthorizationError):
            database.execute("SELECT id FROM public_data WHERE id IN (SELECT id FROM private_data)")
    tokens = tokenize("SELECT 'x\r\ny';\r\nSELECT 2")
    assert [token.position for token in tokens if token.kind.value == "SELECT"] == [(1, 1), (3, 1)]
    slices = split_sql("-- x;\nSELECT 'a;\\\'b'; /* z; */ SELECT `a;b` FROM `t;t`; ;")
    assert len(slices) == 2
    assert "password" not in redact_sql("-- password\nSELECT 'password', 123")
