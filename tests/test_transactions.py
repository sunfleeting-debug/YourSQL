"""事务语义测试：提交、回滚、失败状态与 DDL 事务。"""

from __future__ import annotations

from pathlib import Path

import pytest

from yoursql.common import DatabaseConfig, TransactionError
from yoursql.engine.runtime.database import Database


def _rows(db: Database, sql: str) -> list[tuple[object, ...]]:
    return db.execute(sql).rows


def _make_db(path: Path, **config: object) -> Database:
    return Database(path, config=DatabaseConfig(**config))


def test_begin_commit_persists_changes(tmp_path: Path) -> None:
    path = tmp_path / "commit.db"
    with _make_db(path) as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (2, 'b')")
        assert db.execute("COMMIT").message.startswith("COMMIT")
    with _make_db(path) as db:
        assert _rows(db, "SELECT id FROM t ORDER BY id") == [(1,), (2,)]


def test_begin_rollback_discards_insert(tmp_path: Path) -> None:
    path = tmp_path / "rollback_insert.db"
    with _make_db(path) as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (2, 'b'), (3, 'c')")
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(3,)]
        db.execute("ROLLBACK")
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(1,)]
    with _make_db(path) as db:
        assert _rows(db, "SELECT id FROM t ORDER BY id") == [(1,)]


def test_rollback_restores_updated_and_deleted_rows(tmp_path: Path) -> None:
    path = tmp_path / "rollback_dml.db"
    with _make_db(path) as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))")
        db.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        db.execute("BEGIN")
        db.execute("UPDATE t SET v = 'zz' WHERE id = 2")
        db.execute("DELETE FROM t WHERE id = 3")
        db.execute("DELETE FROM t WHERE id = 1")
        assert _rows(db, "SELECT id, v FROM t") == [(2, "zz")]
        db.execute("ROLLBACK")
        assert _rows(db, "SELECT id, v FROM t ORDER BY id") == [
            (1, "a"),
            (2, "b"),
            (3, "c"),
        ]
    with _make_db(path) as db:
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(3,)]


def test_rollback_restores_index_lookups(tmp_path: Path) -> None:
    """索引页同样走页级前像，回滚后索引与堆表必须一致。"""

    with _make_db(tmp_path / "rollback_index.db") as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))")
        db.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        db.execute("CREATE INDEX idx_v ON t (v)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (3, 'c')")
        db.execute("UPDATE t SET v = 'q' WHERE id = 1")
        db.execute("ROLLBACK")
        assert _rows(db, "SELECT id FROM t WHERE v = 'c'") == []
        assert _rows(db, "SELECT id FROM t WHERE v = 'q'") == []
        assert _rows(db, "SELECT v FROM t WHERE id = 1") == [("a",)]
        assert _rows(db, "SELECT id FROM t WHERE v = 'b'") == [(2,)]


def test_autocommit_still_applies_single_statements(tmp_path: Path) -> None:
    with _make_db(tmp_path / "autocommit.db") as db:
        db.execute("CREATE TABLE t (id INT)")
        db.execute("INSERT INTO t VALUES (1)")
        assert db.current_transaction() is None
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(1,)]


def test_commit_and_rollback_without_transaction_are_rejected(tmp_path: Path) -> None:
    with _make_db(tmp_path / "no_txn.db") as db:
        with pytest.raises(TransactionError):
            db.execute("COMMIT")
        with pytest.raises(TransactionError):
            db.execute("ROLLBACK")


def test_nested_begin_is_rejected(tmp_path: Path) -> None:
    with _make_db(tmp_path / "nested.db") as db:
        db.execute("BEGIN")
        with pytest.raises(TransactionError):
            db.execute("BEGIN")
        db.execute("ROLLBACK")


def test_failed_statement_requires_rollback(tmp_path: Path) -> None:
    """显式事务里语句出错后只能 ROLLBACK，半条语句不会泄漏出去。"""

    with _make_db(tmp_path / "failed.db") as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("BEGIN")
        with pytest.raises(Exception):
            db.execute("INSERT INTO t VALUES (1)")  # 主键冲突
        txn = db.current_transaction()
        assert txn is not None and txn.failed
        with pytest.raises(TransactionError):
            db.execute("SELECT COUNT(*) FROM t")
        with pytest.raises(TransactionError):
            db.execute("COMMIT")
        db.execute("ROLLBACK")
        assert db.current_transaction() is None
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(1,)]


def test_failed_autocommit_statement_rolls_back(tmp_path: Path) -> None:
    with _make_db(tmp_path / "failed_auto.db") as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))")
        db.execute("INSERT INTO t VALUES (1, 'a')")
        with pytest.raises(Exception):
            # 多行插入中途冲突：整条语句必须一起撤掉，不能留下前半批。
            db.execute("INSERT INTO t VALUES (2, 'b'), (1, 'dup'), (3, 'c')")
        assert _rows(db, "SELECT id FROM t ORDER BY id") == [(1,)]


def test_create_table_can_be_rolled_back(tmp_path: Path) -> None:
    path = tmp_path / "ddl_rollback.db"
    with _make_db(path) as db:
        db.execute("CREATE TABLE keep (id INT)")
        db.execute("BEGIN")
        db.execute("CREATE TABLE gone (id INT)")
        assert _rows(db, "SHOW TABLES") != []
        db.execute("ROLLBACK")
        with pytest.raises(Exception):
            db.execute("SELECT * FROM gone")
    with _make_db(path) as db:
        with pytest.raises(Exception):
            db.execute("SELECT * FROM gone")


def test_create_index_can_be_rolled_back(tmp_path: Path) -> None:
    with _make_db(tmp_path / "ddl_index.db") as db:
        db.execute("CREATE TABLE t (id INT, v INT)")
        db.execute("INSERT INTO t VALUES (1, 10)")
        db.execute("BEGIN")
        db.execute("CREATE INDEX idx_v ON t (v)")
        assert db.catalog.find_index("idx_v") is not None
        db.execute("ROLLBACK")
        assert db.catalog.find_index("idx_v") is None
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(1,)]


def test_drop_table_is_rejected_inside_explicit_transaction(tmp_path: Path) -> None:
    with _make_db(tmp_path / "drop_guard.db") as db:
        db.execute("CREATE TABLE t (id INT)")
        db.execute("BEGIN")
        with pytest.raises(TransactionError):
            db.execute("DROP TABLE t")
        db.execute("ROLLBACK")
        # 自动提交下仍然可用
        db.execute("DROP TABLE t")
        assert db.catalog.find_table("t") is None


def test_multi_statement_script_runs_in_one_transaction(tmp_path: Path) -> None:
    """脚本里的显式事务跨语句累积，直到 COMMIT 才落盘。"""

    path = tmp_path / "script.db"
    with _make_db(path) as db:
        db.execute("CREATE TABLE t (id INT)")
        db.execute_script(
            "BEGIN; INSERT INTO t VALUES (1); INSERT INTO t VALUES (2); COMMIT;"
        )
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(2,)]
        db.execute_script("BEGIN; INSERT INTO t VALUES (3); ROLLBACK;")
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(2,)]
    with _make_db(path) as db:
        assert _rows(db, "SELECT id FROM t ORDER BY id") == [(1,), (2,)]


def test_default_isolation_can_be_changed(tmp_path: Path) -> None:
    with _make_db(tmp_path / "isolation.db") as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        assert db.transaction_state()["default_isolation"] == "read_committed"
        db.execute("BEGIN")
        txn = db.current_transaction()
        assert txn is not None and txn.isolation == "read_committed"
        db.execute("ROLLBACK")
        db.execute("BEGIN ISOLATION LEVEL SERIALIZABLE")
        txn = db.current_transaction()
        assert txn is not None and txn.isolation == "serializable"
        db.execute("ROLLBACK")


def test_transaction_state_reports_locks_and_wal(tmp_path: Path) -> None:
    with _make_db(tmp_path / "state.db") as db:
        db.execute("CREATE TABLE t (id INT)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (1)")
        state = db.transaction_state()
        current = state["current"]
        assert current is not None
        assert current["write_locks"] == ["t"]
        assert current["pages_touched"] >= 1
        assert state["locks"]["resources"]["t"][0]["mode"] == "X"
        assert state["wal"]["enabled"] is True
        db.execute("ROLLBACK")


def test_constraint_checks_do_not_self_deadlock(tmp_path: Path) -> None:
    """INSERT 会在校验唯一约束时扫表，不能和自己持有的 X 锁互锁。"""

    with _make_db(tmp_path / "reentrant.db") as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT UNIQUE)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (1, 10)")
        db.execute("INSERT INTO t VALUES (2, 20)")
        db.execute("COMMIT")
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(2,)]
