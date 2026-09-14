from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from yoursql.common.config import default_audit_path
from yoursql.engine.runtime.database import Database


def test_default_audit_path_is_partitioned_by_date() -> None:
    assert default_audit_path(date(2026, 9, 14)) == Path(
        "logs", "audit-2026-09-14.jsonl"
    )


def test_database_writes_audit_events_to_default_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with Database(tmp_path / "audit.db") as database:
        database.execute("CREATE TABLE events(id INT);")
        database.execute("INSERT INTO events VALUES (1);")
        audit_path = database.audit.path

    assert audit_path == default_audit_path()
    assert audit_path is not None and audit_path.is_file()
    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["action"] == "INSERT" and event["success"] for event in events)


def test_explicit_audit_path_overrides_default(tmp_path: Path) -> None:
    audit_path = tmp_path / "custom" / "audit.jsonl"
    with Database(tmp_path / "custom.db", audit_path=audit_path) as database:
        database.execute("CREATE TABLE events(id INT);")

    assert audit_path.is_file()
    assert len(audit_path.read_text(encoding="utf-8").splitlines()) == 1
