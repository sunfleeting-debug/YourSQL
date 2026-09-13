import pytest

from yoursql.common import LexerError, ParserError
from yoursql.sql import (
    BinaryOp,
    Compiler,
    CreateRole,
    CreateUser,
    CreateIndex,
    CreateTable,
    CreateView,
    DropView,
    FunctionCall,
    Grant,
    Insert,
    Lexer,
    Select,
    Show,
    ShowGrants,
    TokenKind,
    parse_script,
    parse_one,
)


def test_lexer_keeps_position_and_decodes_literals() -> None:
    tokens = Lexer("SELECT 'a''b', 1.5 -- comment\nFROM t;").tokenize()
    assert tokens[0].kind is TokenKind.SELECT
    assert tokens[1].literal == "a'b"
    assert next(token for token in tokens if token.kind is TokenKind.FLOAT).literal == 1.5
    assert next(token for token in tokens if token.kind is TokenKind.FROM).line == 2
    assert tokens[-1].kind is TokenKind.EOF


def test_lexer_reports_bad_input() -> None:
    with pytest.raises(LexerError) as error:
        Lexer("SELECT @").tokenize()
    assert error.value.column == 8


def test_parser_handles_course_core_statements() -> None:
    statements = parse_script(
        "CREATE TABLE student(id INT PRIMARY KEY, name VARCHAR, age INT);"
        "INSERT INTO student VALUES (1, 'Alice', 20);"
        "SELECT id, name FROM student WHERE age > 18;"
        "DELETE FROM student WHERE id = 1;"
    )
    assert isinstance(statements[0], CreateTable)
    assert isinstance(statements[1], Insert)
    assert isinstance(statements[2], Select)
    assert isinstance(statements[2].where, BinaryOp)


def test_insert_double_quoted_string_reports_single_quote_hint() -> None:
    with pytest.raises(ParserError, match="字符串请使用单引号"):
        parse_one('INSERT INTO student VALUES (444, "test")')


def test_parser_handles_plan_extensions() -> None:
    statement = parse_one("SELECT count(*) AS total FROM users u JOIN orders o ON u.id = o.user_id GROUP BY u.id HAVING count(*) > 1 ORDER BY total DESC LIMIT 5 OFFSET 2")
    assert isinstance(statement, Select)
    assert isinstance(statement.items[0].expression, FunctionCall)
    assert statement.joins[0].join_type == "INNER"
    assert statement.limit == 5
    assert statement.offset == 2

    index = parse_one("CREATE UNIQUE INDEX idx_users_id ON users (id)")
    assert isinstance(index, CreateIndex)
    assert index.unique

    view = parse_one("CREATE VIEW active_users AS SELECT id, name FROM users WHERE id > 0")
    assert isinstance(view, CreateView)
    assert view.query.from_table is not None and view.query.from_table.name == "users"
    assert view.definition_sql.startswith("SELECT id")
    assert isinstance(parse_one("DROP VIEW IF EXISTS active_users"), DropView)


def test_compiler_exposes_ast_and_explainable_plan() -> None:
    result = Compiler().compile("SELECT * FROM student WHERE age >= 18;")
    assert result.ast.__class__.__name__ == "Select"
    assert result.plan.kind == "Filter" or result.plan.kind == "Project"
    assert "children" in result.plan.to_dict()


def test_parser_reports_missing_separator_between_script_statements() -> None:
    with pytest.raises(ParserError):
        parse_script("SELECT 1 SELECT 2")


def test_parser_handles_permission_management_statements() -> None:
    statements = parse_script(
        "CREATE ROLE reader;"
        "CREATE USER alice IDENTIFIED BY 'secret' DEFAULT ROLE reader;"
        "GRANT SELECT, INSERT ON TABLE student TO USER alice;"
        "SHOW GRANTS FOR ROLE reader;"
    )
    assert isinstance(statements[0], CreateRole)
    assert isinstance(statements[1], CreateUser)
    assert statements[1].roles == ("reader",)
    assert isinstance(statements[2], Grant)
    assert statements[2].privileges == ("SELECT", "INSERT")
    assert statements[2].object_name == "student"
    assert statements[2].target_kind == "USER"
    assert isinstance(statements[3], ShowGrants)
    assert statements[3].target_kind == "ROLE"


def test_parser_handles_basic_catalog_show_statements() -> None:
    statements = parse_script(
        "DESC student;"
        "DESCRIBE student;"
        "SHOW COLUMNS FROM student;"
        "SHOW FIELDS IN student;"
        "SHOW CREATE TABLE student;"
        "SHOW INDEX FROM student;"
        "SHOW INDEXES IN student;"
    )
    assert all(isinstance(statement, Show) for statement in statements)
    assert [(statement.target, statement.object_name) for statement in statements] == [
        ("COLUMNS", "student"),
        ("COLUMNS", "student"),
        ("COLUMNS", "student"),
        ("COLUMNS", "student"),
        ("CREATE_TABLE", "student"),
        ("INDEX", "student"),
        ("INDEX", "student"),
    ]
