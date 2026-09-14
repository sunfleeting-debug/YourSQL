"""单文件页式磁盘管理器和 superblock。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock
from typing import Mapping

from ..common.errors import StorageError
from ..common.trace import current_trace
from .page import Page, PageType


class DiskManager:
    """以固定页大小读写一个数据库文件。"""

    FORMAT_VERSION = 1

    def __init__(self, path: str | os.PathLike[str], *, page_size: int = 4096) -> None:
        self.path = Path(path)
        self.page_size = page_size
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._closed = False
        self._reads = 0
        self._writes = 0
        exists = self.path.exists() and self.path.stat().st_size > 0
        self._file = self.path.open("r+b" if exists else "w+b")
        self._free_pages: set[int] = set()
        self._next_page_id = 1
        self._named_pages: dict[str, int] = {}
        if exists:
            self._load_superblock()
        else:
            self._write_raw(Page.empty(0, page_size, PageType.SUPERBLOCK))
            self._write_superblock()
            self.sync()

    @property
    def page_count(self) -> int:
        with self._lock:
            # WHY：BufferedRandom.seek 会隐式刷出待写字节，元数据查看不能触发写盘。
            # 页文件只扩展不收缩，next_page_id 即当前逻辑文件页数。
            return self._next_page_id

    def metadata(self) -> dict[str, object]:
        with self._lock:
            return {
                "page_size": self.page_size,
                "page_count": self.page_count,
                "next_page_id": self._next_page_id,
                "free_pages": sorted(self._free_pages),
                "named_pages": dict(self._named_pages),
            }

    def _ensure_open(self) -> None:
        if self._closed:
            raise StorageError("数据库文件已经关闭")

    def _load_superblock(self) -> None:
        raw = self._read_raw(0)
        page = Page.from_bytes(raw, page_size=self.page_size)
        if page.page_type is not PageType.SUPERBLOCK:
            raise StorageError("第 0 页不是 superblock")
        try:
            data = json.loads(page.payload.decode("utf-8")) if page.payload else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise StorageError("superblock JSON 损坏") from exc
        if data.get("magic") != "YOURSQLMS":
            raise StorageError("数据库文件魔数错误")
        if int(data.get("version", 0)) > self.FORMAT_VERSION:
            raise StorageError("数据库文件版本过高")
        stored_size = int(data.get("page_size", self.page_size))
        if stored_size != self.page_size:
            raise StorageError(
                f"页大小不匹配，文件为 {stored_size}，配置为 {self.page_size}"
            )
        self._next_page_id = max(1, int(data.get("next_page_id", 1)))
        self._free_pages = {int(value) for value in data.get("free_pages", [])}
        raw_named = data.get("named_pages", {})
        self._named_pages = (
            {str(key): int(value) for key, value in raw_named.items()}
            if isinstance(raw_named, Mapping)
            else {}
        )

    def _superblock_payload(self) -> bytes:
        data = {
            "magic": "YOURSQLMS",
            "version": self.FORMAT_VERSION,
            "page_size": self.page_size,
            "next_page_id": self._next_page_id,
            "free_pages": sorted(self._free_pages),
            "named_pages": self._named_pages,
        }
        return json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _read_raw(self, page_id: int) -> bytes:
        self._reads += 1
        trace = current_trace.get()
        if trace is not None:
            trace.event("disk_read", page_id)
        self._file.seek(page_id * self.page_size)
        raw = self._file.read(self.page_size)
        if len(raw) != self.page_size:
            raise StorageError(f"页 {page_id} 不存在或文件被截断")
        return raw

    def _write_raw(self, page: Page) -> None:
        self._writes += 1
        trace = current_trace.get()
        if trace is not None:
            trace.event("disk_write", page.page_id)
        self._file.seek(page.page_id * self.page_size)
        self._file.write(page.to_bytes())

    def _write_superblock(self) -> None:
        self._write_raw(
            Page(0, self.page_size, PageType.SUPERBLOCK, self._superblock_payload())
        )

    def register_named_page(self, name: str, page_id: int) -> None:
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            self._named_pages[name.strip().lower()] = int(page_id)
            self._write_superblock()

    def named_page(self, name: str) -> int | None:
        with self._lock:
            return self._named_pages.get(name.strip().lower())

    def allocate(
        self, page_type: PageType = PageType.FREE, payload: bytes = b""
    ) -> Page:
        with self._lock:
            self._ensure_open()
            if self._free_pages:
                page_id = min(self._free_pages)
                self._free_pages.remove(page_id)
            else:
                page_id = self._next_page_id
                self._next_page_id += 1
            page = Page(page_id, self.page_size, page_type, payload)
            self._write_raw(page)
            self._write_superblock()
            return page

    def free(self, page_id: int) -> None:
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            if page_id == 0:
                raise StorageError("不能释放 superblock")
            self._write_raw(Page.empty(page_id, self.page_size, PageType.FREE))
            self._free_pages.add(int(page_id))
            for key, value in tuple(self._named_pages.items()):
                if value == page_id:
                    del self._named_pages[key]
            self._write_superblock()

    def _check_page_id(self, page_id: int) -> None:
        if page_id < 0 or page_id >= self.page_count:
            raise StorageError(f"页号 {page_id} 越界")

    def read(self, page_id: int) -> Page:
        with self._lock:
            self._ensure_open()
            self._check_page_id(int(page_id))
            return Page.from_bytes(
                self._read_raw(int(page_id)), page_size=self.page_size
            )

    def write(self, page: Page) -> None:
        with self._lock:
            self._ensure_open()
            if page.page_size != self.page_size:
                raise StorageError("页大小与文件不一致")
            self._check_page_id(page.page_id)
            self._write_raw(page)

    def io_stats(self) -> dict[str, int]:
        """返回进程内页 I/O 计数；调试读取单独排除。"""
        with self._lock:
            return {
                "page_reads": self._reads,
                "page_writes": self._writes,
                "bytes_read": self._reads * self.page_size,
                "bytes_written": self._writes * self.page_size,
            }

    def peek(self, page_id: int) -> Page:
        """只读调试页，恢复文件游标且不改变正常读写计数。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            if page_id == 0:
                return Page(
                    0, self.page_size, PageType.SUPERBLOCK, self._superblock_payload()
                )
            # WHY：正常读写走 BufferedRandom，未 flush 的写入独立句柄看不到；页面被缓冲池淘汰后
            # 再经此路径读取会拿到旧内容（实测 33 页索引 + 32 帧缓冲池会把叶页读成空页）。
            # flush 不改变逻辑游标也不计入 I/O 统计，仍然保持“只读调试”语义。
            self._file.flush()
            # 独立只读句柄不会移动 BufferedRandom 游标，也不会触发隐式 flush。
            with self.path.open("rb") as stream:
                stream.seek(page_id * self.page_size)
                return Page.from_bytes(
                    stream.read(self.page_size), page_size=self.page_size
                )

    def sync(self) -> None:
        with self._lock:
            self._ensure_open()
            self._file.flush()
            os.fsync(self._file.fileno())

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.sync()
                self._file.close()
                self._closed = True

    def __enter__(self) -> "DiskManager":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


class SingleFileDatabase(DiskManager):
    """更具描述性的兼容名称。"""
