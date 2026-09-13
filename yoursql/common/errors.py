"""YourSQL 各层共享的结构化异常。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class YourSQLError(Exception):
    """带机器可读错误码、位置和附加信息的基础异常。"""

    message: str
    code: str = "YOURSQL_ERROR"
    line: int | None = None
    column: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def __str__(self) -> str:
        location = ""
        if self.line is not None:
            location = f" at line {self.line}"
            if self.column is not None:
                location += f", column {self.column}"
        return f"[{self.code}]{location}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        if self.details:
            result["details"] = self.details
        return result


class LexerError(YourSQLError):
    """词法阶段错误。"""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None, **details: Any) -> None:
        super().__init__(message, "LEXER_ERROR", line, column, details)


class ParserError(YourSQLError):
    """语法阶段错误。"""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None, **details: Any) -> None:
        super().__init__(message, "PARSER_ERROR", line, column, details)


class BinderError(YourSQLError):
    """语义绑定阶段错误。"""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None, **details: Any) -> None:
        super().__init__(message, "BINDER_ERROR", line, column, details)


class CatalogError(YourSQLError):
    """系统目录错误。"""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None, **details: Any) -> None:
        super().__init__(message, "CATALOG_ERROR", line, column, details)


class StorageError(YourSQLError):
    """页文件、缓存和记录存储错误。"""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None, **details: Any) -> None:
        super().__init__(message, "STORAGE_ERROR", line, column, details)


class ExecutionError(YourSQLError):
    """执行算子或约束错误。"""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None, **details: Any) -> None:
        super().__init__(message, "EXECUTION_ERROR", line, column, details)


class AuthorizationError(YourSQLError):
    """权限检查错误。"""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None, **details: Any) -> None:
        super().__init__(message, "AUTHORIZATION_ERROR", line, column, details)
