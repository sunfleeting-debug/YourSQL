"""扫描快路径与列裁剪的回归测试。

WHY：这一轮优化改的是"看不见的行为"——`COUNT(*)` 不再逐行解码、`SELECT *` 之外的
投影不再把全部列塞进行上下文。结果对得上不代表语义没变，所以这里把三条底线固定住：

1. 只关心行数的查询（`COUNT(*)` / `SELECT 常量`）结果必须与逐行扫描完全一致，
   包括删除行之后、空表、交叉连接与分组计数；
2. 仍然需要列值的查询不能误走快路径——带 WHERE、`SELECT *`、`GROUP BY` 列
   都必须拿到真实列值；
3. 存储层按槽目录计数（`TableHeap.count`）必须与逐行扫描（`scan`）永远一致。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from yoursql.engine.runtime.database import Database
from yoursql.storage import BufferPool, DiskManager, TableHeap


def _seed(database: Database, rows: int = 300) -> None:
    """建一张带 DECIMAL 列的表并灌入数据，覆盖"需要/不需要还原定点数"两条路径。"""

    database.execute(
        "CREATE TABLE lineitem("
        "l_id INT, l_quantity DECIMAL, l_price DECIMAL, l_shipdate VARCHAR, l_comment VARCHAR);"
    )
    database.insert_rows(
        "lineitem",
        [
            (
                index,
                Decimal(index % 50),
                Decimal(f"{(index % 7) * 1.5:.2f}"),
                f"1994-01-{index % 28 + 1:02d}",
                f"note-{index % 5}",
            )
            for index in range(rows)
        ],
    )


def test_count_star_matches_row_count_and_scan_stats(tmp_path: Path) -> None:
    """`COUNT(*)` 走槽目录快路径，但结果与 rows_examined 语义都不能变。"""

    with Database(tmp_path / "count_star.db") as database:
        _seed(database, 300)
        result = database.execute("SELECT COUNT(*) FROM lineitem;")
        assert result.rows == [(300,)]
        assert result.stats["rows_examined"] == 300, result.stats
        # 快路径仍是全表扫描（只是不解码），不能报成索引访问
        assert result.stats["operator"] == "SeqScan", result.stats


def test_count_star_ignores_deleted_rows(tmp_path: Path) -> None:
    """按槽目录计数必须只数活记录，删除后要与逐行扫描同口径。"""

    with Database(tmp_path / "count_delete.db") as database:
        _seed(database, 200)
        database.execute("DELETE FROM lineitem WHERE l_id < 40;")
        result = database.execute("SELECT COUNT(*) FROM lineitem;")
        assert result.rows == [(160,)]
        scanned = len(database.execute("SELECT l_id FROM lineitem;").rows)
        assert scanned == 160


def test_count_star_with_where_is_still_filtered(tmp_path: Path) -> None:
    """带 WHERE 时必须逐个判断谓词，不能走"只数槽"的快路径。"""

    with Database(tmp_path / "count_where.db") as database:
        _seed(database, 300)
        expected = len(
            database.execute("SELECT l_id FROM lineitem WHERE l_quantity < 10;").rows
        )
        assert expected == 60
        filtered = database.execute(
            "SELECT COUNT(*) FROM lineitem WHERE l_quantity < 10;"
        )
        assert filtered.rows == [(60,)]

        # 恒真谓词同样要走过滤路径，结果仍是全表行数
        always_true = database.execute("SELECT COUNT(*) FROM lineitem WHERE 1 = 1;")
        assert always_true.rows == [(300,)]


def test_select_constant_yields_one_row_per_row(tmp_path: Path) -> None:
    """`SELECT 常量 FROM t` 不需要任何列值，但行数必须与表一致。"""

    with Database(tmp_path / "select_const.db") as database:
        _seed(database, 120)
        result = database.execute("SELECT 1 FROM lineitem;")
        assert len(result.rows) == 120
        assert set(result.rows) == {(1,)}
        assert result.stats["rows_examined"] == 120, result.stats

        limited = database.execute("SELECT 1 FROM lineitem LIMIT 5;")
        assert len(limited.rows) == 5


def test_select_star_still_expands_every_column(tmp_path: Path) -> None:
    """`SELECT *` 必须退回全列，不能因为列裁剪丢掉任何一列。"""

    with Database(tmp_path / "select_star.db") as database:
        _seed(database, 40)
        result = database.execute("SELECT * FROM lineitem;")
        assert len(result.columns) == 5
        assert len(result.rows) == 40
        row = result.rows[0]
        assert row[0] == 0
        assert isinstance(row[1], Decimal)
        assert row[2] == Decimal("0.00")
        assert row[3] == "1994-01-01"


def test_group_by_and_having_still_see_group_columns(tmp_path: Path) -> None:
    """`COUNT(*)` 不再需要全列，但 GROUP BY / HAVING 引用的列必须仍在上下文里。"""

    with Database(tmp_path / "group_having.db") as database:
        _seed(database, 300)
        grouped = database.execute(
            "SELECT l_comment, COUNT(*) FROM lineitem GROUP BY l_comment ORDER BY l_comment;"
        )
        assert grouped.rows == [
            ("note-0", 60),
            ("note-1", 60),
            ("note-2", 60),
            ("note-3", 60),
            ("note-4", 60),
        ]

        having = database.execute(
            "SELECT l_comment, COUNT(*) AS n FROM lineitem GROUP BY l_comment HAVING COUNT(*) > 59 ORDER BY l_comment;"
        )
        assert len(having.rows) == 5

        having_filtered = database.execute(
            "SELECT l_comment, COUNT(*) AS n FROM lineitem GROUP BY l_comment HAVING COUNT(*) > 60;"
        )
        assert having_filtered.rows == []


def test_count_star_over_cross_join_multiplies(tmp_path: Path) -> None:
    """无列引用的交叉连接：两侧都走快路径，乘积行数仍要正确。"""

    with Database(tmp_path / "cross_join.db") as database:
        database.execute("CREATE TABLE a(x INT);")
        database.insert_rows("a", [(index,) for index in range(30)])
        database.execute("CREATE TABLE b(y INT);")
        database.insert_rows("b", [(index,) for index in range(20)])

        assert database.execute("SELECT COUNT(*) FROM a, b;").rows == [(600,)]
        assert database.execute("SELECT COUNT(*) FROM a JOIN b ON a.x = b.y;").rows == [
            (20,)
        ]


def test_count_star_on_empty_table_is_zero(tmp_path: Path) -> None:
    """空表（含建表后未插入、全删两种）都必须返回 0 而不是跳过分组。"""

    with Database(tmp_path / "empty.db") as database:
        database.execute("CREATE TABLE blank(id INT);")
        assert database.execute("SELECT COUNT(*) FROM blank;").rows == [(0,)]
        assert database.execute("SELECT 1 FROM blank;").rows == []

        _seed(database, 10)
        database.execute("DELETE FROM lineitem;")
        assert database.execute("SELECT COUNT(*) FROM lineitem;").rows == [(0,)]


def test_column_aggregates_keep_exact_values(tmp_path: Path) -> None:
    """列裁剪之后，COUNT(列) / SUM(列) / AVG(列) 的取值仍要精确（DECIMAL 不漂移）。"""

    with Database(tmp_path / "column_agg.db") as database:
        _seed(database, 200)
        assert database.execute("SELECT COUNT(l_quantity) FROM lineitem;").rows == [
            (200,)
        ]
        assert database.execute("SELECT MIN(l_quantity), MAX(l_quantity) FROM lineitem;").rows == [
            (Decimal(0), Decimal(49))
        ]
        total = database.execute("SELECT SUM(l_price) FROM lineitem;").rows[0][0]
        expected = sum(Decimal(f"{(index % 7) * 1.5:.2f}") for index in range(200))
        assert total == expected

        nulls = database.execute(
            "SELECT COUNT(l_comment) FROM lineitem WHERE l_comment IS NULL;"
        )
        assert nulls.rows == [(0,)]


def test_table_heap_count_matches_scan(tmp_path: Path) -> None:
    """存储层：按槽目录计数必须与逐行扫描永远同口径（含删除槽）。"""

    with DiskManager(tmp_path / "heap.db") as disk:
        buffer = BufferPool(disk)
        heap = TableHeap(buffer)
        rows = [(index, f"row-{index}") for index in range(500)]
        row_ids = heap.append_batch(rows)

        assert heap.count() == 500
        assert sum(1 for _row_id, _row in heap.scan()) == 500

        for row_id in row_ids[:120]:
            assert heap.delete(row_id)
        # 删除槽留空，但不应再计入行数
        assert heap.count() == 380
        assert sum(1 for _row_id, _row in heap.scan()) == 380
        # 计数不解码，因此抽查一条仍然要能正常读回
        assert heap.read(row_ids[-1]) == rows[-1]
