"""全局常量、枚举与基础类型定义。

本文件是阶段 0「规则定义」的代码化产物：
词法规则（关键字表 / 运算符表 / 分隔符表）与类型规则（DataType）集中在此，
保证 grammar.md 与代码实现共用同一份定义。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

# ============================ 词法规则 ============================


class TokenType(Enum):
    """单词种别码。category() 返回计划书中要求的 5 大类。"""

    KEYWORD = "KEYWORD"
    IDENTIFIER = "IDENTIFIER"
    INT_CONST = "INT_CONST"
    STRING_CONST = "STRING_CONST"
    OPERATOR = "OPERATOR"
    DELIMITER = "DELIMITER"
    EOF = "EOF"

    def category(self) -> str:
        return TOKEN_CATEGORY[self]


TOKEN_CATEGORY = {
    TokenType.KEYWORD: "KEYWORD",
    TokenType.IDENTIFIER: "IDENTIFIER",
    TokenType.INT_CONST: "CONST",
    TokenType.STRING_CONST: "CONST",
    TokenType.OPERATOR: "OPERATOR",
    TokenType.DELIMITER: "DELIMITER",
    TokenType.EOF: "EOF",
}

KEYWORDS = frozenset(
    {
        "SELECT", "FROM", "WHERE", "CREATE", "TABLE", "INSERT", "INTO", "VALUES",
        "DELETE", "UPDATE", "SET", "DROP", "DISTINCT", "ORDER", "BY", "GROUP",
        "LIMIT", "OFFSET", "AS", "AND", "OR", "NOT", "NULL", "IS", "IN",
        "EXPLAIN", "IF", "EXISTS", "PRIMARY", "KEY", "UNIQUE", "DEFAULT",
        "ASC", "DESC", "TRUE", "FALSE", "INT", "VARCHAR", "BOOL", "BOOLEAN",
    }
)

TWO_CHAR_OPERATORS = frozenset({"!=", "<>", ">=", "<="})
ONE_CHAR_OPERATORS = frozenset("=<>+-*/")
DELIMITERS = frozenset("(),;.")

# 比较 / 算术 / 逻辑 运算符分组（供语法与语义阶段共用）
COMPARISON_OPS = frozenset({"=", "!=", "<>", ">", ">=", "<", "<="})
ARITHMETIC_OPS = frozenset({"+", "-", "*", "/"})
LOGICAL_OPS = frozenset({"AND", "OR"})


def normalize_operator(lexeme: str) -> str:
    """把 `<>` 归一化为 `!=`，其余保持不变。"""
    return "!=" if lexeme == "<>" else lexeme


# ============================ 类型规则 ============================


@dataclass(frozen=True)
class DataType:
    """列 / 表达式的数据类型。

    kind  : INT / VARCHAR / BOOL / NULL / UNKNOWN
    length: VARCHAR 的声明长度（0 表示未指定，取默认值）
    """

    kind: str = "UNKNOWN"
    length: int = 0

    def __str__(self) -> str:
        if self.kind == "VARCHAR":
            return f"VARCHAR({self.length})" if self.length else "VARCHAR"
        return self.kind

    def same_kind(self, other: "DataType") -> bool:
        return self.kind == other.kind


INT_TYPE = DataType("INT")
BOOL_TYPE = DataType("BOOL")
NULL_TYPE = DataType("NULL")
UNKNOWN_TYPE = DataType("UNKNOWN")
DEFAULT_VARCHAR_LENGTH = 255


def varchar_type(length: int = 0) -> DataType:
    return DataType("VARCHAR", length)


def make_type(name: str, length: int = 0) -> DataType:
    """由 SQL 类型名构造 DataType。"""
    key = name.upper()
    if key == "INT":
        return INT_TYPE
    if key == "BOOL" or key == "BOOLEAN":
        return BOOL_TYPE
    if key == "VARCHAR":
        return varchar_type(length)
    return DataType(key, length)


# ============================ 存储常量 ============================

PAGE_SIZE = 4096
INVALID_PAGE_ID = -1


class PageType:
    FREE = 0
    META = 1
    DATA = 2
    CATALOG = 3


class ReplacePolicy:
    LRU = "LRU"
    FIFO = "FIFO"


# ============================ 值工具 ============================


def value_str(value: Any) -> str:
    """用于结果展示的值格式化：NULL / 字符串 / 布尔 / 整数。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return value
    return str(value)
