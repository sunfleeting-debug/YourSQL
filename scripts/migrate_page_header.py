"""将页头从 26B 迁移到带 4B 对齐保留区的 30B 格式。"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path

from yoursql.common import DatabaseConfig
from yoursql.common.errors import StorageError
from yoursql.common.types import PageId
from yoursql.engine.security import RBAC
from yoursql.engine.catalog import Catalog, TableMetadata
from yoursql.engine.runtime.database import Database
from yoursql.storage import IndexManager, Page, PageType, SlottedPage, TableHeap
from yoursql.storage.index import _IndexNode
from yoursql.storage.page import (
    PAGE_MAGIC,
    SLOTTED_HEADER,
    SLOTTED_HEADER_SIZE,
    SLOTTED_MAGIC,
    SLOTTED_VERSION,
    SLOT_DELETED,
    SLOT_ENTRY,
    SLOT_ENTRY_SIZE,
)


OLD_PAGE_HEADER = struct.Struct("<4sIBBQI I")
OLD_PAGE_HEADER_SIZE = OLD_PAGE_HEADER.size
OLD_PAGE_VERSION = 1
PAGE_SIZE_CANDIDATES = (512, 1024, 2048, 4096, 8192, 16 * 1024, 32 * 1024, 64 * 1024, 128 * 1024)


def page_type_from_code(code: int) -> PageType:
    """将旧页头编码转换为 PageType。"""
    values = tuple(PageType)
    if code < 1 or code > len(values):
        raise ValueError(f"未知页类型编码: {code}")
    return values[code - 1]


def _parse_old_page(raw: bytes, page_id: int, page_size: int) -> tuple[int, PageType, bytes]:
    """解析一页旧格式数据，保留旧页号和负载供迁移使用。"""

    if len(raw) != page_size or len(raw) < OLD_PAGE_HEADER_SIZE:
        raise ValueError(f"旧页 {page_id} 长度非法")
    magic, version, type_code, _reserved, stored_page_id, payload_length, checksum = OLD_PAGE_HEADER.unpack(
        raw[:OLD_PAGE_HEADER_SIZE]
    )
    if magic != PAGE_MAGIC or version != OLD_PAGE_VERSION or stored_page_id != page_id:
        raise ValueError(f"旧页头校验失败: 第 {page_id} 页")
    end = OLD_PAGE_HEADER_SIZE + payload_length
    if end > page_size:
        raise ValueError(f"旧页 {page_id} 负载长度非法")
    payload = raw[OLD_PAGE_HEADER_SIZE:end]
    if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
        raise ValueError(f"旧页 CRC 校验失败: 第 {page_id} 页")
    return page_id, page_type_from_code(type_code), payload


def detect_page_size(path: Path) -> int:
    """读取旧 superblock，确定数据库页大小。"""

    for page_size in PAGE_SIZE_CANDIDATES:
        with path.open("rb") as stream:
            raw = stream.read(page_size)
        if len(raw) != page_size:
            continue
        try:
            _page_id, page_type, payload = _parse_old_page(raw, 0, page_size)
            if page_type is not PageType.SUPERBLOCK:
                continue
            metadata = json.loads(payload.decode("utf-8"))
            if int(metadata.get("page_size", 0)) == page_size:
                return page_size
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            continue
    raise ValueError(f"无法识别数据库页大小: {path}")


def iter_old_pages(path: Path, page_size: int) -> Iterator[tuple[int, PageType, bytes]]:
    """流式读取旧页，避免大数据库一次性读入内存。"""

    file_size = path.stat().st_size
    if file_size % page_size != 0:
        raise ValueError(f"文件长度不是页大小的整数倍: {path}")
    with path.open("rb") as stream:
        for page_id in range(file_size // page_size):
            raw = stream.read(page_size)
            yield _parse_old_page(raw, page_id, page_size)


def read_old_page(path: Path, page_size: int, page_id: int) -> tuple[int, PageType, bytes]:
    """随机读取旧页，用于逻辑重建时定位目录页。"""

    with path.open("rb") as stream:
        stream.seek(page_id * page_size)
        return _parse_old_page(stream.read(page_size), page_id, page_size)


def decode_old_heap_slots(payload: bytes) -> list[bytes | None] | None:
    """从旧 MSP2 负载提取槽记录，供新页容量重新打包。"""

    if not payload.startswith(SLOTTED_MAGIC):
        return None
    if len(payload) < SLOTTED_HEADER_SIZE:
        raise ValueError("旧 MSP2 槽式页头不完整")
    magic, version, _flags, slot_count, free_start, free_end = SLOTTED_HEADER.unpack(payload[:SLOTTED_HEADER_SIZE])
    directory_end = SLOTTED_HEADER_SIZE + slot_count * SLOT_ENTRY_SIZE
    if magic != SLOTTED_MAGIC or version != SLOTTED_VERSION or free_start != directory_end or free_end > len(payload):
        raise ValueError("旧 MSP2 槽式页边界非法")
    slots: list[bytes | None] = []
    for slot_id in range(slot_count):
        entry_offset = SLOTTED_HEADER_SIZE + slot_id * SLOT_ENTRY_SIZE
        record_offset, record_length, flags, _reserved = SLOT_ENTRY.unpack(payload[entry_offset:entry_offset + SLOT_ENTRY_SIZE])
        if flags & SLOT_DELETED:
            slots.append(None)
            continue
        record_end = record_offset + record_length
        if record_offset < free_end or record_end > len(payload):
            raise ValueError(f"旧 MSP2 槽 {slot_id} 记录范围非法")
        slots.append(bytes(payload[record_offset:record_end]))
    return slots


def decode_legacy_heap_slots(payload: bytes) -> list[bytes | None]:
    """读取旧 JSON/Base64 槽页，供不兼容页容量时逻辑重建。"""

    try:
        value = json.loads(payload.decode("utf-8")) if payload else {"slots": []}
        encoded_slots = value["slots"]
        if not isinstance(encoded_slots, list):
            raise TypeError("slots 不是数组")
        result: list[bytes | None] = []
        for encoded in encoded_slots:
            if encoded is None:
                result.append(None)
            elif isinstance(encoded, str):
                result.append(base64.b64decode(encoded))
            else:
                raise TypeError("槽位不是 Base64 字符串或 null")
        return result
    except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise StorageError("旧 JSON 槽式页损坏") from exc


def decode_heap_slots(payload: bytes) -> list[bytes | None]:
    """统一读取 MSP2 与旧 JSON 槽页。"""

    slots = decode_old_heap_slots(payload)
    if slots is not None:
        return slots
    if not payload:
        return []
    return decode_legacy_heap_slots(payload)


def migrate_page(page_id: int, page_type: PageType, payload: bytes, page_size: int) -> Page:
    """将单个旧页转换为新页格式。"""
    if page_type is PageType.HEAP:
        slots = decode_old_heap_slots(payload)
        if slots is not None:
            return SlottedPage(page_id, page_size, slots).to_page()
    if len(payload) > page_size - Page.HEADER_SIZE and page_type is PageType.INDEX:
        # 极满的旧索引页需要重新序列化，避免新页头减少 4B 后越过容量。
        old_page = Page(page_id, page_size + 4, page_type, payload)
        payload = _IndexNode.from_page(old_page).payload()
    return Page(page_id, page_size, page_type, payload)


def _old_catalog(path: Path, page_size: int) -> Catalog:
    """从旧数据库读取目录元数据。"""
    _page_id, page_type, payload = read_old_page(path, page_size, 0)
    if page_type is not PageType.SUPERBLOCK:
        raise StorageError("旧数据库第 0 页不是 superblock")
    metadata = json.loads(payload.decode("utf-8"))
    named_pages = metadata.get("named_pages", {})
    if not isinstance(named_pages, dict) or "catalog" not in named_pages:
        raise StorageError("旧数据库缺少命名 catalog 页")
    catalog_page_id = int(named_pages["catalog"])
    _catalog_id, catalog_type, catalog_payload = read_old_page(path, page_size, catalog_page_id)
    if catalog_type is not PageType.CATALOG:
        raise StorageError("旧数据库 catalog 页类型错误")
    catalog_data = json.loads(catalog_payload.decode("utf-8")) if catalog_payload else {}
    if not isinstance(catalog_data, dict):
        raise StorageError("旧数据库 catalog 不是对象")
    return Catalog.from_dict(catalog_data)


def _collect_rows(path: Path, page_size: int, catalog: Catalog) -> dict[int, list[tuple[object, ...]]]:
    """按旧目录顺序收集所有活记录；逻辑重建会重新分配 RowId。"""

    owners: dict[int, int] = {}
    rows: dict[int, list[tuple[object, ...]]] = {int(table.table_id): [] for table in catalog.tables()}
    for table in catalog.tables():
        for page_id in table.page_ids:
            normalized = int(page_id)
            if normalized in owners and owners[normalized] != int(table.table_id):
                raise StorageError(f"旧页 {normalized} 同时属于多张表")
            owners[normalized] = int(table.table_id)

    for page_id, page_type, payload in iter_old_pages(path, page_size):
        table_id = owners.get(page_id)
        if table_id is None:
            continue
        if page_type is not PageType.HEAP:
            raise StorageError(f"旧目录把第 {page_id} 页标记为表数据，但页类型为 {page_type.value}")
        for raw in decode_heap_slots(payload):
            if raw is not None:
                rows[table_id].append(TableHeap._decode(raw))
    return rows


def _pack_rows(database: Database, table: TableMetadata, rows: list[tuple[object, ...]], page_size: int) -> None:
    """按新页容量重新打包一张表，保持槽序但不保留旧物理页号。"""

    page_ids: list[PageId] = []
    current: list[bytes] = []

    def flush_current() -> None:
        """将当前待写记录刷新到新的堆页。"""
        if not current:
            return
        page = database.buffer_pool.new_page(PageType.HEAP)
        slotted = SlottedPage(page.page_id, page_size, list(current))
        database.buffer_pool.put_page(slotted.to_page(), dirty=True)
        page_ids.append(PageId(page.page_id))
        current.clear()

    for row in rows:
        encoded = TableHeap._encode(row)
        candidate = [*current, encoded]
        try:
            SlottedPage(0, page_size, candidate).to_page()
        except StorageError as exc:
            if not current:
                raise StorageError(f"表 {table.name!r} 存在无法放入新页的单条记录") from exc
            flush_current()
            current.append(encoded)
            try:
                SlottedPage(0, page_size, current).to_page()
            except StorageError as retry_exc:
                raise StorageError(f"表 {table.name!r} 存在无法放入新页的单条记录") from retry_exc
        else:
            current.append(encoded)
    flush_current()

    table.page_ids = page_ids
    table.first_page_id = page_ids[0] if page_ids else None
    table.row_count = len(rows)
    database._heaps[int(table.table_id)] = TableHeap(database.buffer_pool, [int(page_id) for page_id in page_ids])


def rebuild_database(path: Path, page_size: int) -> None:
    """当单页因减少 4B 无法原位重排时，重建逻辑数据和索引。"""

    catalog = _old_catalog(path, page_size)
    rows = _collect_rows(path, page_size, catalog)
    temporary = path.with_name(f".{path.name}.page-header-v2-rebuild.tmp")
    temporary.unlink(missing_ok=True)
    try:
        config = DatabaseConfig(page_size=page_size, buffer_pool_size=256)
        with Database(temporary, config=config) as database:
            database.catalog = catalog
            database.rbac = RBAC.from_dict(catalog.security or None)
            database.index_manager = IndexManager()
            database._heaps.clear()
            for table in database.catalog.tables():
                _pack_rows(database, table, rows[int(table.table_id)], page_size)
            for metadata in database.catalog.indexes():
                metadata.root_page_id = None
            database._rebuild_indexes()
            database._persist_catalog()
            database.buffer_pool.flush_all()
        temporary.replace(path)
        print(f"已逻辑重建: {path} · {page_size} B/页 · {sum(len(value) for value in rows.values())} 行")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def migrate_raw(path: Path, page_size: int, *, write: bool) -> int:
    """原位转换可容纳的新页；大文件使用流式读写。"""

    temporary = path.with_name(f".{path.name}.page-header-v2.tmp")
    input_size = path.stat().st_size
    count = 0
    try:
        output_stream = temporary.open("wb") if write else None
        try:
            for page_id, page_type, payload in iter_old_pages(path, page_size):
                migrated = migrate_page(page_id, page_type, payload, page_size)
                encoded = migrated.to_bytes()
                if output_stream is not None:
                    output_stream.write(encoded)
                count += 1
        finally:
            if output_stream is not None:
                output_stream.close()
        if write:
            if temporary.stat().st_size != input_size:
                raise ValueError(f"迁移后文件大小变化异常: {path}")
            temporary.replace(path)
    except Exception:
        if write:
            temporary.unlink(missing_ok=True)
        raise
    return count


def migrate(path: Path, *, write: bool) -> None:
    """迁移数据库文件，必要时回退到逻辑重建。"""
    page_size = detect_page_size(path)
    try:
        page_count = migrate_raw(path, page_size, write=write)
    except (MemoryError, OSError, StorageError, ValueError, TypeError, json.JSONDecodeError) as exc:
        if not write:
            print(f"需要逻辑重建: {path} · 原位迁移原因: {exc}")
            return
        print(f"原位迁移无法容纳，转为逻辑重建: {path} · {exc}")
        rebuild_database(path, page_size)
        return
    mode = "已迁移" if write else "可迁移"
    print(f"{mode}: {path} · {page_count} 页 · {page_size} B/页")


def main() -> None:
    """解析命令行参数并启动当前脚本任务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--write", action="store_true", help="写回原数据库文件")
    args = parser.parse_args()
    for path in args.paths:
        migrate(path, write=args.write)


if __name__ == "__main__":
    main()
