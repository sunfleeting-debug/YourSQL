"""阶段 6：系统目录管理（Catalog 持久化）。

设计要点（计划书要求）：
  系统目录本身就是一张「特殊表」——它同样由若干数据页组成，
  使用与用户表完全相同的记录序列化 / 槽位页格式，只是页类型为 CATALOG，
  根页号记录在数据库文件第 0 页（meta）中。

目录表的逻辑结构（每行描述一列）：
    table_name | ordinal | column_name | type_kind | type_len | not_null | pk | root_page
"""

from __future__ import annotations

from database_system.engine.storage_engine import TableHeap
from database_system.sql_compiler.catalog import Catalog
from database_system.storage.buffer import BufferPoolManager
from database_system.storage.file_manager import DiskManager
from database_system.utils.constants import INVALID_PAGE_ID, PageType


class CatalogManager:
    """负责 Catalog <-> 磁盘 的装载与保存。"""

    CATALOG_TABLE = "__mini_catalog__"

    def __init__(self, disk: DiskManager, buffer: BufferPoolManager):
        self.disk = disk
        self.buffer = buffer

    # ------------------------------ 装载 ------------------------------

    def load(self) -> Catalog:
        """启动时从目录表读取全部表结构。"""
        root = self.disk.catalog_root
        if root == INVALID_PAGE_ID:
            return Catalog()
        heap = TableHeap(self.buffer, root)
        rows = [values for _, values in heap.iter_rows()]
        return Catalog.from_rows(rows)

    # ------------------------------ 保存 ------------------------------

    def save(self, catalog: Catalog) -> None:
        """整体重写目录表：释放旧页 -> 分配新页链 -> 写入全部列 -> 更新 meta。"""
        old_root = self.disk.catalog_root
        if old_root != INVALID_PAGE_ID:
            TableHeap(self.buffer, old_root).drop_all()

        new_root = self.buffer.new_page_unpinned(PageType.CATALOG)
        heap = TableHeap(self.buffer, new_root)
        for row in catalog.to_rows():
            heap.insert_row(row)

        self.disk.catalog_root = new_root
        self.disk.write_meta()

    # ------------------------------ 辅助 ------------------------------

    def is_initialized(self) -> bool:
        return self.disk.catalog_root != INVALID_PAGE_ID

    def __str__(self) -> str:
        return f"CatalogManager(root_page={self.disk.catalog_root})"
