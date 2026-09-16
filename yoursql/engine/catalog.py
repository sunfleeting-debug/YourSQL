"""系统目录：表、字段和索引元数据及其 JSON 表示。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from yoursql.common.errors import CatalogError
from yoursql.common.types import Column, DataType, PageId, Schema, TableId, TableStats, Value

#Catalog 保存表结构和数据位置，包括表名、列定义、页号列表、行数等。具体记录由 TableHeap 管理。
#表名
# ↓ 查 Catalog
#表结构、page_ids
 #↓ 创建或取得 TableHeap
#读取数据页
# ↓ 找到有效槽位
#解码得到记录
def _value_to_dict(value: Value | None) -> dict[str, object] | None:
    """将目录中的标量值转换为可持久化的字典值。"""
    if value is None:
        return None
    return {"type": value.data_type.value, "value": value.value}


def _value_from_dict(value: Mapping[str, object] | None) -> Value | None:
    """从持久化字典值恢复目录标量。"""
    if value is None:
        return None
    return Value(DataType.parse(str(value["type"])), value.get("value"))


def _column_to_dict(column: Column) -> dict[str, object]:
    """将列元数据转换为可持久化字典。"""
    return {
        "name": column.name,
        "type": column.data_type.value,
        "nullable": column.nullable,
        "primary_key": column.primary_key,
        "unique": column.unique,
        "default": _value_to_dict(column.default),
    }


def _column_from_dict(value: Mapping[str, object]) -> Column:
    """从字典恢复列元数据。"""
    default = value.get("default")
    default_value = _value_from_dict(default if isinstance(default, Mapping) else None)
    return Column(
        name=str(value["name"]),
        data_type=DataType.parse(str(value["type"])),
        nullable=bool(value.get("nullable", True)),
        primary_key=bool(value.get("primary_key", False)),
        unique=bool(value.get("unique", False)),
        default=default_value,
    )


@dataclass
class IndexMetadata:
    """索引目录项；数据结构本体由 storage.IndexManager 管理。"""

    name: str
    table_id: TableId
    columns: tuple[str, ...]
    unique: bool = False
    index_type: str = "btree"
    root_page_id: PageId | None = None
    # HOW：覆盖列（CREATE INDEX ... INCLUDE (...)）；默认为空，旧目录反序列化后保持纯键索引。
    payload_columns: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {
            "name": self.name,
            "table_id": int(self.table_id),
            "columns": list(self.columns),
            "unique": self.unique,
            "index_type": self.index_type,
            "root_page_id": None
            if self.root_page_id is None
            else int(self.root_page_id),
            "payload_columns": list(self.payload_columns),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "IndexMetadata":
        """从字典数据构造对象实例。"""
        root = value.get("root_page_id")
        return cls(
            name=str(value["name"]),
            table_id=TableId(int(value["table_id"])),
            columns=tuple(str(item) for item in value.get("columns", [])),
            unique=bool(value.get("unique", False)),
            index_type=str(value.get("index_type", "btree")),
            root_page_id=None if root is None else PageId(int(root)),
            payload_columns=tuple(
                str(item) for item in value.get("payload_columns", [])
            ),
        )


@dataclass#解释保存了哪些元数据
# HOW: 表元数据保存结构、页号列表和行数；实际用户记录在 HEAP 页中。
class TableMetadata:
    """目录中的表定义、数据页和行数统计。"""

    table_id: TableId#数据库内部给表分配的编号
    name: str#表名
    schema: Schema#完整列定义
    first_page_id: PageId | None = None#第一个数据页号
    page_ids: list[PageId] = field(default_factory=list)#所有数据页号,数据库怎样知道数据在哪里
    row_count: int = 0#维护的记录数量
    indexes: list[str] = field(default_factory=list)#与这张表关联的索引名称
    system: bool = False#是否为系统表

    @property
    def stats(self) -> TableStats:
        """返回对象的统计信息。"""
        return TableStats(self.row_count, len(self.page_ids))

    # HOW: 表对象转为字典供目录序列化，实际写盘在 Database._persist_catalog。
    def to_dict(self, *, compact_system: bool = False) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        result: dict[str, object] = {
            "table_id": int(self.table_id),
            "name": self.name,
            "page_ids": [int(page_id) for page_id in self.page_ids],
            "row_count": self.row_count,
            "system": self.system,
        }
        if self.first_page_id is not None and (not compact_system or not self.page_ids):
            result["first_page_id"] = int(self.first_page_id)
        if self.indexes:
            result["indexes"] = list(self.indexes)
        if not compact_system:
            result["columns"] = [_column_to_dict(column) for column in self.schema]
            result.setdefault("first_page_id", None)
            result.setdefault("indexes", [])
        return result

    @classmethod
    # HOW: 从字典恢复列定义和页号列表，不在这里读取用户记录。
    def from_dict(cls, value: Mapping[str, object]) -> "TableMetadata":
        """从字典数据构造对象实例。"""
        raw_columns = value.get("columns")
        if raw_columns is None and bool(value.get("system", False)):
            # HOW：紧凑系统表元数据不重复保存固定列定义，加载时复用系统目录规范。
            from yoursql.engine.system_catalog import SYSTEM_TABLE_SPECS

            table_name = str(value["name"]).lower()
            spec = next(
                (
                    item
                    for item in SYSTEM_TABLE_SPECS
                    if item.name.lower() == table_name
                ),
                None,
            )
            if spec is None:
                raise CatalogError(f"未知的内部表 {table_name!r}")
            schema = spec.schema
        else:
            raw_columns = [] if raw_columns is None else raw_columns
            if not isinstance(raw_columns, list):
                raise CatalogError("目录中的 columns 不是数组")
            columns = [
                _column_from_dict(item)
                for item in raw_columns
                if isinstance(item, Mapping)
            ]
            schema = Schema.from_iterable(columns)
        first = value.get("first_page_id")
        raw_page_ids = [PageId(int(page_id)) for page_id in value.get("page_ids", [])]
        if first is None and raw_page_ids:
            first = raw_page_ids[0]
        return cls(
            TableId(int(value["table_id"])),
            str(value["name"]),
            schema,
            None if first is None else PageId(int(first)),
            raw_page_ids,
            int(value.get("row_count", 0)),
            [str(name) for name in value.get("indexes", [])],
            bool(value.get("system", False)),
        )


@dataclass
class ViewMetadata:
    """只读逻辑视图定义；视图没有自己的数据页。"""

    name: str
    schema: Schema
    definition_sql: str
    system: bool = False

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {
            "name": self.name,
            "columns": [_column_to_dict(column) for column in self.schema],
            "definition_sql": self.definition_sql,
            "system": self.system,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ViewMetadata":
        """从字典数据构造对象实例。"""
        raw_columns = value.get("columns", [])
        if not isinstance(raw_columns, list):
            raise CatalogError("目录中的 view columns 不是数组")
        columns = [
            _column_from_dict(item) for item in raw_columns if isinstance(item, Mapping)
        ]
        try:
            schema = Schema.from_iterable(columns)
        except ValueError as exc:
            raise CatalogError("目录中的视图列定义无效") from exc
        definition_sql = value.get("definition_sql")
        if not isinstance(definition_sql, str) or not definition_sql.strip():
            raise CatalogError("目录中的视图查询定义无效")
        return cls(
            str(value["name"]), schema, definition_sql, bool(value.get("system", False))
        )


class Catalog:
    """大小写不敏感的内存目录；数据库层负责把它写入 CATALOG 页。"""

    VERSION = 3

    def __init__(self) -> None:
        """初始化实例所需的状态和依赖。"""
        self._tables: dict[str, TableMetadata] = {}
        self._views: dict[str, ViewMetadata] = {}
        self._indexes: dict[str, IndexMetadata] = {}
        self._next_table_id = 1

    @staticmethod
    def _key(name: str) -> str:
        """根据输入生成稳定的内部键。"""
        return name.strip().lower()

    def __len__(self) -> int:
        """返回对象包含的元素数量。"""
        return sum(not table.system for table in self._tables.values())

    def tables(self, *, include_system: bool = False) -> tuple[TableMetadata, ...]:
        """返回用户表；内部调用可显式要求包含系统表。"""

        selected = (
            table
            for table in self._tables.values()
            if include_system or not table.system
        )
        return tuple(sorted(selected, key=lambda item: int(item.table_id)))

    def system_tables(self) -> tuple[TableMetadata, ...]:
        """返回内部系统表，供启动恢复和只读检查使用。"""

        return tuple(
            table for table in self.tables(include_system=True) if table.system
        )

    def views(self) -> tuple[ViewMetadata, ...]:
        """返回逻辑视图；视图不计入物理表数量。"""

        return tuple(sorted(self._views.values(), key=lambda item: item.name.lower()))

    def indexes(self) -> tuple[IndexMetadata, ...]:
        """返回目录中已注册的索引元数据。"""
        return tuple(sorted(self._indexes.values(), key=lambda item: item.name.lower()))

    def get_table(self, name: str, *, include_system: bool = False) -> TableMetadata:
        """按名称获取表元数据；不存在时抛出目录错误。"""
        table = self._tables.get(self._key(name))
        if table is None or (table.system and not include_system):
            raise CatalogError(f"表 {name!r} 不存在")
        return table

    def find_table(
        self, name: str, *, include_system: bool = False
    ) -> TableMetadata | None:
        """按名称查找表元数据，不存在时返回空值。"""
        table = self._tables.get(self._key(name))
        return (
            table
            if table is not None and (include_system or not table.system)
            else None
        )

    def get_view(self, name: str) -> ViewMetadata:
        """按名称获取视图元数据；不存在时抛出目录错误。"""
        view = self._views.get(self._key(name))
        if view is None:
            raise CatalogError(f"视图 {name!r} 不存在")
        return view

    def find_view(self, name: str) -> ViewMetadata | None:
        """按名称查找视图元数据，不存在时返回空值。"""
        return self._views.get(self._key(name))

    def get_relation(self, name: str) -> TableMetadata | ViewMetadata:
        """读取可出现在 SELECT/FROM 中的物理表或逻辑视图。"""

        table = self.find_table(name)
        if table is not None:
            return table
        view = self.find_view(name)
        if view is not None:
            return view
        raise CatalogError(f"表或视图 {name!r} 不存在")

    def create_table(
        self,
        name: str,
        schema: Schema,
        *,
        table_id: TableId | None = None,
        system: bool = False,
    ) -> TableMetadata:
        """校验并注册新的表元数据。"""
        key = self._key(name)
        if not key:
            raise CatalogError("表名不能为空")
        if key in self._tables or key in self._views:
            raise CatalogError(f"表或视图 {name!r} 已存在")
        selected = table_id or TableId(self._next_table_id)
        if any(item.table_id == selected for item in self._tables.values()):
            raise CatalogError(f"表 ID {int(selected)} 已存在")
        self._next_table_id = max(self._next_table_id, int(selected) + 1)
        # HOW: 建表登记内存元数据，用户数据页按插入需要分配。
        table = TableMetadata(selected, name, schema, system=system)
        self._tables[key] = table
        return table

    def create_view(
        self,
        name: str,
        schema: Schema,
        definition_sql: str,
        *,
        system: bool = False,
    ) -> ViewMetadata:
        """校验并注册新的视图定义。"""
        key = self._key(name)
        if not key:
            raise CatalogError("视图名不能为空")
        if key in self._tables or key in self._views:
            raise CatalogError(f"表或视图 {name!r} 已存在")
        if not definition_sql.strip():
            raise CatalogError("视图查询定义不能为空")
        view = ViewMetadata(name, schema, definition_sql, system)
        self._views[key] = view
        return view

    def drop_view(self, name: str) -> ViewMetadata:
        """删除指定视图并返回其元数据。"""
        view = self._views.pop(self._key(name), None)
        if view is None:
            raise CatalogError(f"视图 {name!r} 不存在")
        return view

    def drop_table(self, name: str) -> TableMetadata:
        """删除指定表及其关联元数据。"""
        table = self._tables.pop(self._key(name), None)
        if table is None:
            raise CatalogError(f"表 {name!r} 不存在")
        if table.system:
            self._tables[self._key(name)] = table
            raise CatalogError("系统表不能删除")
        for index_name in tuple(table.indexes):
            self._indexes.pop(self._key(index_name), None)
        return table

    def create_index(self, index: IndexMetadata) -> IndexMetadata:
        """校验并注册新的索引元数据。"""
        key = self._key(index.name)
        if key in self._indexes:
            raise CatalogError(f"索引 {index.name!r} 已存在")
        table = next(
            (item for item in self._tables.values() if item.table_id == index.table_id),
            None,
        )
        if table is None:
            raise CatalogError(f"表 ID {int(index.table_id)} 不存在")
        if not index.columns:
            raise CatalogError("索引至少需要一列")
        for column in index.columns:
            table.schema.column(column)
        self._indexes[key] = index
        table.indexes.append(index.name)
        return index

    def drop_index(self, name: str) -> IndexMetadata:
        """删除指定索引并释放其资源。"""
        index = self._indexes.pop(self._key(name), None)
        if index is None:
            raise CatalogError(f"索引 {name!r} 不存在")
        table = next(
            (item for item in self._tables.values() if item.table_id == index.table_id),
            None,
        )
        if table is not None:
            table.indexes = [
                item for item in table.indexes if self._key(item) != self._key(name)
            ]
        return index

    def get_index(self, name: str) -> IndexMetadata:
        """按名称获取索引元数据；不存在时抛出目录错误。"""
        index = self._indexes.get(self._key(name))
        if index is None:
            raise CatalogError(f"索引 {name!r} 不存在")
        return index

    def find_index(self, name: str) -> IndexMetadata | None:
        """按名称查找索引元数据，不存在时返回空值。"""
        return self._indexes.get(self._key(name))

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {
            "version": self.VERSION,
            "next_table_id": self._next_table_id,
            "tables": [
                table.to_dict(compact_system=table.system)
                for table in self.tables(include_system=True)
            ],
            "views": [view.to_dict() for view in self.views()],
            "indexes": [index.to_dict() for index in self.indexes()],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "Catalog":
        """从字典数据构造对象实例。"""
        catalog = cls()
        version = int(value.get("version", 1))
        if version > cls.VERSION:
            raise CatalogError(f"不支持的目录版本 {version}")
        raw_tables = value.get("tables", [])
        if not isinstance(raw_tables, list):
            raise CatalogError("目录中的 tables 不是数组")
        for raw_table in raw_tables:
            if isinstance(raw_table, Mapping):
                table = TableMetadata.from_dict(raw_table)
                catalog._tables[catalog._key(table.name)] = table
                catalog._next_table_id = max(
                    catalog._next_table_id, int(table.table_id) + 1
                )
        raw_views = value.get("views", [])
        if not isinstance(raw_views, list):
            raise CatalogError("目录中的 views 不是数组")
        for raw_view in raw_views:
            if isinstance(raw_view, Mapping):
                view = ViewMetadata.from_dict(raw_view)
                key = catalog._key(view.name)
                if key in catalog._tables or key in catalog._views:
                    raise CatalogError(f"目录中的表或视图 {view.name!r} 重复")
                catalog._views[key] = view
        raw_indexes = value.get("indexes", [])
        if not isinstance(raw_indexes, list):
            raise CatalogError("目录中的 indexes 不是数组")
        for raw_index in raw_indexes:
            if isinstance(raw_index, Mapping):
                index = IndexMetadata.from_dict(raw_index)
                catalog._indexes[catalog._key(index.name)] = index
        return catalog
