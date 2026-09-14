"""单文件页式磁盘管理器和 superblock。"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterator
from typing import Mapping

from yoursql.common.codec import PayloadCodec, PayloadCodecName, decode_payload
from yoursql.common.codec import payload_codec as get_payload_codec
from yoursql.common.errors import StorageError
from yoursql.common.trace import current_trace
from yoursql.storage.page import (
    Page,
    PageType,
    decode_free_page_next,
    encode_free_page_payload,
)


@dataclass(frozen=True)
class DiskMetadata:
    """【前端特供】磁盘文件布局的只读摘要。"""

    page_size: int
    page_count: int
    next_page_id: int
    free_pages: tuple[int, ...]
    named_pages: Mapping[str, int]
    free_list_head: int | None
    free_page_count: int
    free_list_format: str

    def __getitem__(self, key: str) -> object:
        """兼容存储检查适配层的旧映射式读取。"""

        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        """返回对象的迭代器。"""
        return iter(
            (
                "page_size",
                "page_count",
                "next_page_id",
                "free_pages",
                "named_pages",
                "free_list_head",
                "free_page_count",
                "free_list_format",
            )
        )

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {
            "page_size": self.page_size,
            "page_count": self.page_count,
            "next_page_id": self.next_page_id,
            "free_pages": list(self.free_pages),
            "named_pages": dict(self.named_pages),
            "free_list_head": self.free_list_head,
            "free_page_count": self.free_page_count,
            "free_list_format": self.free_list_format,
        }


@dataclass(frozen=True)
class DiskIOStats:
    """【前端特供】磁盘管理器进程内累计页 I/O 统计。"""

    page_reads: int
    page_writes: int
    bytes_read: int
    bytes_written: int

    def __getitem__(self, key: str) -> int:
        """按键或下标读取对象中的元素。"""
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        """返回对象的迭代器。"""
        return iter(("page_reads", "page_writes", "bytes_read", "bytes_written"))

    def to_dict(self) -> dict[str, int]:
        """将对象转换为可序列化的字典。"""
        return {
            "page_reads": self.page_reads,
            "page_writes": self.page_writes,
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
        }


class DiskManager:
    """以固定页大小读写一个数据库文件。"""

    FORMAT_VERSION = 2

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        page_size: int = 4096,
        payload_codec: PayloadCodecName = "json",
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.path = Path(path)
        self.page_size = page_size
        self.payload_codec: PayloadCodec = get_payload_codec(payload_codec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._closed = False
        self._reads = 0
        self._writes = 0
        exists = self.path.exists() and self.path.stat().st_size > 0
        self._file = self.path.open("r+b" if exists else "w+b")
        self._free_pages: set[int] = set()
        self._free_page_next: dict[int, int | None] = {}
        self._free_list_head: int | None = None
        # HOW：旧版 superblock 仍可能携带 free_pages 数组；首次发生元数据写入时
        # 再转换为链式布局，避免仅打开旧库就改写文件。
        self._legacy_free_pages: set[int] = set()
        self._next_page_id = 1
        self._named_pages: dict[str, int] = {}
        if exists:
            self._load_superblock()
        else:
            # WHY：第 0 页是固定的启动入口，先写满一个空页建立文件边界，再写入完整
            # superblock，确保后续按固定页大小读取时不会把文件头当成普通数据。
            self._write_raw(Page.empty(0, page_size, PageType.SUPERBLOCK))
            self._write_superblock()
            self.sync()

    @property
    def page_count(self) -> int:
        """返回数据库文件中的页数量。"""
        with self._lock:
            # WHY：BufferedRandom.seek 会隐式刷出待写字节，元数据查看不能触发写盘。
            # 页文件只扩展不收缩，next_page_id 即当前逻辑文件页数。
            return self._next_page_id

    def metadata(self) -> DiskMetadata:
        """【前端特供】返回当前数据库文件布局的只读摘要。"""
        with self._lock:
            return DiskMetadata(
                page_size=self.page_size,
                page_count=self.page_count,
                next_page_id=self._next_page_id,
                free_pages=tuple(sorted(self._free_pages)),
                named_pages=dict(self._named_pages),
                free_list_head=self._free_list_head,
                free_page_count=len(self._free_pages),
                free_list_format=(
                    "legacy_array" if self._legacy_free_pages else "linked_page_v1"
                ),
            )

    def _ensure_open(self) -> None:
        """检查磁盘管理器仍处于打开状态。"""
        if self._closed:
            raise StorageError("数据库文件已经关闭")

    def _load_superblock(self) -> None:
        """读取并校验数据库文件的 superblock。"""
        # WHY：superblock 的页号固定为 0，打开数据库时无需依赖其他页的目录信息即可启动。
        raw = self._read_raw(0)
        page = Page.from_bytes(raw, page_size=self.page_size)
        if page.page_type is not PageType.SUPERBLOCK:
            raise StorageError("第 0 页不是 superblock")
        try:
            data, selected_codec = (
                decode_payload(page.payload, self.payload_codec)
                if page.payload
                else ({}, self.payload_codec)
            )
            self.payload_codec = selected_codec
        except (TypeError, ValueError) as exc:
            raise StorageError("superblock payload 损坏") from exc
        if not isinstance(data, Mapping):
            raise StorageError("superblock payload 不是对象")
        if data.get("magic") != "YOURSQLMS":
            raise StorageError("数据库文件魔数错误")
        if int(data.get("version", 0)) > self.FORMAT_VERSION:
            raise StorageError("数据库文件版本过高")
        stored_size = int(data.get("page_size", self.page_size))
        if stored_size != self.page_size:
            raise StorageError(
                f"页大小不匹配，文件为 {stored_size}，配置为 {self.page_size}"
            )
        stored_codec = data.get("payload_codec", self.payload_codec.name)
        if stored_codec != self.payload_codec.name:
            raise StorageError("superblock payload 编码字段与实际编码不一致")
        self._next_page_id = max(1, int(data.get("next_page_id", 1)))
        if "free_list_head" in data:
            raw_head = data.get("free_list_head")
            self._free_list_head = (
                None if raw_head in {None, 0} else int(raw_head)
            )
            declared_count = int(data.get("free_page_count", 0))
            if declared_count < 0:
                raise StorageError("superblock 空闲页数量不能为负数")
            self._load_free_list(declared_count)
        else:
            raw_free_pages = data.get("free_pages", [])
            if not isinstance(raw_free_pages, list):
                raise StorageError("superblock free_pages 不是数组")
            self._free_pages = {int(value) for value in raw_free_pages}
            if any(page_id <= 0 or page_id >= self._next_page_id for page_id in self._free_pages):
                raise StorageError("superblock free_pages 包含越界页号")
            self._legacy_free_pages = set(self._free_pages)
        raw_named = data.get("named_pages", {})
        self._named_pages = (
            {str(key): int(value) for key, value in raw_named.items()}
            if isinstance(raw_named, Mapping)
            else {}
        )

    def _superblock_payload(self) -> bytes:
        """构造可持久化的 superblock 载荷。"""
        data = {
            "magic": "YOURSQLMS",
            "version": self.FORMAT_VERSION,
            "page_size": self.page_size,
            "next_page_id": self._next_page_id,
            "free_list_head": self._free_list_head,
            "free_page_count": len(self._free_pages),
            "named_pages": self._named_pages,
            "payload_codec": self.payload_codec.name,
        }
        if self._legacy_free_pages:
            # 只读 peek 需要准确反映尚未迁移的旧页；真正写回前会先完成迁移。
            data["version"] = 1
            data.pop("free_list_head")
            data.pop("free_page_count")
            data["free_pages"] = sorted(self._legacy_free_pages)
        return self.payload_codec.encode(data)

    def _load_free_list(self, declared_count: int) -> None:
        """读取并校验链式 free-list。"""

        current = self._free_list_head
        visited: set[int] = set()
        while current is not None:
            if current <= 0 or current >= self._next_page_id:
                raise StorageError(f"free-list 页号 {current} 越界")
            if current in visited:
                raise StorageError("free-list 存在循环")
            if declared_count and len(visited) >= declared_count:
                raise StorageError("free-list 实际长度超过 superblock 记录")
            visited.add(current)
            page = Page.from_bytes(self._read_raw(current), page_size=self.page_size)
            if page.page_type is not PageType.FREE:
                raise StorageError(f"free-list 页 {current} 不是 FREE 页")
            if page.page_id != current:
                raise StorageError(f"free-list 页头页号不匹配：文件偏移={current}，页头={page.page_id}")
            next_page_id = decode_free_page_next(page.payload)
            self._free_page_next[current] = next_page_id
            current = next_page_id

        if declared_count != len(visited):
            raise StorageError(
                f"free-list 长度不一致，superblock={declared_count}，实际={len(visited)}"
            )
        self._free_pages = visited

    def _ensure_linked_free_list(self) -> None:
        """把旧版 free_pages 数组一次转换为页内后继指针。"""

        if not self._legacy_free_pages:
            return
        ordered = sorted(self._legacy_free_pages)
        for index, page_id in enumerate(ordered):
            next_page_id = ordered[index + 1] if index + 1 < len(ordered) else None
            self._write_raw(
                Page(
                    page_id,
                    self.page_size,
                    PageType.FREE,
                    encode_free_page_payload(next_page_id),
                )
            )
            self._free_page_next[page_id] = next_page_id
        self._free_list_head = ordered[0]
        self._legacy_free_pages.clear()

    def _read_raw(self, page_id: int) -> bytes:
        """从数据库文件读取原始页字节。"""
        self._reads += 1
        trace = current_trace.get()
        if trace is not None:
            trace.event("disk_read", page_id)
        # WHY：固定页大小使页号可以直接换算为文件偏移，避免扫描前置页并保持随机访问边界稳定。
        self._file.seek(page_id * self.page_size)
        raw = self._file.read(self.page_size)
        if len(raw) != self.page_size:
            raise StorageError(f"页 {page_id} 不存在或文件被截断")
        return raw

    def _write_raw(self, page: Page) -> None:
        """向数据库文件写入原始页字节。"""
        self._writes += 1
        trace = current_trace.get()
        if trace is not None:
            trace.event("disk_write", page.page_id)
        # WHY：始终写入 Page.to_bytes() 生成的完整固定长度页，避免短写导致后续页边界错位。
        self._file.seek(page.page_id * self.page_size)
        self._file.write(page.to_bytes())

    def _write_superblock(self) -> None:
        """将当前 superblock 写回数据库文件。"""
        self._ensure_linked_free_list()
        self._write_raw(
            Page(0, self.page_size, PageType.SUPERBLOCK, self._superblock_payload())
        )

    def register_named_page(self, name: str, page_id: int) -> None:
        """注册带名称的页并记录其页号。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            self._named_pages[name.strip().lower()] = int(page_id)
            self._write_superblock()

    def named_page(self, name: str) -> int | None:
        """按名称查找已注册的页。"""
        with self._lock:
            return self._named_pages.get(name.strip().lower())

    def allocate(
        self, page_type: PageType = PageType.FREE, payload: bytes = b""
    ) -> Page:
        """分配新的数据库页。"""
        with self._lock:
            self._ensure_open()
            self._ensure_linked_free_list()
            if self._free_list_head is not None:
                page_id = self._free_list_head
                next_page_id = self._free_page_next.get(page_id)
                if page_id not in self._free_pages:
                    raise StorageError(f"free-list 页 {page_id} 未登记")
            else:
                # WHY：next_page_id 单调推进，保证新页不会覆盖已有页；文件只扩展、不收缩。
                page_id = self._next_page_id
            page = Page(page_id, self.page_size, page_type, payload)
            if self._free_list_head is not None:
                self._free_list_head = next_page_id
                self._free_page_next.pop(page_id, None)
                self._free_pages.remove(page_id)
            else:
                self._next_page_id += 1
            self._write_raw(page)
            # WHY：先写出实际页，再发布分配元数据，避免 superblock 先指向尚未物化的页；
            # 两次写入尚非原子操作，崩溃恢复机制列入 TODO。
            self._write_superblock()
            return page

    def free(self, page_id: int) -> None:
        """释放数据库页并回收其页号。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            if page_id == 0:
                raise StorageError("不能释放 superblock")
            self._ensure_linked_free_list()
            normalized = int(page_id)
            if normalized in self._free_pages:
                raise StorageError(f"页 {page_id} 已经是 FREE 页")
            # WHY：FREE 页本身携带链表后继，superblock 只需保存链头，避免释放大索引
            # 时让第 0 页的 JSON 随 free-page 数量膨胀。
            self._write_raw(
                Page(
                    normalized,
                    self.page_size,
                    PageType.FREE,
                    encode_free_page_payload(self._free_list_head),
                )
            )
            self._free_page_next[normalized] = self._free_list_head
            self._free_pages.add(normalized)
            self._free_list_head = normalized
            for key, value in tuple(self._named_pages.items()):
                if value == page_id:
                    # WHY：释放页后清除命名指针，避免 named_pages 指向已回收页。
                    del self._named_pages[key]
            self._write_superblock()

    def free_many(self, page_ids: Iterable[int]) -> None:
        """批量释放页面，并只重写一次 free-list 元数据。"""

        with self._lock:
            self._ensure_open()
            normalized_ids = tuple(dict.fromkeys(int(page_id) for page_id in page_ids))
            if not normalized_ids:
                return
            self._ensure_linked_free_list()
            for page_id in normalized_ids:
                self._check_page_id(page_id)
                if page_id == 0:
                    raise StorageError("不能释放 superblock")
                if page_id in self._free_pages:
                    raise StorageError(f"页 {page_id} 已经是 FREE 页")

            next_page_id = self._free_list_head
            for page_id in normalized_ids:
                self._write_raw(
                    Page(
                        page_id,
                        self.page_size,
                        PageType.FREE,
                        encode_free_page_payload(next_page_id),
                    )
                )
                self._free_page_next[page_id] = next_page_id
                self._free_pages.add(page_id)
                next_page_id = page_id
            self._free_list_head = next_page_id
            for key, value in tuple(self._named_pages.items()):
                if value in normalized_ids:
                    del self._named_pages[key]
            self._write_superblock()

    def _check_page_id(self, page_id: int) -> None:
        """校验页号是否位于有效范围内。"""
        if page_id < 0 or page_id >= self.page_count:
            raise StorageError(f"页号 {page_id} 越界")

    def read(self, page_id: int) -> Page:
        """读取指定页并恢复为页对象。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(int(page_id))
            return Page.from_bytes(
                self._read_raw(int(page_id)), page_size=self.page_size
            )

    def write(self, page: Page) -> None:
        """将页对象写回数据库文件。"""
        with self._lock:
            self._ensure_open()
            # WHY：页大小不一致会让当前文件的固定偏移模型失效，必须在写入前拒绝。
            if page.page_size != self.page_size:
                raise StorageError("页大小与文件不一致")
            self._check_page_id(page.page_id)
            self._write_raw(page)

    def io_stats(self) -> DiskIOStats:
        """【前端特供】返回进程内页 I/O 计数；调试读取单独排除。"""
        with self._lock:
            return DiskIOStats(
                page_reads=self._reads,
                page_writes=self._writes,
                bytes_read=self._reads * self.page_size,
                bytes_written=self._writes * self.page_size,
            )

    def peek(self, page_id: int) -> Page:
        """只读调试页，恢复文件游标且不改变正常读写计数。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            if page_id == 0:
                # WHY：peek 不应改写磁盘；旧版数组会在这里原样展示，首次写入时才迁移。
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
        """将文件缓冲区同步到持久化介质。"""
        with self._lock:
            self._ensure_open()
            # WHY：write/flush 只把内容推进到文件或 OS 缓冲区，只有 flush + fsync 才完成
            # 当前持久化边界；调用频率由上层事务/语句边界统一控制以减少同步开销。
            self._file.flush()
            os.fsync(self._file.fileno())

    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        with self._lock:
            if not self._closed:
                # WHY：关闭前必须完成最后一次同步，避免仍在缓存中的脏数据随文件句柄关闭而丢失。
                self.sync()
                self._file.close()
                self._closed = True

    def __enter__(self) -> "DiskManager":
        """进入上下文管理器并返回当前对象。"""
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """退出上下文管理器并完成资源清理。"""
        self.close()


class SingleFileDatabase(DiskManager):
    """更具描述性的兼容名称。"""
