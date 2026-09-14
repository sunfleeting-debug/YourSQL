"""SQL 词法分析器，输出带行列位置和解码字面量的 Token。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Iterator

from ..common.contracts import SqlValue
from ..common.errors import LexerError


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
        """返回 Token 的词种。"""
        return self.kind

    @property
    def value(self) -> SqlValue | str:
        """返回 Token 的解码值或原始值。"""
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
        """返回 Token 的源文本。"""
        return self.lexeme

    @property
    def position(self) -> tuple[int, int]:
        """返回 Token 的行列位置。"""
        return self.line, self.column

    def as_tuple(self) -> tuple[TokenKind, str, int, int]:
        """返回对象的元组表示。"""
        return self.kind, self.lexeme, self.line, self.column

    def __iter__(self) -> Iterator[object]:
        """返回对象的迭代器。"""
        return iter(self.as_tuple())

    def __getitem__(self, index: int | slice) -> object:
        """按键或下标读取对象中的元素。"""
        return self.as_tuple()[index]

    def __len__(self) -> int:
        """返回对象包含的元素数量。"""
        return 4

    def as_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
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
            data["value"] = self.literal
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
        """初始化实例所需的状态和依赖。"""
        self.source = "" if source is None else source
        if not isinstance(self.source, str):
            raise TypeError("SQL 源码必须是字符串")
        self._index = 0
        self._line = 1
        self._column = 1

    def __iter__(self) -> Iterator[Token]:
        """返回对象的迭代器。"""
        return iter(self.tokenize())

    def tokenize(self, source: str | None = None) -> list[Token]:
        """扫描输入文本并产出 Token 流。"""
        if source is not None:
            if not isinstance(source, str):
                raise TypeError("SQL 源码必须是字符串")
            self.source = source
        self._index, self._line, self._column = 0, 1, 1
        result: list[Token] = []
        while self._index < len(self.source):
            char = self._peek()
            if char in " \t\f\v":
                self._advance()
                continue
            if char in "\r\n":
                self._newline()
                continue
            if char == "-" and self._peek(1) == "-":
                self._line_comment()
                continue
            if char == "/" and self._peek(1) == "*":
                self._block_comment()
                continue
            line, column = self._line, self._column
            if char == "'":
                result.append(self._string(line, column))
                continue
            if char in {'"', "`"}:
                result.append(self._quoted_identifier(char, line, column))
                continue
            if char.isdigit() or (char == "." and self._peek(1).isdigit()):
                result.append(self._number(line, column))
                continue
            match = _IDENTIFIER_RE.match(self.source, self._index)
            if match:
                result.append(self._identifier(match.group(0), line, column))
                continue
            two = self.source[self._index : self._index + 2]
            if two in self._TWO_CHAR:
                self._advance(2)
                result.append(Token(self._TWO_CHAR[two], two, line, column))
                continue
            if char in self._ONE_CHAR:
                self._advance()
                result.append(Token(self._ONE_CHAR[char], char, line, column))
                continue
            raise LexerError(
                f"非法字符 {char!r}", line=line, column=column, character=char
            )
        result.append(Token(TokenKind.EOF, "", self._line, self._column))
        return result

    lex = tokenize
    scan = tokenize

    def _peek(self, offset: int = 0) -> str:
        """查看当前位置的输入项而不推进位置。"""
        index = self._index + offset
        return self.source[index] if index < len(self.source) else ""

    def _advance(self, count: int = 1) -> str:
        """消费当前输入项并返回它，同时推进当前位置。"""
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
        """处理换行并更新词法扫描位置。"""
        char = self._advance()
        if char == "\r" and self._peek() == "\n":
            self._advance()

    def _line_comment(self) -> None:
        """跳过当前行注释。"""
        self._advance(2)
        while self._peek() not in {"", "\r", "\n"}:
            self._advance()

    def _block_comment(self) -> None:
        """跳过块注释并保留源码位置。"""
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
        """扫描标识符或关键字 Token。"""
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
        """扫描整数或浮点数字面量。"""
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
            return Token(TokenKind.FLOAT, spelling, line, column, float(spelling))
        return Token(TokenKind.INTEGER, spelling, line, column, int(spelling))

    def _string(self, line: int, column: int) -> Token:
        """扫描或读取字符串输入。"""
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
        """扫描双引号包围的标识符。"""
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
    """扫描输入文本并产出 Token 流。"""
    return Lexer(source).tokenize()


def lex(source: str) -> list[Token]:
    """对 SQL 文本执行词法分析。"""
    return tokenize(source)


__all__ = ["KEYWORDS", "Lexer", "Token", "TokenKind", "TokenType", "lex", "tokenize"]
