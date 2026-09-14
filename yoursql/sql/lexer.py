"""SQL 词法分析器，输出带行列位置和解码字面量的 Token。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Iterator

from ..common.contracts import SqlValue
from ..common.errors import LexerError
from ..common.types import json_safe
from .diagnostics import Diagnostic


class TokenKind(str, Enum):
    """词种枚举；枚举值即词面，报错时可直接回显。

    HOW：关键字成员顺序与下方 `_KEYWORD_NAMES` 一致，增删关键字需同步两处。
    """

    # —— 结构标记与字面量 ——
    EOF = "EOF"
    IDENTIFIER = "IDENTIFIER"
    QUOTED_IDENTIFIER = "QUOTED_IDENTIFIER"
    INTEGER = "INTEGER"  # 十进制整数
    FLOAT = "FLOAT"  # 带小数点或指数的浮点数
    STRING = "STRING"  # '文本' 字面量
    STRING_LITERAL = "STRING"  # 别名：与 STRING 同值，兼容旧调用点的命名
    BOOLEAN = "BOOLEAN"  # TRUE / FALSE
    NULL = "NULL"  # NULL 字面量

    # —— 关键字（与 _KEYWORD_NAMES 同序） ——
    # 账号、角色与授权（DCL）
    CREATE = "CREATE"
    USER = "USER"
    ROLE = "ROLE"
    IDENTIFIED = "IDENTIFIED"
    GRANT = "GRANT"
    REVOKE = "REVOKE"
    TO = "TO"
    FOR = "FOR"
    GRANTS = "GRANTS"

    # 表 / 视图对象与数据语句（DDL / DML）
    TABLE = "TABLE"
    VIEW = "VIEW"
    VIEWS = "VIEWS"
    DROP = "DROP"
    INSERT = "INSERT"
    INTO = "INTO"
    VALUES = "VALUES"
    SELECT = "SELECT"
    FROM = "FROM"
    WHERE = "WHERE"
    DELETE = "DELETE"
    UPDATE = "UPDATE"
    SET = "SET"

    # 事务控制（TCL）
    BEGIN = "BEGIN"
    START = "START"
    COMMIT = "COMMIT"
    ROLLBACK = "ROLLBACK"
    TRANSACTION = "TRANSACTION"
    WORK = "WORK"
    ISOLATION = "ISOLATION"
    LEVEL = "LEVEL"
    READ = "READ"
    COMMITTED = "COMMITTED"
    SERIALIZABLE = "SERIALIZABLE"

    # 排序、分页、去重与描述
    ORDER = "ORDER"
    BY = "BY"
    ASC = "ASC"
    DESC = "DESC"
    DESCRIBE = "DESCRIBE"
    LIMIT = "LIMIT"
    OFFSET = "OFFSET"
    DISTINCT = "DISTINCT"

    # 别名与谓词
    AS = "AS"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    IS = "IS"
    LIKE = "LIKE"
    IN = "IN"
    BETWEEN = "BETWEEN"

    # 连接
    JOIN = "JOIN"
    INNER = "INNER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    FULL = "FULL"
    OUTER = "OUTER"
    ON = "ON"

    # 分组与集合运算
    GROUP = "GROUP"
    HAVING = "HAVING"
    UNION = "UNION"
    ALL = "ALL"

    # 条件表达式、CTE 与派生表
    CASE = "CASE"
    WHEN = "WHEN"
    THEN = "THEN"
    ELSE = "ELSE"
    END = "END"
    WITH = "WITH"
    CAST = "CAST"

    # 索引、执行计划与约束
    INDEX = "INDEX"
    EXPLAIN = "EXPLAIN"
    IF = "IF"
    EXISTS = "EXISTS"
    PRIMARY = "PRIMARY"
    KEY = "KEY"
    UNIQUE = "UNIQUE"
    DEFAULT = "DEFAULT"
    CONSTRAINT = "CONSTRAINT"
    NULLS = "NULLS"
    FIRST = "FIRST"
    LAST = "LAST"

    # SHOW 系列元数据查看
    SHOW = "SHOW"
    TABLES = "TABLES"
    COLUMNS = "COLUMNS"
    FIELDS = "FIELDS"
    INDEXES = "INDEXES"

    # —— 比较、算术与拼接运算符 ——
    EQ = "="
    ASSIGN = "="  # 别名：与 EQ 同值，赋值语法复用
    EQEQ = "=="
    NE = "!="
    NE2 = "<>"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    PLUS = "+"
    MINUS = "-"
    STAR = "*"
    SLASH = "/"
    PERCENT = "%"
    CONCAT = "||"

    # —— 标点与占位符 ——
    LPAREN = "("
    RPAREN = ")"
    COMMA = ","
    SEMICOLON = ";"
    DOT = "."
    QUESTION = "?"


TokenType = TokenKind

_KEYWORD_NAMES = (
    "CREATE USER ROLE IDENTIFIED BY GRANT REVOKE TO FOR GRANTS TABLE VIEW VIEWS DROP INSERT INTO VALUES"
    " SELECT FROM WHERE DELETE UPDATE SET ORDER BY ASC DESC DESCRIBE LIMIT OFFSET DISTINCT AS AND OR NOT IS LIKE IN"
    " BETWEEN JOIN INNER LEFT RIGHT FULL OUTER ON GROUP HAVING UNION ALL INDEX INDEXES EXPLAIN IF EXISTS PRIMARY KEY"
    " UNIQUE DEFAULT CONSTRAINT NULLS FIRST LAST SHOW TABLES COLUMNS FIELDS"
    " CASE WHEN THEN ELSE END WITH CAST"
    " BEGIN START COMMIT ROLLBACK TRANSACTION WORK ISOLATION LEVEL READ COMMITTED SERIALIZABLE"
).split()
KEYWORDS = {name: getattr(TokenKind, name) for name in _KEYWORD_NAMES}
_NUMBER_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


@dataclass(frozen=True)
class Token:
    """词种、源词素、位置和可选解码值。"""

    kind: TokenKind
    lexeme: str
    line: int
    column: int
    literal: SqlValue = None

    @property
    def type(self) -> TokenKind:
        return self.kind

    @property
    def value(self) -> SqlValue | str:
        return (
            self.literal
            if self.kind
            in {
                TokenKind.STRING,
                TokenKind.INTEGER,
                TokenKind.FLOAT,
                TokenKind.BOOLEAN,
                TokenKind.NULL,
                TokenKind.QUOTED_IDENTIFIER,
            }
            else self.lexeme
        )

    @property
    def text(self) -> str:
        return self.lexeme

    @property
    def position(self) -> tuple[int, int]:
        return self.line, self.column

    def as_tuple(self) -> tuple[TokenKind, str, int, int]:
        return self.kind, self.lexeme, self.line, self.column

    def __iter__(self) -> Iterator[object]:
        return iter(self.as_tuple())

    def __getitem__(self, index: int | slice) -> object:
        return self.as_tuple()[index]

    def __len__(self) -> int:
        return 4

    def as_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "kind": self.kind.value,
            "lexeme": self.lexeme,
            "line": self.line,
            "column": self.column,
        }
        if self.kind in {
            TokenKind.STRING,
            TokenKind.INTEGER,
            TokenKind.FLOAT,
            TokenKind.BOOLEAN,
            TokenKind.NULL,
            TokenKind.QUOTED_IDENTIFIER,
        }:
            # HOW：FLOAT 的 literal 是 Decimal，JSON 边界统一转成可序列化形式。
            data["value"] = json_safe(self.literal)
        return data


class Lexer:
    """只依赖标准库的 SQL 词法扫描器。"""

    _TWO_CHAR = {
        "<=": TokenKind.LE,
        ">=": TokenKind.GE,
        "!=": TokenKind.NE,
        "<>": TokenKind.NE2,
        "==": TokenKind.EQEQ,
        "||": TokenKind.CONCAT,
    }
    _ONE_CHAR = {
        "=": TokenKind.EQ,
        "<": TokenKind.LT,
        ">": TokenKind.GT,
        "+": TokenKind.PLUS,
        "-": TokenKind.MINUS,
        "*": TokenKind.STAR,
        "/": TokenKind.SLASH,
        "%": TokenKind.PERCENT,
        "(": TokenKind.LPAREN,
        ")": TokenKind.RPAREN,
        ",": TokenKind.COMMA,
        ";": TokenKind.SEMICOLON,
        ".": TokenKind.DOT,
        "?": TokenKind.QUESTION,
    }

    def __init__(self, source: str | None = None) -> None:
        self.source = "" if source is None else source
        if not isinstance(self.source, str):
            raise TypeError("SQL 源码必须是字符串")
        self._index = 0
        self._line = 1
        self._column = 1

    def __iter__(self) -> Iterator[Token]:
        return iter(self.tokenize())

    def tokenize(self, source: str | None = None) -> list[Token]:
        return self._scan(source, diagnostics=None)

    def tokenize_recovering(
        self, source: str | None = None
    ) -> tuple[list[Token], list[Diagnostic]]:
        """扫描全部 token，词法错误不中断，逐条收集成诊断。

        HOW：非法字符 / 未闭合字符串等按"跳过问题片段、保证指针前进"的方式恢复，
        因此一次调用能把整个脚本里的词法错误都报出来。
        """

        diagnostics: list[Diagnostic] = []
        tokens = self._scan(source, diagnostics=diagnostics)
        return tokens, diagnostics

    def _scan(
        self,
        source: str | None,
        *,
        diagnostics: list[Diagnostic] | None,
    ) -> list[Token]:
        if source is not None:
            if not isinstance(source, str):
                raise TypeError("SQL 源码必须是字符串")
            self.source = source
        self._index, self._line, self._column = 0, 1, 1
        result: list[Token] = []
        while self._index < len(self.source):
            start_index = self._index
            try:
                token = self._next_token()
            except LexerError as error:
                if diagnostics is None:
                    raise
                diagnostics.append(Diagnostic.from_error("lexer", error))
                # WHY：跳过出问题的片段并强制推进至少一个字符，否则会死循环。
                while self._index == start_index:
                    self._advance()
                continue
            if token is not None:
                result.append(token)
        result.append(Token(TokenKind.EOF, "", self._line, self._column))
        return result

    def _next_token(self) -> Token | None:
        """扫描一个 token；空白与注释返回 None。"""

        char = self._peek()
        if char in " \t\f\v":
            self._advance()
            return None
        if char in "\r\n":
            self._newline()
            return None
        if char == "-" and self._peek(1) == "-":
            self._line_comment()
            return None
        if char == "/" and self._peek(1) == "*":
            self._block_comment()
            return None
        line, column = self._line, self._column
        if char == "'":
            return self._string(line, column)
        if char in {'"', "`"}:
            return self._quoted_identifier(char, line, column)
        if char.isdigit() or (char == "." and self._peek(1).isdigit()):
            return self._number(line, column)
        match = _IDENTIFIER_RE.match(self.source, self._index)
        if match:
            return self._identifier(match.group(0), line, column)
        two = self.source[self._index : self._index + 2]
        if two in self._TWO_CHAR:
            self._advance(2)
            return Token(self._TWO_CHAR[two], two, line, column)
        if char in self._ONE_CHAR:
            self._advance()
            return Token(self._ONE_CHAR[char], char, line, column)
        raise LexerError(
            f"非法字符 {char!r}", line=line, column=column, character=char
        )

    lex = tokenize
    scan = tokenize

    def _peek(self, offset: int = 0) -> str:
        index = self._index + offset
        return self.source[index] if index < len(self.source) else ""

    def _advance(self, count: int = 1) -> str:
        consumed = ""
        for _ in range(count):
            char = self._peek()
            if not char:
                break
            consumed += char
            self._index += 1
            # WHY：字符串和引用标识符也允许换行，位置必须始终按原始源码推进。
            if char == "\r" or (
                char == "\n"
                and (self._index < 2 or self.source[self._index - 2] != "\r")
            ):
                self._line += 1
                self._column = 1
            elif char != "\n":
                self._column += 1
        return consumed

    def _newline(self) -> None:
        char = self._advance()
        if char == "\r" and self._peek() == "\n":
            self._advance()

    def _line_comment(self) -> None:
        self._advance(2)
        while self._peek() not in {"", "\r", "\n"}:
            self._advance()

    def _block_comment(self) -> None:
        line, column = self._line, self._column
        self._advance(2)
        while self._peek():
            if self._peek() == "*" and self._peek(1) == "/":
                self._advance(2)
                return
            if self._peek() in {"\r", "\n"}:
                self._newline()
            else:
                self._advance()
        raise LexerError(
            "块注释未闭合，期望 */", line=line, column=column, expected="*/"
        )

    def _identifier(self, spelling: str, line: int, column: int) -> Token:
        self._advance(len(spelling))
        upper = spelling.upper()
        if upper == "TRUE":
            return Token(TokenKind.BOOLEAN, spelling, line, column, True)
        if upper == "FALSE":
            return Token(TokenKind.BOOLEAN, spelling, line, column, False)
        if upper == "NULL":
            return Token(TokenKind.NULL, spelling, line, column, None)
        return Token(KEYWORDS.get(upper, TokenKind.IDENTIFIER), spelling, line, column)

    def _number(self, line: int, column: int) -> Token:
        match = _NUMBER_RE.match(self.source, self._index)
        if match is None:
            raise LexerError("非法数字", line=line, column=column)
        spelling = match.group(0)
        self._advance(len(spelling))
        if self._peek() in {"e", "E"}:
            raise LexerError("数字指数缺少有效数字", line=line, column=column)
        if self._peek() == "." and self._peek(1) in {".", ""} | set("0123456789"):
            raise LexerError("数字小数点过多", line=line, column=column)
        if any(marker in spelling for marker in ".eE"):
            # WHY：带小数点的字面量必须用 Decimal 保留字面精度；float 会在词法阶段
            # 就把 0.07 变成 0.070000000000000006938893903907228377647697925567626953125。
            try:
                literal = Decimal(spelling)
            except InvalidOperation as exc:
                raise LexerError(
                    f"数字 {spelling!r} 超出可表示范围", line=line, column=column
                ) from exc
            return Token(TokenKind.FLOAT, spelling, line, column, literal)
        return Token(TokenKind.INTEGER, spelling, line, column, int(spelling))

    def _string(self, line: int, column: int) -> Token:
        start = self._index
        self._advance()
        value: list[str] = []
        while self._peek():
            char = self._advance()
            if char == "'":
                if self._peek() == "'":
                    self._advance()
                    value.append("'")
                    continue
                return Token(
                    TokenKind.STRING,
                    self.source[start : self._index],
                    line,
                    column,
                    "".join(value),
                )
            if char == "\\" and self._peek():
                escaped = self._advance()
                value.append(
                    {
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        "0": "\0",
                        "\\": "\\",
                        "'": "'",
                    }.get(escaped, escaped)
                )
            else:
                value.append(char)
        raise LexerError(
            "字符串未闭合，期望单引号", line=line, column=column, expected="'"
        )

    def _quoted_identifier(self, quote: str, line: int, column: int) -> Token:
        start = self._index
        self._advance()
        value: list[str] = []
        while self._peek():
            char = self._advance()
            if char == quote:
                if self._peek() == quote:
                    self._advance()
                    value.append(quote)
                    continue
                return Token(
                    TokenKind.QUOTED_IDENTIFIER,
                    self.source[start : self._index],
                    line,
                    column,
                    "".join(value),
                )
            value.append(char)
        raise LexerError("引用标识符未闭合", line=line, column=column, expected=quote)


def tokenize(source: str) -> list[Token]:
    return Lexer(source).tokenize()


def lex(source: str) -> list[Token]:
    return tokenize(source)


__all__ = ["KEYWORDS", "Lexer", "Token", "TokenKind", "TokenType", "lex", "tokenize"]
