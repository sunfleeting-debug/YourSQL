"""跨边界使用的值类型。

SQL 执行结果和 HTTP JSON 都是动态数据，但它们并不等于任意 Python
对象。把这两类边界单独命名后，服务层的参数含义会比大量
``dict[str, object]`` 更清楚，也方便后续引入静态检查。
"""

from __future__ import annotations

from decimal import Decimal
from typing import TypeAlias

SqlValue: TypeAlias = str | int | float | Decimal | bool | None
JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
SqlRow: TypeAlias = tuple[SqlValue, ...]

__all__ = ["JsonObject", "JsonPrimitive", "JsonValue", "SqlRow", "SqlValue"]
