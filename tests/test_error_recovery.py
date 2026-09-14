"""错误恢复（多错误报告）回归：一次解析把脚本里的错误报全。

WHY：这是"高级扩展 · 编译器扩展"里的错误恢复项。它的价值全在"一条错误的解析器"
与"能报多条错误的解析器"之间的差别上，所以这里不测"能抛错"（旧行为已经覆盖），
而是测"一次调用能报几条、位置对不对、能用的语句有没有留下来"。

另外固定一条契约：既有 ``parse_one`` / ``parse_script`` 仍然首错即抛，
恢复能力只在 ``parse_recovering`` 这条新入口上生效。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yoursql.common.errors import LexerError, ParserError
from yoursql.engine.runtime.database import Database
from yoursql.sql.lexer import Lexer, tokenize
from yoursql.sql.parser import parse_one, parse_recovering, parse_script


def test_legacy_entry_points_still_fail_fast() -> None:
    """既有入口保持"首错即抛"，恢复模式不改变它们的契约。"""

    with pytest.raises(ParserError):
        parse_one("SELECT FROM WHERE")
    with pytest.raises(ParserError):
        parse_script("SELECT 1; SELECT FROM; SELECT 2;")
    with pytest.raises(LexerError):
        tokenize("SELECT @ FROM t;")
    # 非法字符不会悄悄消失：单错模式一定要吵。
    with pytest.raises(LexerError):
        Lexer("SELECT 1 # 2").tokenize()


def test_lexer_recovering_collects_every_bad_character() -> None:
    """词法错误逐个收集，而不是碰到第一个非法字符就收工。"""

    tokens, diagnostics = Lexer("SELECT @ FROM t; SELECT # FROM u;").tokenize_recovering()
    assert [(item.line, item.column) for item in diagnostics] == [(1, 8), (1, 25)]
    assert {item.stage for item in diagnostics} == {"lexer"}
    assert all(item.code == "LEXER_ERROR" for item in diagnostics)
    # 坏字符被跳过，其余 token 照常产出（两个 SELECT、两个 FROM）。
    assert sum(1 for token in tokens if token.lexeme.upper() == "SELECT") == 2


def test_recovering_reports_every_broken_statement() -> None:
    """脚本里每条坏语句都要报出来，好语句照常返回。"""

    outcome = parse_recovering(
        "SELECT id FROM t;\nSELECT FROM;\nSELCT 1;\nUPDATE t SET x = 1;"
    )
    assert not outcome.ok
    assert outcome.failed == 2
    assert [type(item).__name__ for item in outcome.statements] == ["Select", "Update"]
    assert [item.line for item in outcome.diagnostics] == [2, 3]
    assert all(item.stage == "parser" for item in outcome.diagnostics)


def test_recovering_keeps_parsable_items_of_a_broken_projection() -> None:
    """同一条 SELECT 里的多个坏投影项各自报错，好投影项仍然保留。"""

    outcome = parse_recovering("SELECT id, , name, , x FROM t;")
    assert outcome.failed == 2
    assert [item.column for item in outcome.diagnostics] == [12, 20]
    assert len(outcome.statements) == 1
    select = outcome.statements[0]
    assert [item.expression.name for item in select.items] == ["id", "name", "x"]
    assert select.from_table is not None and select.from_table.name == "t"


def test_recovering_never_returns_a_half_built_statement() -> None:
    """投影项全坏时不能吐出"空投影"的 AST，而应把这条语句判为失败。"""

    outcome = parse_recovering("SELECT ; SELECT 1;")
    assert not outcome.ok
    assert [type(item).__name__ for item in outcome.statements] == ["Select"]
    assert outcome.statements[0].items[0].expression.value == 1


def test_recovering_diagnostic_position_and_payload() -> None:
    """诊断要带行列与期望符号，并按位置排序，便于编辑器直接定位。"""

    outcome = parse_recovering("SELECT 1 FROM t;\nSELECT FROM;\nSELCT 2;")
    positions = [item.location for item in outcome.diagnostics]
    assert positions == sorted(positions, key=lambda item: item or (0, 0))
    assert [item.line for item in outcome.diagnostics] == [2, 3]
    rendered = str(outcome.diagnostics[0])
    assert rendered.startswith("[PARSER_ERROR]") and "line 2" in rendered
    assert outcome.diagnostics[0].expected is not None

    payload = outcome.as_dict()
    assert payload["ok"] is False
    assert payload["statements"] == 1
    assert isinstance(payload["diagnostics"], list)
    assert {"stage", "error", "message", "line", "column"} <= set(
        payload["diagnostics"][0]
    )


def test_recovering_mixes_lexer_and_parser_diagnostics_in_position_order() -> None:
    """词法与语法诊断合成一份，并按源码位置排好序。"""

    outcome = parse_recovering("SELECT 1 FROM t;\nSELECT $bad FROM u;\nSELCT 2;")
    assert {item.stage for item in outcome.diagnostics} == {"lexer", "parser"}
    assert [item.line for item in outcome.diagnostics] == [2, 3]
    # '$' 被跳过后，`SELECT bad FROM u` 本身是合法语句，因此仍然被保留。
    assert [type(item).__name__ for item in outcome.statements] == ["Select", "Select"]


def test_recovering_reports_missing_semicolon_once() -> None:
    """缺分号只报一次，不要被恢复动作放大成两条；后面那条语句照常解析。"""

    outcome = parse_recovering("SELECT 1 SELECT 2")
    assert outcome.failed == 1
    assert "分号" in outcome.diagnostics[0].message
    assert len(outcome.statements) == 2


def test_recovering_output_is_executable(tmp_path: Path) -> None:
    """恢复后留下的语句必须是能真跑的，否则"恢复"只是好看。"""

    with Database(tmp_path / "recovery.db") as database:
        database.execute("CREATE TABLE t(id INT, name VARCHAR);")
        database.insert_rows("t", [(1, "a"), (2, "b")])
        outcome = parse_recovering("SELECT id FROM t; SELECT FROM; SELECT name FROM t;")
        assert outcome.failed == 1
        assert len(outcome.statements) == 2
        assert [tuple(row) for row in database.execute("SELECT id FROM t;").rows] == [
            (1,),
            (2,),
        ]
        assert [tuple(row) for row in database.execute("SELECT name FROM t;").rows] == [
            ("a",),
            ("b",),
        ]


def test_recovering_script_is_clean_when_sql_is_clean() -> None:
    """没有错误时既不报诊断，也不要丢语句。"""

    outcome = parse_recovering("SELECT 1; UPDATE t SET x = 1; SELECT 2;")
    assert outcome.ok
    assert outcome.failed == 0
    assert len(outcome.statements) == 3
    assert outcome.report() == "解析通过，共 3 条语句。"


def test_parser_recovering_matches_parser_script_on_valid_input() -> None:
    """恢复模式在合法输入上与既有 ``parse_script`` 结果完全一致。"""

    sql = "SELECT a, b FROM t WHERE a > 1 ORDER BY b; SELECT count(*) FROM t;"
    recovered = parse_recovering(sql)
    assert recovered.ok
    assert list(recovered.statements) == parse_script(sql)


def test_recovering_survives_trailing_and_repeated_semicolons() -> None:
    """多余分号不该产生诊断，也不该打断恢复流程。"""

    outcome = parse_recovering(";; SELECT 1;;; SELECT FROM;; SELECT 2;;")
    assert [type(item).__name__ for item in outcome.statements] == ["Select", "Select"]
    assert outcome.failed == 1


def test_panic_mode_does_not_swallow_the_next_valid_statement() -> None:
    """语句级同步点要精确落在下一条语句上，不能把后面的好语句一起吃掉。"""

    outcome = parse_recovering("SELECT 1 FROM; SELECT 2; SELECT 3;")
    assert [len(item.items) for item in outcome.statements] == [1, 1]
    assert outcome.failed == 1


def test_database_check_script_reports_all_errors_without_side_effects(
    tmp_path: Path,
) -> None:
    """``Database.check_script`` 是只读通道：报全错误，且不建表、不写入。"""

    with Database(tmp_path / "check.db") as database:
        outcome = database.check_script(
            "CREATE TABLE t(id INT);\nSELECT id FROM t;\nSELCT 1;"
        )
        assert outcome.failed == 1
        assert len(outcome.statements) == 2
        # 只检查、不执行：表不应该真的被建出来。
        with pytest.raises(Exception):
            database.execute("SELECT id FROM t;")


def test_cli_check_reports_all_errors_and_exits_nonzero(tmp_path: Path) -> None:
    """CLI ``--check`` 把错误一次列全，并以非零码退出（便于脚本化门禁）。"""

    from yoursql.cli import main

    script = tmp_path / "bad.sql"
    script.write_text("SELECT 1 FROM t;\nSELCT 2;\nSELECT @ FROM t;\n", encoding="utf-8")
    status = main(["--file", str(script), "--check"])
    assert status == 1

    clean = tmp_path / "ok.sql"
    clean.write_text("SELECT 1;\nSELECT 2;\n", encoding="utf-8")
    assert main(["--file", str(clean), "--check"]) == 0
