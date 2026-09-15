"""TPC-H 所需 SQL 特性的执行回归：派生表 / CTE / CASE / CAST / EXISTS / 子查询表达式。

WHY：这些特性是补齐 Q2/Q4/Q7/Q8/Q9/Q12–Q22 的前置条件。它们跨 parser→binder→
planner→executor 四层，任何一层漏一处都会静默给出错误结果而不是报错，因此这里
按"结果必须对得上手算值"来固定行为，而不是只断言能跑通。

另外固定两条容易回归的语义：
- 别名会遮蔽原表名：``FROM t AS u`` 里的 ``t.id`` 必须解析到外层作用域；
- 带外层作用域的 WHERE 不下推到行视图，否则相关谓词会退化成恒真/恒假。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yoursql.engine.runtime.database import Database


@pytest.fixture()
def database(tmp_path: Path):
    with Database(tmp_path / "features.db") as instance:
        instance.execute("CREATE TABLE t(id INT, name VARCHAR);")
        instance.insert_rows("t", [(1, "10"), (2, "20"), (3, "30")])
        yield instance


def _rows(database: Database, sql: str) -> list[tuple]:
    return list(database.execute(sql).rows)


def test_derived_table_in_from(database: Database) -> None:
    assert _rows(
        database,
        "SELECT x.a FROM (SELECT id AS a FROM t WHERE id > 1) AS x ORDER BY x.a;",
    ) == [(2,), (3,)]
    assert _rows(database, "SELECT s.c FROM (SELECT count(*) AS c FROM t) AS s;") == [(3,)]


def test_cte_is_inlined_and_reusable(database: Database) -> None:
    assert _rows(
        database,
        "WITH big AS (SELECT id FROM t WHERE id > 1) SELECT id FROM big ORDER BY id;",
    ) == [(2,), (3,)]
    # 同一个 CTE 被引用两次（外层 FROM + IN 子查询）也必须能各自展开。
    assert _rows(
        database,
        "WITH big AS (SELECT id FROM t WHERE id > 1) "
        "SELECT b.id FROM big AS b WHERE b.id IN (SELECT id FROM big) ORDER BY b.id;",
    ) == [(2,), (3,)]


def test_case_expression_searched_and_simple(database: Database) -> None:
    assert _rows(
        database,
        "SELECT id, CASE WHEN id = 1 THEN 'one' WHEN id = 2 THEN 'two' ELSE 'other' END "
        "AS label FROM t ORDER BY id;",
    ) == [(1, "one"), (2, "two"), (3, "other")]
    assert _rows(
        database,
        "SELECT id, CASE id WHEN 1 THEN 'one' ELSE 'other' END AS label FROM t ORDER BY id;",
    ) == [(1, "one"), (2, "other"), (3, "other")]
    # CASE 嵌在聚合里（Q12/Q14 的写法）。
    assert _rows(
        database, "SELECT sum(CASE WHEN id > 1 THEN 1 ELSE 0 END) AS c FROM t;"
    ) == [(2,)]


def test_cast_expression_both_directions(database: Database) -> None:
    assert _rows(database, "SELECT CAST(id AS VARCHAR) AS s FROM t WHERE id = 2;") == [("2",)]
    assert _rows(database, "SELECT CAST(name AS INT) AS n FROM t WHERE id = 3;") == [(30,)]


def test_exists_and_not_exists_are_correlated(database: Database) -> None:
    assert _rows(
        database,
        "SELECT id FROM t WHERE EXISTS "
        "(SELECT 1 FROM t AS u WHERE u.id = t.id + 1) ORDER BY id;",
    ) == [(1,), (2,)]
    assert _rows(
        database,
        "SELECT id FROM t WHERE NOT EXISTS "
        "(SELECT 1 FROM t AS u WHERE u.id = t.id + 1) ORDER BY id;",
    ) == [(3,)]


def test_scalar_subquery_in_projection_is_correlated(database: Database) -> None:
    assert _rows(
        database,
        "SELECT id, (SELECT count(*) FROM t AS u WHERE u.id <= t.id) AS c FROM t ORDER BY id;",
    ) == [(1, 1), (2, 2), (3, 3)]


def test_scalar_and_in_subqueries_in_where(database: Database) -> None:
    assert _rows(database, "SELECT id FROM t WHERE id = (SELECT max(id) FROM t);") == [(3,)]
    assert _rows(database, "SELECT id FROM t WHERE id IN (SELECT id FROM t WHERE id > 2);") == [
        (3,)
    ]


def test_alias_hides_base_table_name_for_correlation(database: Database) -> None:
    """内层 ``t AS u`` 不能遮蔽外层 ``t``：``t.id`` 必须取外层行的值。"""

    assert _rows(
        database,
        "SELECT (SELECT max(u.id) FROM t AS u WHERE u.id <= t.id) AS biggest FROM t ORDER BY id;",
    ) == [(1,), (2,), (3,)]
    # 别名遮蔽原表名后，直接引用原表名应当报错而不是悄悄读到内层行。
    with pytest.raises(Exception):
        database.execute("SELECT id AS x FROM t AS u WHERE t.id = 1;")


def test_tpch_date_and_string_functions(database: Database) -> None:
    """Q7/Q8/Q9 用 ``CAST(STRFTIME('%Y', d) AS INTEGER)``，Q22 用 ``SUBSTRING``。"""

    assert _rows(
        database,
        "SELECT CAST(STRFTIME('%Y', DATE('1996-03-04')) AS INTEGER) AS y;",
    ) == [(1996,)]
    assert _rows(database, "SELECT SUBSTRING('13-123-4567', 1, 2) AS code;") == [("13",)]
    assert _rows(database, "SELECT SUBSTR('12345', 2) AS rest;") == [("2345",)]
    assert _rows(database, "SELECT SUBSTRING('12345', -2) AS tail;") == [("45",)]
    with pytest.raises(Exception):
        database.execute("SELECT STRFTIME('%Y', 'not-a-date');")


def test_empty_input_aggregate_follows_standard_semantics(database: Database) -> None:
    """空输入：无 GROUP BY 的聚合出一行 NULL，带 GROUP BY 的聚合出 0 行。"""

    assert _rows(database, "SELECT id, COUNT(*) FROM t WHERE id > 100;") == [(None, 0)]
    assert _rows(database, "SELECT COUNT(*), SUM(id) FROM t WHERE id > 100;") == [(0, None)]
    assert _rows(database, "SELECT id, SUM(id) FROM t WHERE id > 100 GROUP BY id;") == []
    # 派生表筛空后同样不能抛"找不到列"。
    assert _rows(
        database,
        "SELECT d.a, SUM(d.a) FROM (SELECT id AS a FROM t WHERE id > 100) AS d GROUP BY d.a;",
    ) == []


def test_self_join_key_survives_ambiguous_bare_names(database: Database) -> None:
    """自连接的连接键必须按限定符取值，不能退化成同名裸列名。

    WHY：``FROM t1, s AS x, s AS y, r WHERE x.b = r.k`` 里，``x``/``y`` 同名同列，
    上下文合并后裸列名 ``b`` 会被标成歧义值；而首表 ``t1`` 根本没有 ``b`` 列。
    一旦连接键退化成裸列名，哈希探测就静默失配、结果整片归零——TPC-H Q8 的
    ``nation AS n1, nation AS n2, region`` 正是这样把 29 行变成 0 行的。
    """

    database.execute("CREATE TABLE t1(id INT, note VARCHAR);")
    database.execute("CREATE TABLE s(a INT, b INT);")
    database.execute("CREATE TABLE r(k INT, name VARCHAR);")
    database.insert_rows("t1", [(1, "p"), (2, "q")])
    database.insert_rows("s", [(1, 10), (2, 10), (3, 20)])
    database.insert_rows("r", [(10, "a"), (20, "b")])
    query = (
        "SELECT COUNT(*) FROM t1, s AS x, s AS y, r "
        "WHERE x.b = r.k AND t1.id = x.a AND y.a = x.a;"
    )
    assert _rows(database, query) == [(2,)]
    # 别名互换（歧义列归属随之改变）也必须给出一致结果。
    assert _rows(
        database,
        "SELECT COUNT(*) FROM t1, s AS y, s AS x, r "
        "WHERE x.b = r.k AND t1.id = x.a AND y.a = x.a;",
    ) == [(2,)]
