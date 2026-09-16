"""覆盖索引（CREATE INDEX ... INCLUDE）与 IndexOnlyScan 的回归测试。

WHY：覆盖索引把投影列写进索引条目，查询可以完全不读堆页；这条路径涉及
DDL 解析、目录持久化、叶页载荷维护（含分裂/合并/删除）与代价判定，任何一环
漏掉都会静默返回旧值或错误结果，因此逐项固定行为。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yoursql.common import CatalogError, DatabaseConfig
from yoursql.engine.runtime.database import Database


def _seed(database: Database) -> None:
    database.execute("CREATE TABLE orders(id INT PRIMARY KEY, status VARCHAR, amount FLOAT, note VARCHAR);")
    database.insert_rows(
        "orders",
        [(index, "paid" if index % 3 == 0 else "open", float(index) * 1.5, f"note-{index}") for index in range(120)],
    )


def test_covering_index_serves_query_without_heap_reads(tmp_path: Path) -> None:
    with Database(tmp_path / "covering.db") as database:
        _seed(database)
        database.execute("CREATE INDEX idx_status_cover ON orders (status) INCLUDE (id, amount);")
        result = database.execute("SELECT id, status, amount FROM orders WHERE status = 'paid';")
        assert len(result.rows) == 40
        assert result.stats["operator"] == "IndexOnlyScan"
        assert all(row[1] == "paid" for row in result.rows)

        # note 不在覆盖列内，应退回堆访问。
        heap_query = database.execute("SELECT note FROM orders WHERE status = 'open';")
        assert heap_query.stats["operator"] != "IndexOnlyScan"
        assert len(heap_query.rows) == 80


def test_covering_index_in_predicate_uses_point_probes(tmp_path: Path) -> None:
    """稀疏 IN 点查不能退化为最小值到最大值的索引范围扫描。"""

    path = tmp_path / "covering-in.db"
    config = DatabaseConfig(page_size=1024, buffer_pool_size=8, replacement_policy="2q")
    with Database(path, config=config) as database:
        database.execute("CREATE TABLE items(id INT PRIMARY KEY, payload VARCHAR);")
        database.insert_rows(
            "items",
            [(index, f"payload-{index}") for index in range(1, 1001)],
        )
        database.execute("CREATE INDEX idx_items_id ON items (id);")
        database.buffer_pool.reset_runtime()

        sql = "SELECT id FROM items WHERE id IN (1, 201, 401, 601, 801);"
        first = database.execute(sql)
        before_second = database.buffer_pool.stats()
        second = database.execute(sql)
        after_second = database.buffer_pool.stats()

        assert first.stats["operator"] == "IndexOnlyScan"
        assert second.rows == first.rows
        assert first.stats["page_reads"] < 20
        assert second.stats["cache_hits"] > 0
        assert after_second.hot_hits > before_second.hot_hits


def test_covering_index_stays_consistent_after_dml(tmp_path: Path) -> None:
    with Database(tmp_path / "covering-dml.db") as database:
        _seed(database)
        database.execute("CREATE INDEX idx_status_cover ON orders (status) INCLUDE (id, amount);")
        assert database.execute("SELECT amount FROM orders WHERE status = 'paid' AND id = 3;").rows == [(4.5,)]

        database.execute("UPDATE orders SET amount = 90.0 WHERE id = 3;")
        updated = database.execute("SELECT amount FROM orders WHERE status = 'paid' AND id = 3;")
        assert updated.rows == [(90.0,)]
        assert updated.stats["operator"] == "IndexOnlyScan"

        database.execute("INSERT INTO orders VALUES (1000, 'paid', 7.5, 'late');")
        assert database.execute("SELECT amount FROM orders WHERE status = 'paid' AND id = 1000;").rows == [(7.5,)]

        database.execute("DELETE FROM orders WHERE id = 1000;")
        assert database.execute("SELECT amount FROM orders WHERE status = 'paid' AND id = 1000;").rows == []


def test_covering_index_survives_reopen_with_many_leaves(tmp_path: Path) -> None:
    """行数足以让叶页分裂、合并时，覆盖列值必须始终与键保持平行。"""

    path = tmp_path / "covering-leaves.db"
    with Database(path) as database:
        database.execute("CREATE TABLE t(id INT, bucket INT, payload VARCHAR);")
        database.insert_rows("t", [(index, index % 7, f"v{index}") for index in range(400)])
        database.execute("CREATE INDEX idx_bucket_cover ON t (bucket) INCLUDE (id, payload);")
        expected = {index: f"v{index}" for index in range(400)}
        rows = database.execute("SELECT id, bucket, payload FROM t WHERE bucket = 3;").rows
        assert len(rows) == len([index for index in range(400) if index % 7 == 3])
        assert all(expected[row[0]] == row[2] and row[1] == 3 for row in rows)
        assert database.execute("SELECT id FROM t WHERE bucket = 3;").stats["operator"] == "IndexOnlyScan"

        database.execute("DELETE FROM t WHERE bucket = 3 AND id < 50;")
        after_delete = database.execute("SELECT id, payload FROM t WHERE bucket = 3;").rows
        assert all(row[0] >= 50 for row in after_delete)
        assert all(expected[row[0]] == row[1] for row in after_delete)

    with Database(path) as reopened:
        rows = reopened.execute("SELECT id, payload FROM t WHERE bucket = 5;").rows
        assert len(rows) == len([index for index in range(400) if index % 7 == 5])
        assert all(expected[row[0]] == row[1] for row in rows)


def test_composite_covering_index_matches_heap_results(tmp_path: Path) -> None:
    """联合覆盖索引的等值与范围查询必须与堆访问结果一致。

    WHY：首列等值 + 联合索引时，若用全键范围比较（`(1,'paid')` vs 上界 `(1,)`）会被判为越界，
    导致覆盖路径静默返回空集；这里逐种谓词对比两条路径的结果。
    """

    with Database(tmp_path / "composite-cover.db") as database:
        database.execute("CREATE TABLE events(id INT, tenant INT, kind VARCHAR, amount FLOAT, extra VARCHAR);")
        database.insert_rows(
            "events",
            [(index, index % 4, ["click", "view", "buy"][index % 3], float(index), f"x{index}") for index in range(200)],
        )
        database.execute("CREATE INDEX idx_tenant_kind ON events (tenant, kind) INCLUDE (id, amount);")

        def heap_rows(where: str) -> list[tuple[object, ...]]:
            # 多选一列未覆盖列，强制走堆路径，再丢掉该列以便比较。
            result = database.execute(f"SELECT id, tenant, kind, amount, extra FROM events WHERE {where};")
            return sorted(tuple(row[:4]) for row in result.rows)

        def covered_rows(where: str) -> tuple[list[tuple[object, ...]], str]:
            result = database.execute(f"SELECT id, tenant, kind, amount FROM events WHERE {where};")
            return sorted(tuple(row) for row in result.rows), str(result.stats["operator"])

        for where in ("tenant = 1", "tenant = 2 AND kind = 'buy'", "tenant BETWEEN 1 AND 2", "kind = 'click'"):
            expected = heap_rows(where)
            actual, operator = covered_rows(where)
            assert actual == expected, f"{where} 两条路径结果不一致：{len(actual)} vs {len(expected)}"
            if where != "kind = 'click'":  # 首列无约束时不可用索引
                assert operator == "IndexOnlyScan", f"{where} 未走覆盖索引直读：{operator}"
            assert len(actual) > 0


def test_covering_index_rejects_bad_include_columns(tmp_path: Path) -> None:
    with Database(tmp_path / "covering-errors.db") as database:
        _seed(database)
        with pytest.raises(CatalogError):
            database.execute("CREATE INDEX idx_bad ON orders (status) INCLUDE (missing_column);")
        with pytest.raises(CatalogError):
            database.execute("CREATE INDEX idx_dup ON orders (status) INCLUDE (status);")
        assert [index.name for index in database.catalog.indexes()] == []
