"""【前端特供】存储只读检查和页面地图数据，不触碰缓存替换状态。"""

from __future__ import annotations

import base64
import json
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from yoursql.common import JsonObject
from yoursql.common.errors import YourSQLError
from yoursql.storage.heap import TableHeap
from yoursql.storage.index import index_page_info
from yoursql.storage.page import (
    HEADER_SIZE,
    PAGE_VERSION,
    SLOT_ENTRY_SIZE,
    Page,
    PageType,
    SlottedPage,
)
from yoursql.engine.catalog import TableMetadata
from yoursql.engine.runtime.database import Database

__all__ = [
    "authorize_storage",
    "inspect_index",
    "inspect_page",
    "page_header",
    "storage_cache_snapshot",
    "storage_index_snapshot",
    "storage_page_changes",
    "storage_settings_snapshot",
    "storage_snapshot",
]


@dataclass(frozen=True)
class IndexPageBinding:
    """【前端特供】索引物理页到目录元数据的绑定。"""

    index_name: str
    table: TableMetadata | None


def authorize_storage(database: Database) -> None:
    """【前端特供】校验存储检查接口所需的全库读取和安全管理权限。"""
    database.session.authorize("SECURITY")
    database.session.authorize("SELECT")


def _is_admin(database: Database) -> bool:
    """admin 角色可以查看内部权限表的调试内容。"""

    user = database.session.user
    return user.name.lower() == "admin" or "admin" in user.roles


def _table_for_page(database: Database, page_id: int) -> TableMetadata | None:
    """根据页号查找所属的表元数据。"""
    for table in database.catalog.tables(include_system=True):
        if page_id in {int(item) for item in table.page_ids}:
            return table
    return None


def _index_pages(
    database: Database,
) -> dict[int, list[IndexPageBinding]]:
    """建立索引物理页到索引目录项的反向映射。"""

    tables = {
        table.table_id: table for table in database.catalog.tables(include_system=True)
    }
    bindings: dict[int, list[IndexPageBinding]] = {}
    for metadata in database.catalog.indexes():
        try:
            page_ids = database.index_manager.get(metadata.name).physical_page_ids(
                readonly=True
            )
        except YourSQLError:
            # WHY：目录可能保留尚未完成重建的索引项，页面检查仍应返回可用的页信息。
            continue
        table = tables.get(metadata.table_id)
        for page_id in page_ids:
            bindings.setdefault(int(page_id), []).append(
                IndexPageBinding(metadata.name, table)
            )
    return bindings


def _display_row(raw: bytes | None, masked: bool) -> list[object] | None:
    """内部权限页非管理员只返回同列数的占位值，保留槽位结构。"""

    if raw is None:
        return None
    row = list(TableHeap._decode(raw))
    return ["MASKED"] * len(row) if masked else row


def _catalog_content(database: Database, admin: bool) -> JsonObject:
    """【前端特供】返回目录页对应的完整结构化内容，供工作台展开查看。"""

    tables = database.catalog.tables()
    table_by_id = {
        int(table.table_id): table
        for table in database.catalog.tables(include_system=True)
    }
    views = tuple(view for view in database.catalog.views() if admin or not view.system)
    indexes = tuple(
        index
        for index in database.catalog.indexes()
        if admin
        or not (
            table_by_id.get(int(index.table_id))
            and table_by_id[int(index.table_id)].system
        )
    )
    return {
        "version": database.catalog.VERSION,
        "tables": [table.to_dict() for table in tables],
        "views": [view.to_dict() for view in views],
        "indexes": [index.to_dict() for index in indexes],
        "system_tables": [table.to_dict() for table in database.catalog.system_tables()]
        if admin
        else "MASKED",
        "permission_storage": "internal_tables" if admin else "MASKED",
    }


def page_header(
    page: Page,
    database: Database | None = None,
    *,
    index_pages: Mapping[int, list[IndexPageBinding]] | None = None,
) -> JsonObject:
    """【前端特供】构造工作台展示用的页头摘要。"""
    result: JsonObject = {
        "page_id": page.page_id,
        "type": page.page_type.value,
        "page_size": page.page_size,
        "header_size": HEADER_SIZE,
        "payload_size": len(page.payload),
        "free_space": page.free_space,
        "magic": "MDBP",
        "version": PAGE_VERSION,
        "crc32": f"{zlib.crc32(page.payload) & 0xFFFFFFFF:08x}",
    }
    if page.page_type is PageType.HEAP:
        slotted = SlottedPage.from_page(page)
        result.update(
            {
                "free_space": slotted.free_space,
                "storage_format": slotted.storage_format,
                "slot_count": len(slotted.slots),
                "logical_used_space": page.page_size - slotted.free_space,
                "logical_free_space": slotted.free_space,
            }
        )
    elif page.page_type is PageType.INDEX:
        try:
            node = index_page_info(page, offset=0, limit=32)
            result.update(
                {
                    "index_format": node.format,
                    "index_node_type": node.node_type,
                    "index_level": node.level,
                    "index_key_count": node.key_count,
                }
            )
        except YourSQLError:
            result.update({"index_format": "unknown", "index_node_type": "corrupt"})
    if database is not None:
        table = _table_for_page(database, page.page_id)
        if table is not None:
            result["table_name"] = table.name if _is_admin(database) else "MASKED"
            result["system_table"] = table.system
            result["masked"] = table.system and not _is_admin(database)
        if page.page_type is PageType.INDEX:
            bindings = _index_pages(database) if index_pages is None else index_pages
            for binding in bindings.get(page.page_id, []):
                index_name = binding.index_name
                index_table = binding.table
                is_masked = (
                    index_table is not None
                    and index_table.system
                    and not _is_admin(database)
                )
                result["index_name"] = index_name if not is_masked else "MASKED"
                result["table_name"] = (
                    index_table.name
                    if index_table is not None and not is_masked
                    else "MASKED"
                )
                result["system_table"] = (
                    index_table.system if index_table is not None else False
                )
                result["masked"] = is_masked
                break
    return result


def storage_snapshot(
    database: Database, offset: int, limit: int, *, map_only: bool = False
) -> JsonObject:
    """【前端特供】返回页面地图；map_only 只给画地图必需的页头字段。"""

    authorize_storage(database)
    disk = database.disk
    metadata = disk.metadata()
    buffer_pool = database.buffer_pool.snapshot(0, limit)
    # WHY：网格只按页类型与占用率着色，表名/索引名只在选中页的关联面板里用；逐页查目录
    # 与整张索引绑定表在 4800+ 页的演示库上每次要 0.6–1.0 s，分批加载会乘以批次数，
    # 所以轻量模式不算标签，选中页改由 /api/storage/pages/{id} 按需补齐。
    index_pages = {} if map_only else _index_pages(database)
    pages = [
        page_header(
            database.buffer_pool.peek_page(page_id),
            None if map_only else database,
            index_pages=index_pages,
        )
        for page_id in range(offset, min(offset + limit, disk.page_count))
    ]
    files = [
        {
            "name": database.path.name,
            "size_bytes": database.path.stat().st_size if database.path.exists() else 0,
        }
    ]
    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "readonly": True,
        "map_only": map_only,
        "files": files,
        "pages": pages,
        "total": disk.page_count,
        "offset": offset,
        "limit": limit,
        "page_size": disk.page_size,
        "next_page_id": metadata.next_page_id,
        "free_pages": list(metadata.free_pages)[offset : offset + limit],
        "free_page_count": len(metadata.free_pages),
        "named_pages": dict(metadata.named_pages),
        "system_tables": [table.to_dict() for table in database.catalog.system_tables()]
        if _is_admin(database)
        else "MASKED",
        "storage_revision": buffer_pool.revision,
        "buffer_pool": buffer_pool.to_dict(),
        "io": disk.io_stats().to_dict(),
        "indexes": [index.to_dict() for index in database.catalog.indexes()][:limit],
        "limitations": [
            "B+Tree 的内部页、叶子页、分裂/合并和 key/RowId 均已落盘。",
            f"HEAP 页统一采用双向槽式布局，记录区保存 {database.payload_codec.name} 行数据。",
            "I/O 指标是本进程页接口调用计数，不是操作系统物理磁盘或设备吞吐量。",
        ],
    }


def storage_page_changes(database: Database, since: int, limit: int) -> JsonObject:
    """【前端特供】按内容变更游标返回页头增量，避免重复读取整张页面地图。"""

    authorize_storage(database)
    changes = database.buffer_pool.changes_since(since)
    page_ids = [int(page_id) for page_id in changes.changed_page_ids]
    revision = changes.revision
    truncated = changes.truncated or len(page_ids) > limit
    if truncated:
        return {
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "readonly": True,
            "since": since,
            "revision": revision,
            "changed_page_ids": page_ids[:limit],
            "pages": [],
            "truncated": True,
            "note": "页级变更日志超出保留范围，请重新加载页面地图。",
        }

    disk = database.disk
    metadata = disk.metadata()
    index_pages = _index_pages(database)
    pages = [
        page_header(
            database.buffer_pool.peek_page(page_id), database, index_pages=index_pages
        )
        for page_id in page_ids
        if 0 <= page_id < disk.page_count
    ]
    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "readonly": True,
        "since": since,
        "revision": revision,
        "changed_page_ids": page_ids,
        "pages": pages,
        "total": disk.page_count,
        "page_size": disk.page_size,
        "free_pages": list(metadata.free_pages),
        "free_page_count": len(metadata.free_pages),
        "truncated": False,
    }


def storage_cache_snapshot(database: Database, offset: int, limit: int) -> JsonObject:
    """【前端特供】只读取 BufferPool 运行态，无需重新加载页面地图。"""

    authorize_storage(database)
    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "readonly": True,
        "buffer_pool": database.buffer_pool.snapshot(offset, limit).to_dict(),
        "io": database.disk.io_stats().to_dict(),
        "note": "缓存统计为进程内累计值；读取本接口不会 pin、淘汰或改变替换时钟。",
    }


def storage_settings_snapshot(database: Database) -> JsonObject:
    """【前端特供】返回设置弹窗所需的最小运行参数，不重建索引页映射。"""

    authorize_storage(database)
    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "readonly": True,
        "page_size": database.disk.page_size,
        "buffer_pool": database.buffer_pool.snapshot(0, 100).to_dict(),
    }


def storage_index_snapshot(database: Database, limit: int) -> JsonObject:
    """【前端特供】只刷新索引目录，索引详情和页面地图保持不动。"""

    authorize_storage(database)
    return {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "readonly": True,
        "indexes": [index.to_dict() for index in database.catalog.indexes()][:limit],
    }


def inspect_page(
    database: Database,
    page_id: int,
    offset: int,
    limit: int,
    *,
    index_name: str | None = None,
    resolve_index: bool = True,
) -> JsonObject:
    """【前端特供】读取并返回指定页的只读检查信息。"""
    authorize_storage(database)
    if page_id >= database.disk.page_count:
        raise YourSQLError("页不存在", "NOT_FOUND")
    page = database.buffer_pool.peek_page(page_id)
    admin = _is_admin(database)
    table = _table_for_page(database, page_id)
    masked = table is not None and table.system and not admin
    if page.page_type is PageType.INDEX and index_name:
        # WHY：索引检查页已经携带所属索引名；直接使用已验证的目录项，避免每次点击都遍历所有 B+Tree。
        metadata = database.catalog.get_index(index_name)
        table = next(
            (
                item
                for item in database.catalog.tables(include_system=True)
                if item.table_id == metadata.table_id
            ),
            None,
        )
        index_pages = {page_id: [IndexPageBinding(metadata.name, table)]}
    elif resolve_index:
        index_pages = _index_pages(database) if page.page_type is PageType.INDEX else None
    else:
        # WHY：页面地图点击只需要页详情；索引归属映射可选，不能阻塞索引页的首屏响应。
        index_pages = {} if page.page_type is PageType.INDEX else None
    result = page_header(page, database, index_pages=index_pages)
    result.update(
        {
            "readonly": True,
            "offset": offset,
            "limit": limit,
            "source": "buffer" if page_id in database.buffer_pool else "disk",
        }
    )
    # WHY：内部用户表包含密码哈希；管理员可调试，其他安全审计会话只看布局摘要。
    expose_raw = admin or (page.page_type is not PageType.CATALOG and not masked)
    if expose_raw:
        raw_limit = min(max(1, limit) * 4096, 16_384)
        raw = page.payload[:raw_limit]
        full_page = page.to_bytes()
        if page.page_type is PageType.HEAP:
            # 双向页 payload 含二进制页头和槽目录，保留可读摘要；raw_page 仍提供真实字节。
            slotted_preview = SlottedPage.from_page(page)
            layout_preview = slotted_preview.layout_metadata()
            raw_text = json.dumps(
                {
                    "format": layout_preview.get("format"),
                    "slot_count": layout_preview.get("slot_count"),
                    "slot_directory": layout_preview.get("slot_directory"),
                    "free_region": layout_preview.get("free_region"),
                    "free_regions": layout_preview.get("free_regions"),
                    "record_region": layout_preview.get("record_region"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            raw_encoding = "utf-8"
        else:
            try:
                raw_text = raw.decode("utf-8")
                raw_encoding = "utf-8"
            except UnicodeDecodeError:
                raw_text = None
                raw_encoding = "binary"
        result["raw_payload"] = {
            "encoding": raw_encoding,
            "size_bytes": len(page.payload),
            "preview_bytes": len(raw),
            "truncated": len(raw) < len(page.payload),
            "text": raw_text,
            "hex": raw.hex(" "),
            "base64": base64.b64encode(raw).decode("ascii"),
            "note": "仅显示页 payload 前 16 KiB；双向 HEAP 页的 text 是布局摘要，raw_page 才是完整二进制字节。",
        }
        # WHY：页格视图需要把固定页的每个区域映射到确定偏移，返回完整单页而不是让前端猜测。
        result["raw_page"] = {
            "encoding": "binary",
            "size_bytes": len(full_page),
            "preview_bytes": len(full_page),
            "truncated": False,
            "text": None,
            "hex": full_page.hex(" "),
            "base64": base64.b64encode(full_page).decode("ascii"),
            "note": "只读返回当前页完整固定长度字节；页格视图按槽目录项宽度拆分单元。",
        }
    if not expose_raw:
        result["masked"] = True
        result["mask_reason"] = "内部权限表仅对 admin 公开原始字节。"
    if page.page_type == PageType.HEAP:
        slotted = SlottedPage.from_page(page)
        slot_rows = []
        # HOW：双向页的槽目录保存真实 offset/length，记录区从固定页尾向前分配。
        layout = slotted.layout_metadata()
        result["physical_layout"] = layout
        layout_slots = layout.get("slots", [])
        if not isinstance(layout_slots, list):
            layout_slots = []
        for slot_id, raw in enumerate(slotted.slots):
            entry = (
                layout_slots[slot_id]
                if slot_id < len(layout_slots)
                and isinstance(layout_slots[slot_id], dict)
                else {}
            )
            entry_deleted = bool(entry.get("deleted"))
            if entry_deleted:
                page_offset = None
            elif isinstance(entry.get("offset"), int):
                page_offset = entry["offset"]
            else:
                page_offset = None
            byte_length = (
                entry.get("length") if isinstance(entry.get("length"), int) else 0
            )
            directory_offset = (
                entry.get("directory_offset")
                if isinstance(entry.get("directory_offset"), int)
                else None
            )
            if offset <= slot_id < offset + limit:
                slot_rows.append(
                    {
                        "slot_id": slot_id,
                        "deleted": raw is None,
                        "record_bytes": len(raw) if raw else 0,
                        "row": _display_row(raw, masked),
                        "page_offset": page_offset,
                        "byte_length": byte_length,
                        "slot_directory_offset": directory_offset,
                        "slot_directory_length": SLOT_ENTRY_SIZE,
                        "storage_encoding": f"{database.payload_codec.name} row payload bytes",
                    }
                )
        result["layout"] = (
            f"双向槽式页：槽目录向前增长，空闲片段按真实范围分布，{database.payload_codec.name} 记录区从页尾向前增长。"
        )
        result["slots"] = slot_rows
        result["total_slots"] = len(slotted.slots)
    elif page.page_type == PageType.CATALOG:
        result["catalog"] = {
            "tables": [table.to_dict() for table in database.catalog.tables()][
                offset : offset + limit
            ],
            "system_tables": [
                table.to_dict() for table in database.catalog.system_tables()
            ]
            if admin
            else "MASKED",
            "permission_storage": "internal_tables" if admin else "MASKED",
        }
        result["catalog_content"] = _catalog_content(database, admin)
    elif page.page_type == PageType.SUPERBLOCK:
        result["metadata"] = database.disk.metadata().to_dict()
    elif page.page_type == PageType.INDEX:
        # INDEX 页现在是真实 B+Tree 节点；旧数据库留下的空根页仍由 index_page_info
        result["index_node"] = index_page_info(
            page, offset=offset, limit=min(limit, 100)
        ).to_dict()
        result["note"] = (
            "真实落盘 B+Tree 节点；entries 是叶子 key/RowId，children 是内部页子节点。"
        )
    else:
        result["note"] = "该页没有可展示的记录。"
    return result


def inspect_index(database: Database, name: str, offset: int, limit: int) -> JsonObject:
    """【前端特供】读取并返回指定索引的只读检查信息。"""
    authorize_storage(database)
    metadata = database.catalog.get_index(name)
    return {
        "metadata": metadata.to_dict(),
        **database.index_manager.get(name).snapshot(offset, limit).to_dict(),
    }
