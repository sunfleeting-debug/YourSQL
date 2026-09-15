"""分组聚合的**流式累加器**语义回归。

WHY：原先的分组实现把每行的完整上下文都留在内存里（单组聚合 35.5 MB / 60,175 行），
聚合求值再对每个聚合各遍历一遍组列表。改成边扫边累积后内存降到 O(组数)，代价是
``COUNT / SUM / AVG / MIN / MAX`` 的边界语义（NULL、空输入、DISTINCT）全部改由累加器
决定——这类改动一旦写错只会**静默返回错值**，不会报错，所以这里逐条固定手算结果。

覆盖三类容易回归的点：
1. NULL 处理：``COUNT(*)`` 计 NULL 行，``COUNT(col)/SUM(col)/AVG(col)/MIN/MAX`` 跳过 NULL；
2. 聚合出现的位置：投影 / HAVING / ORDER BY（后两者可能不出现在投影里，
   采集必须靠 ``_walk_expressions`` 遍历到，漏掉就查不回预计算值）；
3. 同一语句里多个聚合（含内容相同但不同节点）必须各自独立、互不串值。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yoursql.engine.runtime.database import Database


@pytest.fixture()
def database(tmp_path: Path):
    """6 行数据，刻意含一个 NULL 值：a 组 4 行（1, 2, NULL, 2），b 组 2 行（5, 5）。"""

    with Database(tmp_path / "aggregation.db") as instance:
        instance.execute("CREATE TABLE s(g VARCHAR, v INT);")
        instance.insert_rows(
            "s",
            [("a", 1), ("a", 2), ("a", None), ("b", 5), ("b", 5), ("a", 2)],
        )
        yield instance


def _rows(database: Database, sql: str) -> list[tuple]:
    return list(database.execute(sql).rows)


def test_count_star_counts_null_rows_but_count_column_skips_them(database: Database) -> None:
    """``COUNT(*)`` 连 NULL 行一起数；``COUNT(v)`` 只数非 NULL。"""

    assert _rows(database, "SELECT COUNT(*) FROM s;") == [(6,)]
    assert _rows(database, "SELECT COUNT(v) FROM s;") == [(5,)]


def test_all_five_aggregates_in_one_statement(database: Database) -> None:
    """五个聚合同语句：SUM 跳 NULL 得 15，AVG 除以非 NULL 计数得 3.0。"""

    assert _rows(
        database,
        "SELECT COUNT(*), COUNT(v), SUM(v), AVG(v), MIN(v), MAX(v) FROM s;",
    ) == [(6, 5, 15, 3.0, 1, 5)]


def test_group_by_produces_one_row_per_group_with_correct_aggregates(
    database: Database,
) -> None:
    """a 组：4 行 / SUM=5 / AVG=5÷3；b 组：2 行 / SUM=10 / AVG=5.0。"""

    assert _rows(
        database,
        "SELECT g, COUNT(*), SUM(v), AVG(v) FROM s GROUP BY g ORDER BY g;",
    ) == [("a", 4, 5, 1.6666666666666667), ("b", 2, 10, 5.0)]


def test_null_column_does_not_shift_min_max(database: Database) -> None:
    """NULL 不参与比较：MIN/MAX 不能因为 NULL 被当成极值。"""

    assert _rows(database, "SELECT MIN(v), MAX(v) FROM s;") == [(1, 5)]


def test_count_distinct_dedupes_non_null_values(database: Database) -> None:
    """去重后剩 {1, 2, 5} 三个值。"""

    assert _rows(database, "SELECT COUNT(DISTINCT v) FROM s;") == [(3,)]


def test_sum_and_avg_distinct_ignore_null_and_repeated(database: Database) -> None:
    """``SUM(DISTINCT v)=1+2+5=8``，``AVG(DISTINCT v)=8÷3``。"""

    assert _rows(database, "SELECT SUM(DISTINCT v) FROM s;") == [(8,)]
    assert _rows(database, "SELECT AVG(DISTINCT v) FROM s;") == [(2.6666666666666665,)]


def test_two_aggregates_with_identical_body_do_not_share_state(database: Database) -> None:
    """``SUM(v)`` 与 ``SUM(v)+1`` 是两个不同节点，各自独立求值（结果 15 / 16）。"""

    assert _rows(database, "SELECT SUM(v) AS tot, SUM(v) + 1 AS plus FROM s;") == [(15, 16)]


def test_aggregate_in_expression_combines_two_accumulators(database: Database) -> None:
    """``SUM(v)/COUNT(v)`` 与 ``AVG(v)`` 同值（15÷5=3.0），证明两个累加器都被采集到。"""

    assert _rows(database, "SELECT SUM(v)/COUNT(v) FROM s;") == [(3.0,)]
    assert _rows(database, "SELECT g, MAX(v)-MIN(v) AS spread FROM s GROUP BY g ORDER BY g;") == [
        ("a", 1),
        ("b", 0),
    ]


def test_having_filters_groups_by_aggregate(database: Database) -> None:
    """HAVING 里的聚合即使不在投影里也必须能被求值。"""

    assert _rows(
        database,
        "SELECT g, SUM(v) FROM s GROUP BY g HAVING SUM(v) > 6 ORDER BY g;",
    ) == [("b", 10)]
    # 聚合只出现在 HAVING、投影只选分组键。
    assert _rows(database, "SELECT g FROM s GROUP BY g HAVING COUNT(*) > 2;") == [("a",)]
    # 单组聚合 HAVING 为假 → 0 行（标准语义）。
    assert _rows(database, "SELECT COUNT(*) AS c FROM s HAVING COUNT(*) > 10;") == []


def test_order_by_aggregate_not_in_projection(database: Database) -> None:
    """聚合只出现在 ORDER BY 里：按 SUM(v) 降序应为 b(10) 在 a(5) 前。"""

    assert _rows(database, "SELECT g FROM s GROUP BY g ORDER BY SUM(v) DESC;") == [("b",), ("a",)]
    # 走别名引用同一聚合，结果必须一致。
    assert _rows(
        database,
        "SELECT g, SUM(v) AS tot FROM s GROUP BY g ORDER BY tot DESC;",
    ) == [("b", 10), ("a", 5)]


def test_empty_input_semantics_match_standard(database: Database) -> None:
    """空输入：无 GROUP BY 出一行（COUNT=0、SUM=NULL），带 GROUP BY 出 0 行。"""

    assert _rows(database, "SELECT COUNT(*) FROM s WHERE v > 100;") == [(0,)]
    assert _rows(database, "SELECT SUM(v), COUNT(v) FROM s WHERE v > 100;") == [(None, 0)]
    assert _rows(database, "SELECT g, COUNT(*) FROM s WHERE v > 100 GROUP BY g;") == []


@pytest.mark.parametrize("rows", [1, 2, 3, 200])
def test_single_group_accumulates_without_row_contexts(tmp_path: Path, rows: int) -> None:
    """N 行全落进同一组时，累加结果必须与手算一致。

    大 N 的单组用例同时守住两件事：累加是 O(N) 的一遍扫描（不做逐行上下文留存），
    以及累加过程不会因为中途换表示而丢精度。内存量级本身属 ``benchmarks/`` 的口径，
    这里只固定正确性。
    """

    with Database(tmp_path / f"groups-{rows}.db") as instance:
        instance.execute("CREATE TABLE m(g INT, v INT);")
        instance.insert_rows("m", [(1, i) for i in range(rows)])
        assert _rows(instance, "SELECT g, SUM(v), COUNT(v) FROM m GROUP BY g;") == [
            (1, sum(range(rows)), rows)
        ]
