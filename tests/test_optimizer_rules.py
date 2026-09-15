from pathlib import Path

import pytest

from yoursql.common import ExecutionError
from yoursql.engine.runtime.database import Database
from yoursql.sql.ast import BinaryOp, Literal
from yoursql.planner.optimizer import Optimizer
from yoursql.planner.physical import PlanNode


def _scan_nodes(plan: PlanNode | None) -> list[PlanNode]:
    if plan is None:
        return []
    nodes = [plan] if plan.kind in {"SeqScan", "IndexScan"} else []
    for child in plan.children:
        nodes.extend(_scan_nodes(child))
    return nodes


def _find_node(plan: PlanNode | None, kind: str) -> PlanNode | None:
    if plan is None:
        return None
    if plan.kind == kind:
        return plan
    for child in plan.children:
        found = _find_node(child, kind)
        if found is not None:
            return found
    return None


def test_constant_folding_boolean_rules_and_constant_index_expression(tmp_path: Path) -> None:
    with Database(tmp_path / "optimizer-rules.db") as db:
        db.execute("CREATE TABLE items(id INT, active BOOLEAN, price INT);")
        db.execute("INSERT INTO items VALUES (1, TRUE, 10), (2, FALSE, 20), (3, TRUE, 30);")
        db.execute("CREATE INDEX idx_items_id ON items (id);")

        folded = db.compile("SELECT 1 + 2 AS value;").optimized_plan
        assert folded is not None
        item = folded.properties["items"][0]
        assert isinstance(item.expression, Literal)
        assert item.expression.value == 3
        assert db.execute("SELECT 1 + 2 AS value;").rows == [(3,)]

        simplified = db.compile(
            "SELECT id FROM items WHERE TRUE AND active = TRUE OR FALSE ORDER BY id;"
        ).optimized_plan
        assert simplified is not None
        predicate = simplified.children[0].children[0].properties["predicate"]
        assert isinstance(predicate, BinaryOp)
        assert predicate.operator == "="
        assert db.execute("SELECT id FROM items WHERE TRUE AND active = TRUE OR FALSE ORDER BY id;").rows == [(1,), (3,)]

        always_empty = db.compile("SELECT id FROM items WHERE FALSE;").optimized_plan
        assert always_empty is not None
        assert any(node.kind == "EmptyScan" for node in [always_empty, *always_empty.children])
        assert db.execute("SELECT id FROM items WHERE FALSE;").rows == []

        constant_index = db.execute("SELECT id FROM items WHERE id = 1 + 2;")
        assert constant_index.rows == [(3,)]
        # HOW：查询只引用索引键列，走覆盖索引直读（IndexOnlyScan）而非回表。
        assert constant_index.stats["operator"] == "IndexOnlyScan"


def test_inner_join_predicates_are_pushed_to_each_scan(tmp_path: Path) -> None:
    with Database(tmp_path / "predicate-pushdown.db") as db:
        db.execute("CREATE TABLE departments(id INT, name VARCHAR);")
        db.execute("CREATE TABLE employees(id INT, department_id INT, active BOOLEAN);")
        db.execute("INSERT INTO departments VALUES (1, 'Engineering'), (2, 'Sales');")
        db.execute("INSERT INTO employees VALUES (1, 1, TRUE), (2, 1, FALSE), (3, 2, TRUE);")
        db.execute("CREATE INDEX idx_departments_id ON departments (id);")

        sql = (
            "SELECT e.id, d.name FROM employees AS e "
            "JOIN departments AS d ON e.department_id = d.id "
            "WHERE e.active = TRUE AND d.id = 1 ORDER BY e.id;"
        )
        result = db.execute(sql)
        assert result.rows == [(1, "Engineering")]

        optimized = db.compile(sql).optimized_plan
        assert optimized is not None
        join = _find_node(optimized, "Join")
        assert join is not None
        assert all(child.kind == "Filter" for child in join.children)
        scans = _scan_nodes(optimized)
        # HOW：连接重排（join_reordering）会按行数决定驱动表，所以只断言两侧各走什么路径，
        # 不绑定先后顺序——本用例要固定的是"谓词被下推到每个扫描节点"。
        assert sorted(scan.kind for scan in scans) == ["IndexScan", "SeqScan"]
        assert all("pushed_predicate" in scan.properties for scan in scans)


def test_null_and_outer_join_semantics_are_not_changed_by_rewrites(tmp_path: Path) -> None:
    with Database(tmp_path / "optimizer-boundaries.db") as db:
        db.execute("CREATE TABLE left_table(id INT, flag BOOLEAN);")
        db.execute("CREATE TABLE right_table(left_id INT, label VARCHAR);")
        db.execute("INSERT INTO left_table VALUES (1, TRUE), (2, NULL);")
        db.execute("INSERT INTO right_table VALUES (1, 'matched');")

        assert db.execute("SELECT id FROM left_table WHERE NULL OR flag = TRUE ORDER BY id;").rows == [(1,)]
        assert db.execute("SELECT id FROM left_table WHERE NULL;").rows == []
        with pytest.raises(ExecutionError, match="除数不能为零"):
            db.execute("SELECT 1 / 0;")

        sql = (
            "SELECT l.id, r.label FROM left_table AS l "
            "LEFT JOIN right_table AS r ON l.id = r.left_id "
            "WHERE l.id = 2 ORDER BY l.id;"
        )
        assert db.execute(sql).rows == [(2, None)]
        plan = db.compile(sql).optimized_plan
        assert plan is not None
        assert any(node.kind == "Filter" for node in _all_nodes(plan))
        join = next(node for node in _all_nodes(plan) if node.kind == "Join")
        assert all(child.kind in {"SeqScan", "IndexScan"} for child in join.children)


def _all_nodes(plan: PlanNode | None) -> list[PlanNode]:
    if plan is None:
        return []
    return [plan, *[node for child in plan.children for node in _all_nodes(child)]]


# --- 算法规则框架：规则可枚举、可单独关闭、命中情况写进计划 ---


def test_rule_catalogue_is_enumerable() -> None:
    catalogue = Optimizer.rule_catalogue()
    names = [rule.name for rule in catalogue]

    assert len(names) >= 2, "验收要求至少两条可枚举的优化规则"
    assert len(names) == len(set(names)), "规则名不应重复"
    assert all(rule.summary.strip() for rule in catalogue)
    assert all(
        rule.stage in {"expression", "predicate", "access-path", "join", "cardinality"}
        for rule in catalogue
    )
    # 默认实例启用全部规则，顺序与清单一致。
    assert [rule.name for rule in Optimizer().enabled_rules] == names


def test_unknown_disabled_rule_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知的优化规则"):
        Optimizer(disabled_rules=("no_such_rule",))


def test_optimize_records_fired_rules_in_plan(tmp_path: Path) -> None:
    with Database(tmp_path / "fired-rules.db") as db:
        db.execute("CREATE TABLE items(id INT, active BOOLEAN, price INT);")
        db.execute("INSERT INTO items VALUES (1, TRUE, 10), (2, FALSE, 20), (3, TRUE, 30);")

        optimizer = Optimizer()
        folded = optimizer.optimize(
            db.compile("SELECT 1 + 2 AS value;").plan, index_columns={}
        )
        assert folded.properties["rules"] == ("constant_folding",)
        assert optimizer.last_fired_rules == ("constant_folding",)

        simplified = optimizer.optimize(
            db.compile(
                "SELECT id FROM items WHERE TRUE AND active = TRUE OR FALSE;"
            ).plan,
            index_columns={},
        )
        assert simplified.properties["rules"] == (
            "boolean_simplification",
            "predicate_pushdown",
        )

        # 恒假过滤直接改写成空扫描，谓词消除规则应被记录。
        empty = optimizer.optimize(
            db.compile("SELECT id FROM items WHERE FALSE;").plan, index_columns={}
        )
        assert empty.properties["rules"] == ("predicate_elimination",)
        assert any(node.kind == "EmptyScan" for node in _all_nodes(empty))


def test_disabling_a_rule_changes_the_plan(tmp_path: Path) -> None:
    with Database(tmp_path / "disabled-rules.db") as db:
        db.execute("CREATE TABLE items(id INT, active BOOLEAN, price INT);")
        db.execute("INSERT INTO items VALUES (1, TRUE, 10), (2, FALSE, 20), (3, TRUE, 30);")

        sql = "SELECT id FROM items WHERE id = 1;"
        logical = db.compile(sql).plan
        index_columns = {"items": {"id"}}

        with_index = Optimizer().optimize(logical, index_columns=index_columns)
        assert any(node.kind == "IndexScan" for node in _all_nodes(with_index))
        assert "index_selection" in with_index.properties["rules"]

        # 关掉访问路径规则后，同样的语句只能走顺序扫描，且规则不再出现在命中列表。
        without_index = Optimizer(disabled_rules=("index_selection",)).optimize(
            logical, index_columns=index_columns
        )
        assert not any(node.kind == "IndexScan" for node in _all_nodes(without_index))
        scans = [node for node in _all_nodes(without_index) if node.kind in {"SeqScan", "IndexScan"}]
        assert [scan.kind for scan in scans] == ["SeqScan"]
        assert "index_selection" not in without_index.properties["rules"]

        # 关掉下推后，谓词过滤保留在扫描节点之上，不再出现 pushed_predicate。
        no_push = Optimizer(disabled_rules=("predicate_pushdown",)).optimize(
            logical, index_columns=index_columns
        )
        assert "predicate_pushdown" not in no_push.properties.get("rules", ())
        assert all(
            "pushed_predicate" not in node.properties for node in _all_nodes(no_push)
        )


def test_disabled_rules_do_not_leak_between_optimizers(tmp_path: Path) -> None:
    with Database(tmp_path / "rule-isolation.db") as db:
        db.execute("CREATE TABLE items(id INT);")
        db.execute("INSERT INTO items VALUES (1);")
        logical = db.compile("SELECT id FROM items WHERE id = 1;").plan
        index_columns = {"items": {"id"}}

        restricted = Optimizer(disabled_rules=("index_selection",))
        restricted.optimize(logical, index_columns=index_columns)
        assert restricted.last_fired_rules == ("predicate_pushdown",)

        # 另一个实例不受影响：规则开关按实例（ContextVar 作用域）隔离。
        default = Optimizer()
        default.optimize(logical, index_columns=index_columns)
        assert "index_selection" in default.last_fired_rules


def test_fired_rules_survive_plan_cache_hit(tmp_path: Path) -> None:
    with Database(tmp_path / "rule-cache.db") as db:
        db.execute("CREATE TABLE items(id INT);")
        db.execute("INSERT INTO items VALUES (1);")
        logical = db.compile("SELECT 1 + 2 AS value;").plan

        optimizer = Optimizer()
        first = optimizer.optimize(logical, sql="SELECT 1 + 2 AS value;", index_columns={})
        assert first.properties["rules"] == ("constant_folding",)

        # 第二次命中计划缓存；命中规则应从缓存计划里读回，而不是丢失。
        optimizer.last_fired_rules = ()
        cached = optimizer.optimize(logical, sql="SELECT 1 + 2 AS value;", index_columns={})
        assert cached.properties["rules"] == ("constant_folding",)
        assert optimizer.last_fired_rules == ("constant_folding",)


def test_explain_rules_marks_disabled_state() -> None:
    text = Optimizer(disabled_rules=("predicate_pushdown",)).explain_rules()
    assert "6/7 条启用" in text
    assert "[关闭] predicate_pushdown" in text
    assert "[启用] constant_folding" in text
    assert "7/7 条启用" in Optimizer().explain_rules()


# --- limit_pushdown：限行下推 ---


def test_limit_pushdown_moves_limit_below_projection(tmp_path: Path) -> None:
    """投影不改变基数时，LIMIT 应越过投影贴近扫描（并在计划里留痕）。"""

    with Database(tmp_path / "limit-pushdown.db") as db:
        db.execute("CREATE TABLE items(id INT);")
        db.insert_rows("items", [(index,) for index in range(30)])

        sql = "SELECT id FROM items LIMIT 4;"
        plan = db.compile(sql).optimized_plan
        assert plan is not None
        assert plan.kind == "Project"
        assert plan.children[0].kind == "Limit"
        assert plan.children[0].properties["pushed"] is True
        assert "limit_pushdown" in plan.properties["rules"]
        assert db.execute(sql).rows == [(0,), (1,), (2,), (3,)]


def test_limit_pushdown_marks_top_n_for_sorted_limit(tmp_path: Path) -> None:
    """排序 + 限行只需要前 k 行 → Sort 上应标注 top_n，供运行时有界堆使用。"""

    with Database(tmp_path / "limit-topn.db") as db:
        db.execute("CREATE TABLE items(id INT, rank_no INT);")
        db.insert_rows("items", [(index, 30 - index) for index in range(30)])

        sql = "SELECT id FROM items ORDER BY rank_no LIMIT 4;"
        plan = db.compile(sql).optimized_plan
        assert plan is not None
        sort = _find_node(plan, "Sort")
        assert sort is not None
        assert sort.properties["top_n"] == 4
        assert "limit_pushdown" in plan.properties["rules"]
        assert db.execute(sql).rows == [(29,), (28,), (27,), (26,)]


def test_disabling_limit_pushdown_drops_top_n(tmp_path: Path) -> None:
    """关掉规则后计划里不再有 top_n——规则是可现场演示的。"""

    with Database(
        tmp_path / "limit-topn-off.db", disabled_rules=("limit_pushdown",)
    ) as db:
        db.execute("CREATE TABLE items(id INT, rank_no INT);")
        db.insert_rows("items", [(index, 30 - index) for index in range(30)])

        sql = "SELECT id FROM items ORDER BY rank_no LIMIT 4;"
        plan = db.compile(sql).optimized_plan
        assert plan is not None
        sort = _find_node(plan, "Sort")
        assert sort is not None
        assert "top_n" not in sort.properties
        assert "limit_pushdown" not in plan.properties.get("rules", ())
        assert db.execute(sql).rows == [(29,), (28,), (27,), (26,)]
