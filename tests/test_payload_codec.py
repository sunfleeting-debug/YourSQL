"""manual payload 在存储层和 HTTP 层的端到端回归。"""

from __future__ import annotations

from pathlib import Path
from urllib.request import Request, urlopen

from yoursql.common import DatabaseConfig, ManualPayloadCodec, decode_payload
from yoursql.engine.runtime.database import Database
from yoursql.engine.services.http import HTTPService
from yoursql.storage.page import SlottedPage


def test_manual_codec_is_written_to_superblock_heap_index_and_catalog(tmp_path: Path) -> None:
    path = tmp_path / "manual.db"
    config = DatabaseConfig(payload_codec="manual")
    with Database(path, config=config) as database:
        database.execute(
            "CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR);"
            "CREATE INDEX idx_student_id ON student(id);"
            "INSERT INTO student VALUES (1, 'Alice'), (2, 'Bob');"
        )
        assert database.payload_codec.name == "manual"
        assert database.disk.read(0).payload.startswith(b"YSPL")

        catalog_page = database.disk.read(database.disk.named_page("catalog") or 0)
        assert catalog_page.payload.startswith(b"MCAT2")
        assert catalog_page.payload[13:].startswith(b"YSPL")

        table = database.catalog.get_table("student")
        heap_page = database.disk.read(int(table.page_ids[0]))
        slotted = SlottedPage.from_page(heap_page)
        assert slotted.slots[0] is not None
        assert slotted.slots[0].startswith(b"YSPL")

        index_page_id = database.catalog.get_index("idx_student_id").root_page_id
        assert index_page_id is not None
        index_page = database.disk.read(int(index_page_id))
        assert index_page.payload.startswith(b"MBIXYSPL")

    with Database(path, config=DatabaseConfig(payload_codec="json")) as reopened:
        assert reopened.payload_codec.name == "manual"
        assert reopened.execute("SELECT id, name FROM student ORDER BY id;").rows == [
            (1, "Alice"),
            (2, "Bob"),
        ]


def test_manual_http_request_and_response_are_decoded_without_json(tmp_path: Path) -> None:
    path = tmp_path / "manual-http.db"
    codec = ManualPayloadCodec()
    with Database(path, config=DatabaseConfig(payload_codec="manual")) as database:
        with HTTPService(database, legacy_anonymous=True) as service:
            base = f"http://{service.address[0]}:{service.address[1]}"
            body = codec.encode({"sql": "SELECT 1;"})
            request = Request(
                base + "/sql",
                data=body,
                method="POST",
                headers={
                    "Accept": "application/x-yoursql, application/json",
                    "Content-Type": "application/x-yoursql; version=1",
                },
            )
            with urlopen(request) as response:
                raw = response.read()
                assert "application/x-yoursql" in response.headers["Content-Type"]
                value, selected = decode_payload(raw, "manual")
                assert selected.name == "manual"
                assert value["rows"] == [[1]]
