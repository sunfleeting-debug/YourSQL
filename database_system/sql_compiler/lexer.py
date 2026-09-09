"""阶段 1：词法分析器。

输入：SQL 文本（可含多条语句、注释、字符串转义）
输出：Token 流，每个 Token = [种别码, 词素值, 行号, 列号]
错误：LexicalError（非法字符 / 未闭合字符串 / 未闭合块注释 / 非法数字），带行列位置且不崩溃
"""

from __future__ import annotations

from database_system.utils.constants import (
    DELIMITERS,
    KEYWORDS,
    ONE_CHAR_OPERATORS,
    TWO_CHAR_OPERATORS,
    TokenType,
    normalize_operator,
)
from database_system.utils.errors import LexicalError


class Token:
    """单词。

    type   : TokenType 种别码
    lexeme : 词素值（源码原文）
    value  : 语义值——关键字为大写形式，整型常量为 int，字符串常量为转义后的 str
    line / column : 1 开始的位置信息
    """

    __slots__ = ("type", "lexeme", "value", "line", "column")

    def __init__(self, type_: TokenType, lexeme: str, value, line: int, column: int):
        self.type = type_
        self.lexeme = lexeme
        self.value = value
        self.line = line
        self.column = column

    @property
    def category(self) -> str:
        return self.type.category()

    def to_tuple(self):
        """[种别码, 词素值, 行号, 列号]"""
        return (self.category, self.lexeme, self.line, self.column)

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "type": self.type.value,
            "lexeme": self.lexeme,
            "value": self.value,
            "line": self.line,
            "column": self.column,
        }

    def __repr__(self) -> str:
        return f"Token({self.category}, {self.lexeme!r}, line={self.line}, column={self.column})"

    def __eq__(self, other):  # 便于测试断言
        if not isinstance(other, Token):
            return NotImplemented
        return self.to_tuple() == other.to_tuple()


class Lexer:
    """手写扫描器，逐字符读取，支持注释、字符串转义与多字符运算符。"""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens: list[Token] = []
        self.errors: list[LexicalError] = []

    # ------------------------------ 工具 ------------------------------

    def _cur(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def _peek(self, k: int = 1) -> str:
        idx = self.pos + k
        return self.text[idx] if idx < len(self.text) else ""

    def _bump(self, n: int = 1) -> None:
        for _ in range(n):
            if self.pos >= len(self.text):
                return
            if self.text[self.pos] == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
            self.pos += 1

    def _add(self, type_: TokenType, lexeme: str, value, line: int, column: int) -> None:
        self.tokens.append(Token(type_, lexeme, value, line, column))

    def _error(self, message: str, line: int, column: int) -> LexicalError:
        err = LexicalError(message, line, column)
        self.errors.append(err)
        return err

    # ------------------------------ 主流程 ------------------------------

    def tokenize(self) -> list[Token]:
        """扫描全文，返回 Token 流（末尾含 EOF）。遇到第一个词法错误即抛出。"""
        text = self.text
        n = len(text)
        while self.pos < n:
            ch = text[self.pos]

            # 1) 空白
            if ch in " \t\r\n\f\v":
                self._bump()
                continue

            # 2) 注释
            if ch == "-" and self._peek() == "-":
                self._skip_line_comment()
                continue
            if ch == "/" and self._peek() == "*":
                self._skip_block_comment()
                continue

            # 3) 字符串常量
            if ch == "'":
                self._scan_string()
                continue

            # 4) 数字常量
            if ch.isdigit():
                self._scan_number()
                continue

            # 5) 标识符 / 关键字
            if ch.isalpha() or ch == "_":
                self._scan_word()
                continue

            # 6) 运算符（先匹配双字符）
            two = text[self.pos : self.pos + 2]
            if two in TWO_CHAR_OPERATORS:
                line, column = self.line, self.column
                self._bump(2)
                self._add(TokenType.OPERATOR, two, normalize_operator(two), line, column)
                continue
            if ch in ONE_CHAR_OPERATORS:
                line, column = self.line, self.column
                self._bump()
                self._add(TokenType.OPERATOR, ch, normalize_operator(ch), line, column)
                continue

            # 7) 分隔符
            if ch in DELIMITERS:
                line, column = self.line, self.column
                self._bump()
                self._add(TokenType.DELIMITER, ch, ch, line, column)
                continue

            # 8) 非法字符
            line, column = self.line, self.column
            raise self._error(f"illegal character '{ch}'", line, column)

        self._add(TokenType.EOF, "<EOF>", None, self.line, self.column)
        return self.tokens

    # ------------------------------ 子扫描 ------------------------------

    def _skip_line_comment(self) -> None:
        while self.pos < len(self.text) and self._cur() != "\n":
            self._bump()

    def _skip_block_comment(self) -> None:
        start_line, start_col = self.line, self.column
        self._bump(2)  # 跳过 /*
        while self.pos < len(self.text):
            if self._cur() == "*" and self._peek() == "/":
                self._bump(2)
                return
            self._bump()
        raise self._error("unterminated block comment (missing '*/')", start_line, start_col)

    def _scan_string(self) -> None:
        start_line, start_col = self.line, self.column
        self._bump()  # 跳过开头的 '
        buf: list[str] = []
        while True:
            if self.pos >= len(self.text):
                raise self._error(
                    "unterminated string literal (missing closing quote)",
                    start_line,
                    start_col,
                )
            ch = self._cur()
            if ch == "'":
                if self._peek() == "'":      # '' -> 一个字面单引号
                    buf.append("'")
                    self._bump(2)
                    continue
                self._bump()
                break
            if ch == "\n":
                raise self._error("unterminated string literal (newline in string)",
                                  start_line, start_col)
            buf.append(ch)
            self._bump()
        lexeme = "".join(buf)
        self._add(TokenType.STRING_CONST, lexeme, lexeme, start_line, start_col)

    def _scan_number(self) -> None:
        start_line, start_col = self.line, self.column
        start = self.pos
        while self.pos < len(self.text) and self._cur().isdigit():
            self._bump()
        # 非法数字：1.5（不支持浮点）、123abc、1.2.3
        nxt = self._cur()
        if nxt == "." or (nxt and (nxt.isalpha() or nxt == "_")):
            while self.pos < len(self.text) and (
                self._cur().isalnum() or self._cur() in "._"
            ):
                self._bump()
            bad = self.text[start : self.pos]
            raise self._error(
                f"invalid numeric literal '{bad}' (only integer literals are supported)",
                start_line,
                start_col,
            )
        lexeme = self.text[start : self.pos]
        value = int(lexeme)
        if value > 2147483647 or value < -2147483648:
            raise self._error(
                f"integer literal '{lexeme}' out of range (INT32)", start_line, start_col
            )
        self._add(TokenType.INT_CONST, lexeme, value, start_line, start_col)

    def _scan_word(self) -> None:
        start_line, start_col = self.line, self.column
        start = self.pos
        while self.pos < len(self.text) and (self._cur().isalnum() or self._cur() == "_"):
            self._bump()
        lexeme = self.text[start : self.pos]
        upper = lexeme.upper()
        if upper in KEYWORDS:
            self._add(TokenType.KEYWORD, lexeme, upper, start_line, start_col)
        else:
            self._add(TokenType.IDENTIFIER, lexeme, lexeme, start_line, start_col)


def tokenize(sql: str) -> list[Token]:
    """便捷入口：SQL 文本 -> Token 流。"""
    return Lexer(sql).tokenize()


def format_tokens(tokens: list[Token]) -> str:
    """把 Token 流格式化为可读表格，供演示与调试使用。"""
    header = f"{'CATEGORY':<11} {'LEXEME':<20} {'VALUE':<16} LINE  COLUMN"
    lines = [header, "-" * len(header)]
    for t in tokens:
        val = "" if t.value is None else str(t.value)
        lines.append(
            f"{t.category:<11} {t.lexeme:<20} {val:<16} {t.line:<5} {t.column}"
        )
    return "\n".join(lines)
