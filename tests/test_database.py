from pathlib import Path

import pytest

from yoursql.common import AuthorizationError, BinderError, CatalogError, ExecutionError
from yoursql.engine.runtime.database import Database
from yoursql.storage import PageType


def test_course_core_sql_and_restart_persistence(tmp_path: Path) -> None:
    path = tmp_path / "student.db"
    with Database(path) as db:
        db.execute("CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR, age INT);")
        db.execute("INSERT INTO student VALUES (1, 'Alice', 20);")
        db.execute("INSERT INTO student VALUES (2, 'Bob', 17);")
        result = db.execute("SELECT id, name FROM student WHERE age > 18 ORDER BY id;")
        assert result.columns == ("id", "name")
        assert result.rows == [(1, "Alice")]
        assert result.stats["operator"] == "SeqScan"
        db.execute("DELETE FROM student WHERE id = 1;")

    with Database(path) as db:
        result = db.execute("SELECT * FROM student ORDER BY id;")
        assert result.rows == [(2, "Bob", 17)]
        assert db.execute("SHOW TABLES;").rows == [("student",)]


def test_delete_all_rows_reclaims_heap_page_and_reuses_it(tmp_path: Path) -> None:
    path = tmp_path / "delete-reclaim.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t(id INT, name VARCHAR);")
        db.execute("INSERT INTO t VALUES (1, 'Alice'), (2, 'Bob');")
        table = db.catalog.get_table("t")
        page_id = int(table.page_ids[0])

        result = db.execute("DELETE FROM t;")

        assert result.affected_rows == 2
        assert table.page_ids == []
        assert table.first_page_id is None
        assert db.disk.read(page_id).page_type is PageType.FREE

        db.execute("INSERT INTO t VALUES (3, 'Carol');")
        assert [int(value) for value in table.page_ids] == [page_id]
        assert db.execute("SELECT * FROM t;").rows == [(3, "Carol")]

    with Database(path) as db:
        assert db.execute("SELECT * FROM t;").rows == [(3, "Carol")]


def test_update_constraints_and_persistence(tmp_path: Path) -> None:
    with Database(tmp_path / "updates.db") as db:
        db.execute("CREATE TABLE t(id INT PRIMARY KEY, value INT);")
        db.execute("INSERT INTO t VALUES (1, 10), (2, 20);")
        db.execute("UPDATE t SET value = value + 5 WHERE id = 1;")
        assert db.execute("SELECT value FROM t WHERE id = 1;").rows == [(15,)]

        with pytest.raises(ExecutionError):
            db.execute("INSERT INTO t VALUES (1, 99);")


def test_index_scan_and_aggregate(tmp_path: Path) -> None:
    with Database(tmp_path / "index.db") as db:
        db.execute("CREATE TABLE t(id INT, group_id INT, value INT);")
        db.execute("INSERT INTO t VALUES (1, 1, 10), (2, 1, 20), (3, 2, 30);")
        db.execute("CREATE INDEX idx_t_group ON t (group_id);")
        result = db.execute("SELECT id FROM t WHERE group_id = 1 ORDER BY id;")
        assert result.rows == [(1,), (2,)]
        assert result.stats["operator"] == "IndexScan"
        aggregate = db.execute("SELECT group_id, count(*) AS n, sum(value) AS total FROM t GROUP BY group_id ORDER BY group_id;")
        assert aggregate.rows == [(1, 2, 30), (2, 1, 30)]


def test_optimizer_plan_is_consumed_and_invalidated_by_index_changes(tmp_path: Path) -> None:
    with Database(tmp_path / "optimizer.db") as db:
        db.execute("CREATE TABLE t(id INT, value INT); INSERT INTO t VALUES (1, 10), (2, 20);")
        sql = "SELECT id FROM t WHERE id = 2"

        assert db.execute(sql).stats["operator"] == "SeqScan"
        assert len(db.optimizer.cache) == 1

        db.execute("CREATE INDEX idx_t_id ON t (id);")
        assert len(db.optimizer.cache) == 0
        # HOW：只引用索引键列的查询由索引直接回答（IndexOnlyScan），不再回表。
        assert db.execute(sql).stats["operator"] == "IndexOnlyScan"

        optimized = db.compile(sql).optimized_plan
        assert optimized is not None
        assert any(node.kind == "IndexScan" for node in db._scan_plans(optimized))

        db.execute("DROP INDEX idx_t_id;")
        assert len(db.optimizer.cache) == 0
        assert db.execute(sql).stats["operator"] == "SeqScan"


def test_large_index_scan_falls_back_for_low_selectivity(tmp_path: Path) -> None:
    """大表索引命中率过高时改用顺序扫描，空结果和稀疏结果仍走索引。"""

    with Database(tmp_path / "selectivity.db") as db:
        db.execute("CREATE TABLE orders(id INT, status VARCHAR);")
        db.insert_rows(
            "orders",
            ((index, "paid" if index < 160 else "rare" if index == 199 else "other") for index in range(200)),
        )
        db.execute("CREATE INDEX idx_orders_status ON orders (status);")

        broad = db.execute("SELECT * FROM orders WHERE status = 'paid';")
        assert len(broad.rows) == 160
        assert broad.stats["operator"] == "SeqScan"
        optimized = db.compile("SELECT * FROM orders WHERE status = 'paid';").optimized_plan
        assert optimized is not None
        assert all(node.kind != "IndexScan" for node in db._scan_plans(optimized))
        assert "SeqScan" in db.execute("EXPLAIN SELECT * FROM orders WHERE status = 'paid';").rows[0][0]

        rare = db.execute("SELECT * FROM orders WHERE status = 'rare';")
        assert rare.rows == [(199, "rare")]
        assert rare.stats["operator"] == "IndexScan"

        missing = db.execute("SELECT * FROM orders WHERE status = 'missing';")
        assert missing.rows == []
        assert missing.stats["operator"] == "IndexScan"
def test_date_function_supports_benchmark_modifiers(tmp_path: Path) -> None:
    with Database(tmp_path / "date.db") as db:
        result = db.execute(
            "SELECT DATE('1994-01-01') AS start_date, "
            "DATE('1994-01-01', '+1 year') AS next_year, "
            "DATE('1998-12-01', '-90 day') AS cutoff;"
        )
        assert result.rows == [("1994-01-01", "1995-01-01", "1998-09-02")]


def test_bulk_insert_rows_uses_schema_validation(tmp_path: Path) -> None:
    with Database(tmp_path / "bulk.db") as db:
        db.execute("CREATE TABLE t(id INT PRIMARY KEY, value FLOAT);")
        result = db.insert_rows("t", ((1, 1.5), (2, 2)))
        assert result.affected_rows == 2
        assert db.execute("SELECT * FROM t ORDER BY id;").rows == [(1, 1.5), (2, 2.0)]


def test_rbac_persists_users_roles_and_privileges(tmp_path: Path) -> None:
    path = tmp_path / "auth.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t(id INT);")
        db.rbac.create_role("reader")
        db.rbac.grant("SELECT t", role="reader")
        db.rbac.create_user("alice", "secret", roles=("reader",))

    with Database(path, user="alice", password="secret") as db:
        assert db.execute("SELECT * FROM t;").rows == []
        with pytest.raises(AuthorizationError):
            db.execute("INSERT INTO t VALUES (1);")


def test_rbac_is_stored_in_hidden_internal_tables(tmp_path: Path) -> None:
    path = tmp_path / "internal-auth.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t(id INT);")
        db.execute("CREATE ROLE reader;")
        db.execute("CREATE USER alice IDENTIFIED BY 'secret' DEFAULT ROLE reader;")
        db.execute("GRANT SELECT ON t TO ROLE reader;")

        assert "security" not in db.catalog.to_dict()
        assert db.execute("SHOW TABLES;").rows == [("t",)]
        assert {table.name for table in db.catalog.system_tables()} == {
            "_sys_users", "_sys_roles", "_sys_role_members", "_sys_privileges",
        }
        assert all(table.system for table in db.catalog.system_tables())
        assert all(table.row_count > 0 for table in db.catalog.system_tables())

        with pytest.raises(BinderError):
            db.execute("SELECT * FROM _sys_users;")

    with Database(path, user="alice", password="secret") as db:
        assert db.execute("SHOW GRANTS;").rows == [("alice", "SELECT T")]


def test_admin_can_query_safe_system_views(tmp_path: Path) -> None:
    path = tmp_path / "system-views.db"
    with Database(path) as db:
        db.execute("CREATE ROLE reader;")
        db.execute("CREATE USER alice IDENTIFIED BY 'secret' DEFAULT ROLE reader;")

        assert {name for (name,) in db.execute("SHOW VIEWS;").rows} == {
            "sys_users", "sys_roles", "sys_role_members", "sys_privileges",
        }
        users = db.execute("SELECT * FROM sys_users ORDER BY user_name;")
        assert users.columns == ("user_name",)
        assert users.rows == [("admin",), ("alice",)]
        assert db.execute("SELECT * FROM sys_roles ORDER BY role_name;").rows == [
            ("admin",), ("reader",),
        ]
        assert db.execute("SHOW CREATE VIEW sys_users;").rows[0][1].startswith(
            "CREATE VIEW sys_users AS SELECT user_name"
        )
        assert db.catalog.get_view("sys_users").system is True
        assert "password_hash" not in db.execute("DESC sys_users;").columns

        with pytest.raises(CatalogError):
            db.execute("DROP VIEW sys_users;")

    with Database(path) as db:
        assert db.execute("SELECT user_name FROM sys_users ORDER BY user_name;").rows == [
            ("admin",), ("alice",),
        ]

    with Database(path, user="alice", password="secret") as db:
        with pytest.raises(AuthorizationError):
            db.execute("SELECT * FROM sys_users;")


def test_sql_permission_management_and_enforcement(tmp_path: Path) -> None:
    path = tmp_path / "sql-auth.db"
    with Database(path) as db:
        db.execute("CREATE TABLE student(id INT);")
        assert db.execute("CREATE ROLE reader;").message == "CREATE ROLE reader"
        db.execute("CREATE USER alice IDENTIFIED BY 'secret' DEFAULT ROLE reader;")
        db.execute("CREATE USER bob IDENTIFIED BY 'secret';")
        db.execute("GRANT SELECT ON student TO ROLE reader;")
        db.execute("GRANT INSERT ON student TO USER alice;")
        assert db.execute("SHOW GRANTS FOR ROLE reader;").rows == [("reader", "SELECT STUDENT")]
        assert db.execute("SHOW GRANTS FOR USER alice;").rows == [("alice", "INSERT STUDENT"), ("alice", "SELECT STUDENT")]
        db.execute("REVOKE INSERT ON student FROM USER alice;")

    with Database(path, user="alice", password="secret") as db:
        assert db.execute("SHOW GRANTS;").rows == [("alice", "SELECT STUDENT")]
        assert db.execute("SELECT * FROM student;").rows == []
        assert db.execute("DESC student;").rows == [("id", "INT", "YES", "", None)]
        with pytest.raises(AuthorizationError):
            db.execute("INSERT INTO student VALUES (1);")
        with pytest.raises(AuthorizationError):
            db.execute("CREATE ROLE blocked;")

    with Database(path, user="bob", password="secret") as db:
        with pytest.raises(AuthorizationError):
            db.execute("SHOW COLUMNS FROM student;")


def test_subquery_union_and_range_index(tmp_path: Path) -> None:
    with Database(tmp_path / "query.db") as db:
        db.execute("CREATE TABLE t(id INT, value INT);")
        db.execute("INSERT INTO t VALUES (1, 10), (2, 20), (3, 30);")
        db.execute("CREATE INDEX idx_t_value ON t (value);")
        result = db.execute("SELECT id FROM t WHERE value >= 20 ORDER BY id;")
        assert result.rows == [(2,), (3,)]
        assert result.stats["operator"] == "IndexScan"
        subquery = db.execute("SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE value = 20);")
        assert subquery.rows == [(2,)]
        union = db.execute("SELECT id FROM t WHERE id = 1 UNION SELECT id FROM t WHERE id = 3;")
        assert union.rows == [(1,), (3,)]


def test_index_matching_supports_prefixes_and_compound_predicates(tmp_path: Path) -> None:
    """索引匹配覆盖联合索引最左前缀和常见复合谓词。"""

    with Database(tmp_path / "compound-index.db") as db:
        db.execute("CREATE TABLE events(id INT, tenant_id INT, score INT, label VARCHAR);")
        db.execute(
            "INSERT INTO events VALUES "
            "(1, 10, 20, 'a'), (2, 10, 30, 'b'), (3, 20, 20, 'c'), (4, 30, 40, 'd');"
        )
        db.execute("CREATE INDEX events_tenant_score ON events (tenant_id, score);")

        prefix = db.execute("SELECT id FROM events WHERE tenant_id = 10 ORDER BY id;")
        assert prefix.rows == [(1,), (2,)]
        assert prefix.stats["operator"] == "IndexScan"

        exact = db.execute("SELECT id FROM events WHERE tenant_id = 10 AND score = 30;")
        assert exact.rows == [(2,)]
        assert exact.stats["operator"] == "IndexScan"

        range_result = db.execute("SELECT id FROM events WHERE tenant_id = 10 AND score BETWEEN 20 AND 30 ORDER BY id;")
        assert range_result.rows == [(1,), (2,)]
        assert range_result.stats["operator"] == "IndexScan"

        in_result = db.execute("SELECT id FROM events WHERE tenant_id IN (10, 30) ORDER BY id;")
        assert in_result.rows == [(1,), (2,), (4,)]
        assert in_result.stats["operator"] == "IndexScan"

        or_result = db.execute("SELECT id FROM events WHERE tenant_id = 10 OR tenant_id = 30 ORDER BY id;")
        assert or_result.rows == [(1,), (2,), (4,)]
        assert or_result.stats["operator"] == "IndexScan"

        # 联合索引不能跳过最左列；仅按 score 过滤时仍应顺序扫描。
        skipped_prefix = db.execute("SELECT id FROM events WHERE score = 20 ORDER BY id;")
        assert skipped_prefix.rows == [(1,), (3,)]
        assert skipped_prefix.stats["operator"] == "SeqScan"


def test_read_only_view_persists_definition_and_results(tmp_path: Path) -> None:
    path = tmp_path / "view.db"
    with Database(path) as db:
        db.execute("CREATE TABLE users(id INT, name VARCHAR, active BOOLEAN);")
        db.execute("INSERT INTO users VALUES (1, 'Alice', TRUE), (2, 'Bob', FALSE);")
        db.execute("CREATE VIEW active_users AS SELECT id, name FROM users WHERE active = TRUE;")

        assert ("active_users",) in db.execute("SHOW VIEWS;").rows
        assert db.execute("SELECT name FROM active_users WHERE id > 0;").rows == [("Alice",)]
        assert db.execute("DESC active_users;").rows == [
            ("id", "INT", "YES", "", None),
            ("name", "VARCHAR", "YES", "", None),
        ]
        assert db.execute("SHOW CREATE VIEW active_users;").rows[0][1].startswith("CREATE VIEW active_users AS SELECT id")
        assert db.catalog.get_view("active_users").definition_sql.startswith("SELECT id")

        with pytest.raises(BinderError):
            db.execute("INSERT INTO active_users VALUES (3, 'Carol');")
        with pytest.raises(BinderError):
            db.execute("UPDATE active_users SET name = 'Carol';")
        with pytest.raises(BinderError):
            db.execute("DELETE FROM active_users;")
        with pytest.raises(BinderError):
            db.execute("CREATE INDEX idx_active_users ON active_users (id);")

    with Database(path) as db:
        assert db.execute("SELECT * FROM active_users ORDER BY id;").rows == [(1, "Alice")]
        db.execute("DROP VIEW active_users;")
        assert db.execute("SHOW VIEWS;").rows == [
            ("sys_privileges",), ("sys_role_members",), ("sys_roles",), ("sys_users",),
        ]


def test_drop_if_exists_syntax(tmp_path: Path) -> None:
    with Database(tmp_path / "drop.db") as db:
        db.execute("CREATE TABLE t(id INT);")
        assert db.execute("DROP TABLE IF EXISTS t;").message == "DROP TABLE t"
        assert db.execute("DROP TABLE IF EXISTS t;").message == "table t does not exist"


def test_join_distinct_functions_and_order_alias(tmp_path: Path) -> None:
    with Database(tmp_path / "join.db") as db:
        db.execute("CREATE TABLE users(id INT PRIMARY KEY, name VARCHAR);")
        db.execute("CREATE TABLE orders(id INT, user_id INT, amount INT);")
        db.execute("INSERT INTO users VALUES (1, 'Alice'), (2, 'Bob');")
        db.execute("INSERT INTO orders VALUES (10, 1, 8), (11, 1, 12);")
        joined = db.execute("SELECT u.name, o.amount FROM users u JOIN orders o ON u.id = o.user_id ORDER BY o.amount DESC;")
        assert joined.rows == [("Alice", 12), ("Alice", 8)]
        left = db.execute("SELECT u.id, o.amount FROM users u LEFT JOIN orders o ON u.id = o.user_id ORDER BY u.id, o.amount;")
        assert left.rows == [(1, 8), (1, 12), (2, None)]
        distinct = db.execute("SELECT DISTINCT upper(name) AS label FROM users ORDER BY label DESC;")
        assert distinct.rows == [("BOB",), ("ALICE",)]


def test_restart_and_unique_index(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    with Database(path) as db:
        db.execute("CREATE TABLE t(id INT, value INT);")
        db.execute("INSERT INTO t VALUES (1, 10);")
        db.execute("CREATE UNIQUE INDEX one_t_id ON t (id);")
        with pytest.raises(ExecutionError):
            db.execute("INSERT INTO t VALUES (1, 20);")
    with Database(path) as db:
        assert db.execute("SELECT * FROM t;").rows == [(1, 10)]
        db.execute("DROP INDEX one_t_id;")


def test_unique_index_allows_multiple_null_keys(tmp_path: Path) -> None:
    """唯一索引与列约束保持一致：NULL 不参与重复键冲突。"""

    with Database(tmp_path / "unique-null.db") as db:
        db.execute("CREATE TABLE t(id INT, value INT); INSERT INTO t VALUES (1, NULL), (2, NULL), (3, 7);")
        db.execute("CREATE UNIQUE INDEX one_value ON t (value);")
        db.execute("INSERT INTO t VALUES (4, NULL);")
        with pytest.raises(ExecutionError):
            db.execute("INSERT INTO t VALUES (5, 7);")


def test_type_and_column_errors_are_diagnosable(tmp_path: Path) -> None:
    with Database(tmp_path / "errors.db") as db:
        db.execute("CREATE TABLE t(id INT NOT NULL, name VARCHAR);")
        with pytest.raises(BinderError):
            db.execute("INSERT INTO t VALUES ('bad', 'x');")
        with pytest.raises(BinderError):
            db.execute("SELECT missing FROM t;")


def test_basic_catalog_show_commands_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "catalog-show.db"
    with Database(path) as db:
        db.execute("CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR NOT NULL DEFAULT 'guest', active BOOLEAN DEFAULT TRUE);")
        db.execute("CREATE UNIQUE INDEX idx_student_name ON student (name);")

        columns = db.execute("DESC student;")
        assert columns.columns == ("field", "type", "null", "key", "default")
        assert columns.rows == [
            ("id", "INT", "NO", "PRI", None),
            ("name", "VARCHAR", "NO", "", "guest"),
            ("active", "BOOLEAN", "YES", "", True),
        ]
        assert db.execute("SHOW FIELDS IN student;").rows == columns.rows
        assert db.execute("SHOW INDEXES FROM student;").rows == [
            ("student", "idx_student_name", True, "btree", "name")
        ]
        assert db.execute("SHOW CREATE TABLE student;").rows == [
            (
                "student",
                "CREATE TABLE student (id INT PRIMARY KEY, name VARCHAR NOT NULL DEFAULT 'guest', active BOOLEAN DEFAULT TRUE);",
            )
        ]

    with Database(path) as db:
        assert db.execute("DESCRIBE student;").rows == columns.rows
        assert db.execute("SHOW INDEX FROM student;").rows == [
            ("student", "idx_student_name", True, "btree", "name")
        ]
