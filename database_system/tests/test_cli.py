"""页式存储调试 CLI。

这个工具故意不依赖 SQL、Catalog 或执行器，只使用 storage 层：

    python -m database_system.tests.test_cli DB pages
    python -m database_system.tests.test_cli DB page 1
    python -m database_system.tests.test_cli DB raw 1 --length 64
    python -m database_system.tests.test_cli DB buffer --fetch 1 2 1 --flush

写操作会直接修改数据库文件，适合临时调试，不建议对正式数据文件使用。
"""

from __future__ import annotations

import argparse
import os
import sys

from database_system.storage.buffer import BufferPoolManager
from database_system.storage.file_manager import DiskManager
from database_system.storage.page import Page
from database_system.utils.constants import PAGE_SIZE, PageType
from database_system.utils.errors import StorageError


def _hex(data: bytes, limit: int = 64) -> str:
    text = bytes(data[:limit]).hex(" ")
    if len(data) > limit:
        text += " ..."
    return text or "<empty>"


def _bytes(value: str) -> bytes:
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise StorageError(f"invalid hex bytes: {value!r}") from exc


def _page(disk: DiskManager, page_id: int) -> Page:
    return Page.decode(disk.read_page(page_id), page_id=page_id)


def _show_page(page: Page, record_limit: int = 64) -> None:
    print(
        f"page={page.page_id} type={page.page_type} next={page.next_page_id} "
        f"slots={page.num_slots} free_pointer={page.free_pointer} "
        f"free_space={page.free_space()}"
    )
    for slot_id in range(page.num_slots):
        if page.is_slot_deleted(slot_id):
            print(f"  slot={slot_id} deleted")
            continue
        offset, length = page.get_slot(slot_id)
        record = page.get_record(slot_id) or b""
        print(
            f"  slot={slot_id} offset={offset} length={length} "
            f"hex={_hex(record, record_limit)}"
        )


def cmd_info(disk: DiskManager, _args: argparse.Namespace) -> None:
    print(f"path={os.path.abspath(disk.path)}")
    print(f"page_size={PAGE_SIZE}")
    print(f"page_count={disk.page_count}")
    print(f"free_list_head={disk.free_list_head}")
    print(f"free_pages={sorted(disk.free_pages)}")
    print(f"catalog_root={disk.catalog_root}")


def cmd_pages(disk: DiskManager, _args: argparse.Namespace) -> None:
    print("page  type  next  slots  free_pointer  free_space  status")
    print("----  ----  ----  -----  ------------  ----------  ------")
    for page_id in range(1, disk.page_count):
        if page_id in disk.free_pages:
            print(f"{page_id:4}  -     -     -      -             -           FREE")
            continue
        try:
            page = _page(disk, page_id)
            print(
                f"{page_id:4}  {page.page_type:4}  {page.next_page_id:4}  "
                f"{page.num_slots:5}  {page.free_pointer:12}  "
                f"{page.free_space():10}  OK"
            )
        except StorageError as exc:
            print(f"{page_id:4}  ?     ?     ?      ?             ?           INVALID: {exc}")


def cmd_alloc(disk: DiskManager, args: argparse.Namespace) -> None:
    page_id = disk.allocate_page()
    if args.page_type == "CATALOG":
        page = Page(page_id)
        page.init(PageType.CATALOG)
        disk.write_page(page_id, page.encode())
    print(f"allocated page={page_id} type={args.page_type}")


def cmd_free(disk: DiskManager, args: argparse.Namespace) -> None:
    disk.deallocate_page(args.page_id)
    print(f"freed page={args.page_id}")


def cmd_page(disk: DiskManager, args: argparse.Namespace) -> None:
    _show_page(_page(disk, args.page_id), args.record_limit)


def cmd_raw(disk: DiskManager, args: argparse.Namespace) -> None:
    if args.offset < 0 or args.length < 0 or args.offset > PAGE_SIZE:
        raise StorageError("raw range is outside page boundary")
    data = disk.read_page(args.page_id)
    end = min(PAGE_SIZE, args.offset + args.length)
    print(f"page={args.page_id} offset={args.offset} length={end - args.offset}")
    print(_hex(data[args.offset:end], args.length))


def _write_page(disk: DiskManager, page: Page) -> None:
    disk.write_page(page.page_id, page.encode())


def cmd_insert(disk: DiskManager, args: argparse.Namespace) -> None:
    page = _page(disk, args.page_id)
    slot_id = page.insert_record(_bytes(args.data))
    _write_page(disk, page)
    print(f"inserted page={page.page_id} slot={slot_id}")


def cmd_update(disk: DiskManager, args: argparse.Namespace) -> None:
    page = _page(disk, args.page_id)
    if not page.update_record(args.slot_id, _bytes(args.data)):
        raise StorageError("update failed: slot does not exist or payload is too large")
    _write_page(disk, page)
    print(f"updated page={page.page_id} slot={args.slot_id}")


def cmd_delete(disk: DiskManager, args: argparse.Namespace) -> None:
    page = _page(disk, args.page_id)
    if not page.delete_record(args.slot_id):
        raise StorageError("delete failed: slot does not exist or is already deleted")
    _write_page(disk, page)
    print(f"deleted page={page.page_id} slot={args.slot_id}")


def cmd_next(disk: DiskManager, args: argparse.Namespace) -> None:
    page = _page(disk, args.page_id)
    page.next_page_id = args.next_page_id
    _write_page(disk, page)
    print(f"updated page={page.page_id} next={args.next_page_id}")


def cmd_write(disk: DiskManager, args: argparse.Namespace) -> None:
    if args.offset < 0 or args.offset > PAGE_SIZE:
        raise StorageError("write offset is outside page boundary")
    payload = _bytes(args.data)
    if args.offset + len(payload) > PAGE_SIZE:
        raise StorageError("write range exceeds page boundary")
    data = bytearray(disk.read_page(args.page_id))
    data[args.offset : args.offset + len(payload)] = payload
    disk.write_page(args.page_id, bytes(data))
    print(f"wrote page={args.page_id} offset={args.offset} length={len(payload)}")


def cmd_buffer(disk: DiskManager, args: argparse.Namespace) -> None:
    buffer = BufferPoolManager(
        disk,
        pool_size=args.pool_size,
        policy=args.policy,
        verbose=args.verbose,
    )
    for page_id in args.fetch:
        page = buffer.fetch_page(page_id)
        print(f"fetch page={page_id} pin={page.pin_count}")
        if not args.keep_pinned:
            buffer.unpin_page(page_id)
    for page_id_text, offset_text, data_text in args.write or []:
        page_id = int(page_id_text)
        offset = int(offset_text)
        payload = _bytes(data_text)
        if offset < 0 or offset + len(payload) > PAGE_SIZE:
            raise StorageError("buffer write range exceeds page boundary")
        page = buffer.fetch_page(page_id)
        page.data[offset : offset + len(payload)] = payload
        buffer.unpin_page(page_id, is_dirty=True)
        print(f"buffer-write page={page_id} offset={offset} length={len(payload)}")
    if args.flush:
        print(f"flushed={buffer.flush_all()}")
    print(f"stats={buffer.stats.to_dict()}")
    print(f"frames={list(buffer.frames)}")
    print("log:")
    for line in buffer.log:
        print(f"  {line}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniSQL 页式存储调试 CLI")
    parser.add_argument("database", help="数据库文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="查看超级块和空闲页信息").set_defaults(func=cmd_info)
    sub.add_parser("pages", help="列出所有页的概要").set_defaults(func=cmd_pages)

    alloc = sub.add_parser("alloc", help="分配一个新页")
    alloc.add_argument("--page-type", choices=("DATA", "CATALOG"), default="DATA")
    alloc.set_defaults(func=cmd_alloc)

    free = sub.add_parser("free", help="回收一个页面")
    free.add_argument("page_id", type=int)
    free.set_defaults(func=cmd_free)

    page = sub.add_parser("page", help="查看页头、槽位和记录")
    page.add_argument("page_id", type=int)
    page.add_argument("--record-limit", type=int, default=64)
    page.set_defaults(func=cmd_page)

    raw = sub.add_parser("raw", help="查看页的原始十六进制内容")
    raw.add_argument("page_id", type=int)
    raw.add_argument("--offset", type=int, default=0)
    raw.add_argument("--length", type=int, default=128)
    raw.set_defaults(func=cmd_raw)

    insert = sub.add_parser("insert", help="向页中插入十六进制记录")
    insert.add_argument("page_id", type=int)
    insert.add_argument("data", help="例如 01000000")
    insert.set_defaults(func=cmd_insert)

    update = sub.add_parser("update", help="原地更新槽位记录")
    update.add_argument("page_id", type=int)
    update.add_argument("slot_id", type=int)
    update.add_argument("data")
    update.set_defaults(func=cmd_update)

    delete = sub.add_parser("delete", help="删除槽位记录")
    delete.add_argument("page_id", type=int)
    delete.add_argument("slot_id", type=int)
    delete.set_defaults(func=cmd_delete)

    next_page = sub.add_parser("next", help="修改页链中的 next_page_id")
    next_page.add_argument("page_id", type=int)
    next_page.add_argument("next_page_id", type=int)
    next_page.set_defaults(func=cmd_next)

    write = sub.add_parser("write", help="直接覆盖页内指定字节")
    write.add_argument("page_id", type=int)
    write.add_argument("offset", type=int)
    write.add_argument("data", help="十六进制字节")
    write.set_defaults(func=cmd_write)

    buffer = sub.add_parser("buffer", help="观察缓冲池访问、淘汰和日志")
    buffer.add_argument("--fetch", nargs="+", type=int, required=True)
    buffer.add_argument("--pool-size", type=int, default=2)
    buffer.add_argument("--policy", choices=("LRU", "FIFO"), default="LRU")
    buffer.add_argument("--keep-pinned", action="store_true")
    buffer.add_argument("--flush", action="store_true")
    buffer.add_argument("--verbose", action="store_true")
    buffer.add_argument(
        "--write",
        nargs=3,
        action="append",
        metavar=("PAGE", "OFFSET", "HEX"),
        help="通过缓冲池修改页内字节并标记 dirty，可重复指定",
    )
    buffer.set_defaults(func=cmd_buffer)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with DiskManager(args.database) as disk:
            args.func(disk, args)
        return 0
    except (StorageError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
