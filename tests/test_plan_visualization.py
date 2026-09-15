"""查询计划可视化（Mermaid / DOT / HTML）与优化规则入口的回归。

WHY：计划可视化原先只在 Web 工作台里有；验收现场常常只有终端和浏览器。
这里固定"后端也能把计划画出来"这条能力：图结构正确、实体转义正确、
并且 CLI 与脚本两条入口都能直接产出可渲染文本/HTML。
"""

from __future__ import annotations

from pathlib import Path

from yoursql.cli import main
from yoursql.engine.runtime.database import Database

JOIN_SQL = (
    "SELECT e.id, d.name FROM emp AS e JOIN dept AS d ON e.dept_id = d.id "
    "WHERE e.salary > 5 AND e.active = TRUE ORDER BY e.id;"
)


def _seed(path: Path) -> None:
    with Database(path) as db:
        db.execute("CREATE TABLE dept(id INT, name VARCHAR);")
        db.execute("CREATE TABLE emp(id INT, dept_id INT, salary INT, active BOOLEAN);")
        db.execute("INSERT INTO dept VALUES (1, 'A'), (2, 'B');")
        db.execute("INSERT INTO emp VALUES (1, 1, 10, TRUE), (2, 2, 20, FALSE);")
        db.execute("CREATE INDEX idx_emp_dept ON emp (dept_id);")


def _count_nodes(plan) -> int:
    return 1 + sum(_count_nodes(child) for child in plan.children)


def _walk(plan):
    yield plan
    for child in plan.children:
        yield from _walk(child)


def test_mermaid_declares_every_node_before_its_edges(tmp_path: Path) -> None:
    with Database(tmp_path / "mermaid.db") as db:
        db.execute("CREATE TABLE dept(id INT, name VARCHAR);")
        db.execute("CREATE TABLE emp(id INT, dept_id INT, salary INT, active BOOLEAN);")
        plan = db.compile(JOIN_SQL).optimized_plan
        assert plan is not None

        lines = plan.to_mermaid().splitlines()
        assert lines[0] == "flowchart TD"
        node_lines = [line for line in lines if "-->" not in line]
        edge_lines = [line for line in lines if "-->" in line]
        assert len(node_lines) == 1 + _count_nodes(plan)
        # 树：边数 = 节点数 - 1，且父节点一定先声明。
        assert len(edge_lines) == _count_nodes(plan) - 1
        declared: set[str] = set()
        for line in lines[1:]:
            if "-->" in line:
                source, target = line.split("-->")
                assert source.strip() in declared
                assert target.strip() in declared
            else:
                declared.add(line.split("[")[0].strip())
        # 方向可切换，便于横向排版。
        assert plan.to_mermaid("LR").startswith("flowchart LR")


def test_mermaid_escapes_html_sensitive_characters(tmp_path: Path) -> None:
    with Database(tmp_path / "escape.db") as db:
        db.execute("CREATE TABLE notes(id INT, body VARCHAR);")
        db.execute("INSERT INTO notes VALUES (1, 'a&b');")
        plan = db.compile(
            "SELECT id FROM notes WHERE id > 1 OR body = 'say \"hi\" & bye';"
        ).optimized_plan
        assert plan is not None

        mermaid = plan.to_mermaid()
        assert "&gt;" in mermaid, "比较运算符必须转义，否则会被当成 Mermaid 语法"
        assert "#quot;" in mermaid, "双引号必须转义，否则会截断节点标签"
        assert "&amp;" in mermaid, "& 必须转义，且不能被二次转义成 &amp;amp;"
        assert "&amp;amp;" not in mermaid
        # 转义后仍是一份合法的 Mermaid 头部。
        assert mermaid.startswith("flowchart TD")


def test_dot_output_is_graphviz_compatible(tmp_path: Path) -> None:
    with Database(tmp_path / "dot.db") as db:
        db.execute("CREATE TABLE dept(id INT, name VARCHAR);")
        db.execute("CREATE TABLE emp(id INT, dept_id INT, salary INT, active BOOLEAN);")
        plan = db.compile(JOIN_SQL).optimized_plan
        assert plan is not None

        dot = plan.to_dot()
        assert dot.startswith("digraph YourSQLPlan {")
        assert dot.rstrip().endswith("}")
        assert dot.count("->") == _count_nodes(plan) - 1
        # DOT 用 \n 在标签里换行，而不是 Mermaid 的 <br/>。
        assert "\\n" in dot and "<br/>" not in dot


def test_label_lines_report_kind_then_properties_and_are_clipped(tmp_path: Path) -> None:
    with Database(tmp_path / "label.db") as db:
        db.execute("CREATE TABLE t(id INT, v VARCHAR);")
        plan = db.compile(
            "SELECT id FROM t WHERE v = 'a_very_long_literal_value_used_for_clipping';"
        ).optimized_plan
        assert plan is not None

        # 根是 Project，真正带谓词的是它下面的 Filter。
        filter_node = next(
            node for node in _walk(plan) if node.kind == "Filter"
        )
        labels = filter_node.label_lines()
        assert labels[0] == "Filter"
        assert any(line.startswith("predicate=") for line in labels)
        assert all(len(line) <= 60 for line in labels)
        # 只展示可读属性，不把整棵 AST 塞进标签。
        assert all(not line.startswith("statement=") for line in labels)


def test_html_export_is_self_contained(tmp_path: Path) -> None:
    from scripts.plan_visualize import main as visualize_main

    database_path = tmp_path / "viz.db"
    _seed(database_path)
    out = tmp_path / "plan.html"

    status = visualize_main(
        ["--database", str(database_path), "--sql", JOIN_SQL, "--format", "html", "--out", str(out)]
    )
    assert status == 0
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "flowchart TD" in html
    # Mermaid 源放在 script[type=text/plain] 里，保证实体不被 HTML 二次解码。
    assert '<script type="text/plain" id="mermaid-src">' in html
    assert "&gt;" in html


def test_cli_rules_listing_and_plan_rendering(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "cli.db"
    _seed(database_path)

    assert main(["--rules"]) == 0
    rules_text = capsys.readouterr().out
    assert "constant_folding" in rules_text
    assert "predicate_pushdown" in rules_text
    assert "7/7 条启用" in rules_text

    assert main(["--database", str(database_path), "--sql", JOIN_SQL, "--plan", "mermaid"]) == 0
    mermaid = capsys.readouterr().out
    assert mermaid.startswith("flowchart TD")
    assert "rules=" in mermaid, "EXPLAIN 应说明本次命中了哪些规则"

    assert main(["--database", str(database_path), "--sql", JOIN_SQL, "--plan", "json"]) == 0
    assert '"kind"' in capsys.readouterr().out

    assert main(["--database", str(database_path), "--sql", JOIN_SQL, "--plan", "text"]) == 0
    assert "SeqScan" in capsys.readouterr().out


def test_cli_disable_rule_changes_the_plan(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "cli-disabled.db"
    _seed(database_path)
    query = "SELECT id FROM emp WHERE dept_id = 1;"

    assert main(
        ["--database", str(database_path), "--sql", query, "--plan", "text"]
    ) == 0
    baseline = capsys.readouterr().out
    assert "IndexScan" in baseline and "pushed_predicate" in baseline

    assert main(
        [
            "--database",
            str(database_path),
            "--sql",
            query,
            "--plan",
            "text",
            "--disable-rule",
            "index_selection",
            "--disable-rule",
            "predicate_pushdown",
        ]
    ) == 0
    disabled = capsys.readouterr().out
    assert "IndexScan" not in disabled
    assert "pushed_predicate" not in disabled


def test_cli_plan_argument_errors_exit_nonzero(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "cli-errors.db"
    _seed(database_path)

    assert main(["--database", str(database_path), "--plan", "text"]) == 1
    assert "需要配合 --sql 或 --file" in capsys.readouterr().err

    assert main(
        ["--database", str(database_path), "--sql", "SELECT 1;", "--disable-rule", "nope"]
    ) == 1
    assert "未知的优化规则" in capsys.readouterr().err


def test_database_accepts_disabled_rules(tmp_path: Path) -> None:
    with Database(tmp_path / "db-rules.db", disabled_rules=("index_selection",)) as db:
        assert "index_selection" in db.optimizer.disabled_rules
        text = db.optimizer.explain_rules()
        assert "[关闭] index_selection" in text
        assert "6/7 条启用" in text
