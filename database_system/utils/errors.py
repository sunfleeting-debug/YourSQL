"""统一错误类型。

四个阶段各有一种错误，输出格式统一为：
    <错误类型> at line <行>, column <列>: <原因说明>
例如：
    SemanticError at line 3, column 11: operator '+' cannot be applied to INT and VARCHAR
"""

from __future__ import annotations


class MiniSQLError(Exception):
    """所有 MiniSQL 错误的基类。"""

    error_type = "Error"

    def __init__(self, message: str, line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.message = message
        self.line = line
        self.column = column

    def __str__(self) -> str:
        if self.line is None:
            return f"{self.error_type}: {self.message}"
        return f"{self.error_type} at line {self.line}, column {self.column}: {self.message}"

    def to_dict(self) -> dict:
        return {
            "type": self.error_type,
            "line": self.line,
            "column": self.column,
            "reason": self.message,
        }


class LexicalError(MiniSQLError):
    """词法错误：非法字符、未闭合字符串 / 注释、非法数字。"""

    error_type = "LexicalError"


class ParseError(MiniSQLError):
    """语法错误：输出时显示为 SyntaxError。"""

    error_type = "SyntaxError"


class SemanticError(MiniSQLError):
    """语义错误：表 / 列不存在、类型不匹配、列数不匹配等。"""

    error_type = "SemanticError"


class ExecutionError(MiniSQLError):
    """执行期错误：存储或执行引擎异常。"""

    error_type = "ExecutionError"


class StorageError(MiniSQLError):
    """存储系统错误：页已满、缓冲区满等。"""

    error_type = "StorageError"


# 供 except 子句统一捕获
COMPILER_ERRORS = (LexicalError, ParseError, SemanticError)
