"""流式执行、连接谓词下推与批量装载的回归测试。

WHY：这三项都是“看不见的行为”——结果对得上但性能可能悄悄退化回全量物化，
或者批量写页后数据不再与源一致，因此用可断言的指标（rows_examined、逐值比对、页数）固定住。
"""

from __future__ import annotations

from pathlib import Path

from yoursql.engine.runtime.database import Database


def _seed_orders(database: Database, rows: int = 400) -> None:
    database.execute("CREATE TABLE orders(id INT, customer_id INT, status VARCHAR, amount FLOAT);")
    database.insert_rows(
        "orders",
        [(index, index % 20, "paid" if index % 2 == 0 else "open", float(index)) for index in range(rows)],
    )


def test_limit_stops_scanning_early(tmp_path: Path) -> None:
    """无排序/去重/聚合时，LIMIT 应提前结束扫描而不是全表读完再截断。"""

    with Database(tmp_path / "limit.db") as database:
        _seed_orders(database)
        limited = database.execute("SELECT * FROM orders LIMIT 5;")
        assert len(limited.rows) == 5
        assert limited.stats["rows_examined"] == 5, limited.stats

        offset = database.execute("SELECT * FROM orders LIMIT 5 OFFSET 380;")
        assert len(offset.rows) == 5
        assert offset.stats["rows_examined"] == 385, offset.stats

        # 排序需要全量，因此仍然扫全表
        ordered = database.execute("SELECT * FROM orders ORDER BY id LIMIT 5;")
        assert len(ordered.rows) == 5
        assert ordered.stats["rows_examined"] == 400, ordered.stats


def test_join_pushes_single_table_predicates_to_scans(tmp_path: Path) -> None:
    """JOIN 查询里只引用单表的 WHERE 条件应下推到该表扫描。"""

    with Database(tmp_path / "pushdown.db") as database:
        database.execute("CREATE TABLE customers(id INT, name VARCHAR);")
        database.insert_rows("customers", [(index, f"c{index}") for index in range(200)])
        database.execute("CREATE TABLE orders(id INT, customer_id INT);")
        database.insert_rows("orders", [(index, index % 200) for index in range(2000)])

        sql = "SELECT c.name, o.id FROM customers AS c JOIN orders AS o ON o.customer_id = c.id WHERE c.id < 5;"
        result = database.execute(sql)
        # 左表被下推到 5 行，因此连接只做 5 × 2000 次候选比较，而不是 200 × 2000
        assert len(result.rows) == 50
        assert result.stats["rows_examined"] == 50, result.stats

        unfiltered = database.execute(
            "SELECT c.name, o.id FROM customers AS c JOIN orders AS o ON o.customer_id = c.id WHERE c.id < 5 OR o.id < 0;"
        )
        # OR 组不满足“只引用单表”，仍应给出正确结果（只是不回退为错误计划）
        assert len(unfiltered.rows) == 50


def test_batch_loading_keeps_rows_and_pages_consistent(tmp_path: Path) -> None:
    """批量写页后：行数、逐行取值、重开读取都要与源数据一致。"""

    source = [(index, f"value-{index}", float(index) * 1.25) for index in range(5000)]
    path = tmp_path / "batch.db"
    with Database(path) as database:
        database.execute("CREATE TABLE t(id INT, label VARCHAR, amount FLOAT);")
        assert database.insert_rows("t", source).affected_rows == len(source)
        table = database.catalog.get_table("t")
        heap = database._heap(table)
        read_back = [row for _row_id, row in heap.scan()]
        assert read_back == source
        page_count = len(heap.page_ids)

    with Database(path) as reopened:
        heap = reopened._heap(reopened.catalog.get_table("t"))
        assert [row for _row_id, row in heap.scan()] == source
        assert len(heap.page_ids) == page_count
        assert reopened.execute("SELECT count(*) FROM t;").rows == [(len(source),)]


def test_unique_constraints_still_enforced_with_batch_loading(tmp_path: Path) -> None:
    """有主键/唯一列的表在批量路径下必须拦住重复值，且不留下无索引条目的堆行。"""

    with Database(tmp_path / "batch-unique.db") as database:
        database.execute("CREATE TABLE t(id INT PRIMARY KEY, label VARCHAR);")
        database.insert_rows("t", [(1, "a"), (2, "b")])
        # 主键没有单列唯一索引：由装载期集合拦截，冲突批次不会写入
        try:
            database.insert_rows("t", [(3, "c"), (3, "d")])
        except Exception as error:  # noqa: BLE001
            assert "唯一约束冲突" in str(error)
        else:
            raise AssertionError("重复主键没有被拦截")
        assert database.execute("SELECT count(*) FROM t;").rows == [(2,)]

        # 有唯一索引时延迟到装载结束 bulk_load；冲突要让整次调用回滚
        database.execute("CREATE UNIQUE INDEX uq_t_id ON t (id);")
        try:
            database.insert_rows("t", [(4, "e"), (4, "f")])
        except Exception as error:  # noqa: BLE001
            assert "唯一" in str(error)
        else:
            raise AssertionError("唯一索引没有拦截重复值")
        assert database.execute("SELECT count(*) FROM t;").rows == [(2,)]
        assert database.execute("SELECT id FROM t WHERE id = 4;").rows == []
        heap = database._heap(database.catalog.get_table("t"))
        assert sorted(row[0] for _row_id, row in heap.scan()) == [1, 2]
