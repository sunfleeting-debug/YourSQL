"""模式目录 Catalog —— 等价于编译原理中的符号表（Symbol Table）。

层级结构：Catalog -> TableSchema -> Column
对外接口（计划书要求）：createTable / findTable / findColumn / getType
另外提供 to_rows() / from_rows()，供 engine/catalog_manager.py 把目录
作为一张特殊表持久化到页式存储中。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from database_system.sql_compiler import ast_nodes as ast
from database_system.utils.constants import (
    INT_TYPE,
    UNKNOWN_TYPE,
    DataType,
    make_type,
)
from database_system.utils.errors import SemanticError


@dataclass
class Column:
    """列元数据。"""

    name: str = ""
    data_type: DataType = UNKNOWN_TYPE
    ordinal: int = 0
    not_null: bool = False
    primary_key: bool = False
    unique: bool = False
    default: Any = None

    @property
    def key(self) -> str:
        return self.name.lower()

    def __str__(self) -> str:
        flags = []
        if self.primary_key:
            flags.append("PRIMARY KEY")
        if self.not_null:
            flags.append("NOT NULL")
        suffix = " " + " ".join(flags) if flags else ""
        return f"{self.name} {self.data_type}{suffix}"

    @staticmethod
    def from_def(coldef: "ast.ColumnDef", ordinal: int = 0) -> "Column":
        return Column(
            name=coldef.name,
            data_type=make_type(coldef.type_name, coldef.type_length),
            ordinal=ordinal,
            not_null=coldef.not_null,
            primary_key=coldef.primary_key,
            unique=coldef.unique,
            default=coldef.default.value if coldef.default is not None else None,
        )


@dataclass
class TableSchema:
    """表元数据（表结构 + 首个数据页号）。"""

    name: str = ""
    columns: list = field(default_factory=list)
    root_page_id: int = -1

    def __post_init__(self):
        for i, col in enumerate(self.columns):
            col.ordinal = i

    @property
    def key(self) -> str:
        return self.name.lower()

    def find_column(self, name: str) -> Optional[Column]:
        key = name.lower()
        for col in self.columns:
            if col.key == key:
                return col
        return None

    def index_of(self, name: str) -> int:
        col = self.find_column(name)
        return -1 if col is None else col.ordinal

    def column_names(self) -> list:
        return [c.name for c in self.columns]

    def __str__(self) -> str:
        cols = ", ".join(str(c) for c in self.columns)
        return f"{self.name}({cols})"


class Catalog:
    """系统目录：表名(小写) -> TableSchema。"""

    def __init__(self):
        self._tables: dict = {}

    # ------------------------------ 核心接口 ------------------------------

    def create_table(
        self,
        name: str,
        columns: list,
        root_page_id: int = -1,
        if_not_exists: bool = False,
    ) -> TableSchema:
        """createTable：注册一张表（已存在且 if_not_exists 时返回原表）。"""
        schema = TableSchema(name, list(columns), root_page_id)
        existing = self._tables.get(schema.key)
        if existing is not None:
            if if_not_exists:
                return existing
            raise SemanticError(f"table '{name}' already exists")
        self._tables[schema.key] = schema
        return schema

    def drop_table(self, name: str, if_exists: bool = False) -> Optional[TableSchema]:
        key = name.lower()
        schema = self._tables.pop(key, None)
        if schema is None and not if_exists:
            raise SemanticError(f"table '{name}' does not exist")
        return schema

    def find_table(self, name: str) -> Optional[TableSchema]:
        """findTable"""
        return self._tables.get(name.lower())

    def get_table(self, name: str) -> TableSchema:
        schema = self.find_table(name)
        if schema is None:
            raise SemanticError(f"table '{name}' does not exist")
        return schema

    def find_column(self, table_name: str, column_name: str) -> Optional[Column]:
        """findColumn"""
        schema = self.find_table(table_name)
        return None if schema is None else schema.find_column(column_name)

    def get_type(self, table_name: str, column_name: str) -> Optional[DataType]:
        """getType"""
        col = self.find_column(table_name, column_name)
        return None if col is None else col.data_type

    def list_tables(self) -> list:
        return [self._tables[k] for k in sorted(self._tables)]

    def table_names(self) -> list:
        return [t.name for t in self.list_tables()]

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._tables

    def __len__(self) -> int:
        return len(self._tables)

    def __str__(self) -> str:
        if not self._tables:
            return "<empty catalog>"
        return "\n".join(str(t) for t in self.list_tables())

    # ------------------------------ 持久化支持 ------------------------------

    def to_rows(self) -> list:
        """把目录摊平为行，一行一列：[表名, 序号, 列名, 类型, 长度, NOT NULL, PK, 根页号]"""
        rows = []
        for table in self.list_tables():
            for col in table.columns:
                rows.append(
                    [
                        table.name,
                        col.ordinal,
                        col.name,
                        col.data_type.kind,
                        col.data_type.length,
                        1 if col.not_null else 0,
                        1 if col.primary_key else 0,
                        table.root_page_id,
                    ]
                )
        return rows

    @staticmethod
    def from_rows(rows: list) -> "Catalog":
        """由目录表的行重建 Catalog（root_page_id 取该表序号 0 行的值）。"""
        catalog = Catalog()
        tables: dict = {}
        roots: dict = {}
        display: dict = {}
        for row in rows:
            (tname, ordinal, cname, kind, length, not_null, pk, root) = row
            key = str(tname).lower()
            tables.setdefault(key, [])
            roots.setdefault(key, int(root))
            display.setdefault(key, str(tname))
            tables[key].append(
                Column(
                    name=str(cname),
                    data_type=DataType(str(kind), int(length or 0)),
                    ordinal=int(ordinal),
                    not_null=bool(not_null),
                    primary_key=bool(pk),
                )
            )
        for key, cols in tables.items():
            cols.sort(key=lambda c: c.ordinal)
            catalog._tables[key] = TableSchema(display[key], cols, roots[key])
        return catalog
