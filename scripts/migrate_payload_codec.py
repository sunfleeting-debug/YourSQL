"""将现有 JSON payload 数据库安全迁移为 manual payload 数据库。"""

from __future__ import annotations

import argparse
import hashlib
import os
import uuid
from collections.abc import Iterable
from pathlib import Path

from yoursql.common.config import DatabaseConfig
from yoursql.common.types import PageId
from yoursql.engine.catalog import IndexMetadata, TableMetadata
from yoursql.engine.runtime.database import Database
from yoursql.storage.disk import DiskManager


def _database_config(path: Path, codec: str) -> DatabaseConfig:
    """按现有文件页大小创建迁移配置。"""

    page_size = Database.detect_page_size(path) or 4096
    return DatabaseConfig(page_size=page_size, payload_codec=codec)  # type: ignore[arg-type]


def _row_digest(database: Database, table: TableMetadata) -> tuple[int, str]:
    """以稳定摘要校验迁移前后的行集与行序。"""

    digest = hashlib.sha256()
    count = 0
    for record in database._heap(table).scan():
        digest.update(repr(record.row).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _copy_catalog_shape(source: Database, target: Database) -> None:
    """复制用户表和用户视图定义；系统对象由目标库初始化。"""

    for source_table in source.catalog.system_tables():
        target_table = target.catalog.get_table(
            source_table.name, include_system=True
        )
        if target_table.table_id != source_table.table_id:
            raise RuntimeError(
                f"系统表 {source_table.name} 的 table_id 不一致："
                f"{source_table.table_id} != {target_table.table_id}"
            )

    for table in source.catalog.tables():
        target.catalog.create_table(
            table.name,
            table.schema,
            table_id=table.table_id,
        )

    for view in source.catalog.views():
        if not view.system:
            target.catalog.create_view(
                view.name,
                view.schema,
                view.definition_sql,
            )


def _copy_rows(source: Database, target: Database) -> None:
    """按源库逻辑行重建用户表页，编码由目标库自动使用 manual。"""

    for source_table in source.catalog.tables():
        target_table = target.catalog.get_table(source_table.name)
        rows = (record.row for record in source._heap(source_table).scan())
        target_heap = target._heap(target_table)
        target_heap.append_batch(rows)
        target_table.page_ids = [PageId(page_id) for page_id in target_heap.page_ids]
        target_table.first_page_id = (
            target_table.page_ids[0] if target_table.page_ids else None
        )
        target_table.row_count = source_table.row_count


def _index_entries(database: Database, table: TableMetadata, metadata: IndexMetadata) -> Iterable[tuple[tuple[object, ...], object, tuple[object, ...]]]:
    """为迁移后的表生成索引键、RowId 与覆盖列值。"""

    for record in database._heap(table).scan():
        key = tuple(record.row[table.schema.index(column)] for column in metadata.columns)
        payload = tuple(
            record.row[table.schema.index(column)]
            for column in metadata.payload_columns
        )
        if metadata.unique and any(value is None for value in key):
            continue
        yield key, record.row_id, payload


def _copy_indexes(source: Database, target: Database) -> None:
    """在目标页上重建 B+Tree，避免复用源库的页号和 JSON 节点。"""

    for source_metadata in source.catalog.indexes():
        source_table = next(
            table
            for table in source.catalog.tables(include_system=True)
            if table.table_id == source_metadata.table_id
        )
        target_table = target.catalog.get_table(
            source_table.name, include_system=True
        )
        metadata = IndexMetadata(
            source_metadata.name,
            target_table.table_id,
            source_metadata.columns,
            source_metadata.unique,
            source_metadata.index_type,
            payload_columns=source_metadata.payload_columns,
        )
        tree = target.index_manager.create(
            metadata.name,
            unique=metadata.unique,
            buffer_pool=target.buffer_pool,
            on_root_change=lambda page_id, item=metadata: setattr(
                item, "root_page_id", PageId(page_id)
            ),
        )
        tree.bulk_load(_index_entries(target, target_table, metadata))
        target.catalog.create_index(metadata)


def _copy_rbac(source: Database, target: Database) -> None:
    """重建内部权限表并保留用户、角色和授权。"""

    target.rbac = source.rbac
    target.system_catalog.persist_rbac(target.rbac)


def _validate(source: Database, target: Database) -> None:
    """校验用户表行集、目录关系、索引定义和 RBAC。"""

    if target.payload_codec.name != "manual":
        raise RuntimeError("目标库没有使用 manual payload")
    source_tables = source.catalog.tables()
    target_tables = target.catalog.tables()
    if [table.name.lower() for table in source_tables] != [
        table.name.lower() for table in target_tables
    ]:
        raise RuntimeError("迁移前后用户表目录不一致")
    for source_table, target_table in zip(source_tables, target_tables, strict=True):
        if _row_digest(source, source_table) != _row_digest(target, target_table):
            raise RuntimeError(f"表 {source_table.name} 的行数据校验失败")
    source_views = [(view.name.lower(), view.definition_sql) for view in source.catalog.views()]
    target_views = [(view.name.lower(), view.definition_sql) for view in target.catalog.views()]
    if source_views != target_views:
        raise RuntimeError("迁移前后视图目录不一致")
    source_indexes = [
        (
            item.name.lower(),
            item.table_id,
            item.columns,
            item.unique,
            item.payload_columns,
        )
        for item in source.catalog.indexes()
    ]
    target_indexes = [
        (
            item.name.lower(),
            item.table_id,
            item.columns,
            item.unique,
            item.payload_columns,
        )
        for item in target.catalog.indexes()
    ]
    if source_indexes != target_indexes:
        raise RuntimeError("迁移前后索引目录不一致")
    if source.rbac.to_dict() != target.rbac.to_dict():
        raise RuntimeError("迁移前后 RBAC 不一致")


def _migrate_to_temp(path: Path, temp_path: Path) -> None:
    """把单个源库复制到 manual 临时库并完成校验。"""

    source_config = _database_config(path, "json")
    target_config = DatabaseConfig(
        page_size=source_config.page_size,
        buffer_pool_size=source_config.buffer_pool_size,
        replacement_policy=source_config.replacement_policy,
        payload_codec="manual",
    )
    with Database(path, config=source_config) as source, Database(
        temp_path, config=target_config
    ) as target:
        if source.payload_codec.name == "manual":
            raise RuntimeError(f"{path} 已经是 manual payload")
        _copy_catalog_shape(source, target)
        _copy_rows(source, target)
        _copy_rbac(source, target)
        target._persist_catalog()
        _copy_indexes(source, target)
        target._persist_catalog()
        target.buffer_pool.flush_all()
        target.disk.sync()
        _validate(source, target)


def migrate_file(path: Path, *, replace: bool) -> None:
    """迁移一个数据库；替换模式会留下可恢复的原文件备份。"""

    path = path.resolve()
    if not path.is_file() or path.suffix.lower() != ".db":
        raise ValueError(f"不是可迁移的 .db 文件：{path}")
    with DiskManager(path) as disk:
        if disk.payload_codec.name == "manual":
            print(f"SKIP {path}: 已经是 manual")
            return

    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.manual.tmp")
    backup_path = path.with_name(f"{path.name}.pre-manual.bak")
    try:
        _migrate_to_temp(path, temp_path)
        if not replace:
            print(f"DRY-RUN {path}: 校验通过，临时文件 {temp_path}")
            return
        if backup_path.exists():
            raise FileExistsError(f"备份已存在，为避免覆盖请先处理：{backup_path}")
        os.replace(path, backup_path)
        try:
            os.replace(temp_path, path)
        except Exception:
            os.replace(backup_path, path)
            raise
        print(f"MIGRATED {path} -> manual；原文件备份：{backup_path}")
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="要迁移的 .db；不传时迁移当前目录下的 data/*.db",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="校验后替换原文件，并保留 .pre-manual.bak 备份",
    )
    return parser.parse_args()


def main() -> int:
    """执行命令行迁移。"""

    args = _parse_args()
    paths = args.paths or sorted(Path("data").glob("*.db"))
    if not paths:
        raise SystemExit("没有找到要迁移的 .db 文件")
    for path in paths:
        migrate_file(path, replace=args.write)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
