from pathlib import Path

import pytest

from yoursql.common import ExecutionError
from yoursql.engine.database import Database
from yoursql.sql.ast import BinaryOp, Literal
from yoursql.sql.plan import PlanNode


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
        assert [scan.kind for scan in scans] == ["SeqScan", "IndexScan"]
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
