"""单文件页式磁盘管理器和 superblock。"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable, Mapping
from typing import Iterator

from yoursql.common.codec import (
    PayloadCodec,
    PayloadCodecError,
    PayloadCodecName,
    decode_payload,
)
from yoursql.common.codec import payload_codec as get_payload_codec
from yoursql.common.errors import StorageError
from yoursql.common.trace import current_trace
from yoursql.storage.page import (
    Page,
    PageType,
    decode_free_page_next,
    encode_free_page_payload,
)

_DIRECTORY_MAGIC = b"MDIR1"
_DIRECTORY_HEADER = struct.Struct("<5sQ")


@dataclass(frozen=True)
class DiskMetadata:
    """【前端特供】磁盘文件布局的只读摘要。"""

    page_size: int
    page_count: int
    next_page_id: int
    free_pages: tuple[int, ...]
    named_pages: Mapping[str, int]
    catalog_page_id: int | None
    directory_root_page: int | None
    directory_page_count: int
    free_list_head: int | None
    free_page_count: int
    free_list_format: str

    def __getitem__(self, key: str) -> object:
        # === 兼容旧检查适配层的映射式读取 ===
        # 新代码直接访问 dataclass 字段；保留下标形式，避免旧工作台接口立即失效。
        """兼容存储检查适配层的旧映射式读取。"""

        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        # === 兼容旧检查适配层的映射式遍历 ===
        """返回对象的迭代器。"""
        return iter(
            (
                "page_size",
                "page_count",
                "next_page_id",
                "free_pages",
                "named_pages",
                "catalog_page_id",
                "directory_root_page",
                "directory_page_count",
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
            "catalog_page_id": self.catalog_page_id,
            "directory_root_page": self.directory_root_page,
            "directory_page_count": self.directory_page_count,
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
        # === 兼容旧检查适配层的映射式读取 ===
        """按键或下标读取对象中的元素。"""
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        # === 兼容旧检查适配层的映射式遍历 ===
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


def decode_directory_page_payload(
    payload: bytes, payload_codec: PayloadCodec
) -> tuple[int | None, dict[str, int]]:
    """解码单个命名页目录页，返回后继页号与逻辑命名项。"""

    if len(payload) < _DIRECTORY_HEADER.size:
        raise StorageError("命名页目录页头不完整")
    magic, raw_next = _DIRECTORY_HEADER.unpack(payload[: _DIRECTORY_HEADER.size])
    if magic != _DIRECTORY_MAGIC:
        raise StorageError("命名页目录魔数错误")
    try:
        # === 兼容旧目录 payload 编码 ===
        # 目录页和 superblock 共用 payload 回退策略，读取旧编码后由当前 codec 继续管理。
        data, _codec = decode_payload(
            payload[_DIRECTORY_HEADER.size :], payload_codec
        )
    except (PayloadCodecError, TypeError, ValueError) as exc:
        raise StorageError("命名页目录 payload 损坏") from exc
    if not isinstance(data, Mapping):
        raise StorageError("命名页目录 payload 不是对象")
    entries: dict[str, int] = {}
    for raw_name, raw_page_id in data.items():
        name = str(raw_name).strip().lower()
        if not name or name == "catalog" or name in entries:
            raise StorageError("命名页目录包含重复或保留名称")
        try:
            page_id = int(raw_page_id)
        except (TypeError, ValueError) as exc:
            raise StorageError("命名页目录包含无效页号") from exc
        if page_id <= 0:
            raise StorageError("命名页目录包含空页号")
        entries[name] = page_id
    return (None if raw_next == 0 else int(raw_next), entries)


class DiskManager:
    """以固定页大小读写一个数据库文件。"""

    FORMAT_VERSION = 3

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
        # === 兼容旧版 superblock 的 free_pages 数组 ===
        # HOW：旧版 superblock 仍可能携带 free_pages 数组；首次发生元数据写入时
        # 再转换为链式布局，避免仅打开旧库就改写文件。
        self._legacy_free_pages: set[int] = set()
        self._next_page_id = 1
        self._catalog_page_id: int | None = None
        self._directory_root_page: int | None = None
        self._directory_page_ids: list[int] = []
        self._pending_directory_reclaim: list[int] = []
        self._named_pages: dict[str, int] = {}
        self._directory_dirty = False
        # HOW：页分配/释放只在内存中更新链表，事务或关闭时一次性刷写 superblock，
        # 避免每个页都反复改写第 0 页并放大损坏窗口。
        self._superblock_dirty = False
        # HOW：页分配既会改 superblock 又绕过缓冲池，事务无法通过缓存观测到它；
        # 这里留一个回调，让运行时把"本事务新分配的页号"记下来，回滚时精确回收。
        self.allocate_hook: Callable[[int], None] | None = None
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
            named_pages = dict(self._named_pages)
            if self._catalog_page_id is not None:
                named_pages["catalog"] = self._catalog_page_id
            return DiskMetadata(
                page_size=self.page_size,
                page_count=self.page_count,
                next_page_id=self._next_page_id,
                free_pages=tuple(sorted(self._free_pages)),
                named_pages=named_pages,
                catalog_page_id=self._catalog_page_id,
                directory_root_page=self._directory_root_page,
                directory_page_count=len(self._directory_page_ids),
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
            # === 兼容旧 payload 编码 ===
            # 旧库可能使用另一种 PayloadCodec；decode_payload 会先尝试配置值，
            # 失败后回退到另一种编码，并把实际成功的 codec 保留下来供后续读取。
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
        # === 兼容旧 superblock 版本 ===
        # 旧版本允许继续读取；只有比当前版本更新的文件才拒绝，具体旧字段由下面的
        # free-list 和命名页兼容分支处理。
        if int(data.get("version", 0)) > self.FORMAT_VERSION:
            raise StorageError("数据库文件版本过高")
        # === 兼容旧 superblock 缺少 page_size 字段 ===
        # 旧格式缺字段时沿用打开参数，但已有字段仍必须和文件配置一致。
        stored_size = int(data.get("page_size", self.page_size))
        if stored_size != self.page_size:
            raise StorageError(
                f"页大小不匹配，文件为 {stored_size}，配置为 {self.page_size}"
            )
        physical_size = self.path.stat().st_size
        if physical_size % self.page_size:
            raise StorageError("数据库文件大小不是完整页的整数倍")
        physical_page_count = physical_size // self.page_size
        if physical_page_count < 1:
            raise StorageError("数据库文件缺少 superblock")
        # === 兼容旧 superblock 缺少 payload_codec 字段 ===
        # 旧格式没有记录编码名称，此时沿用实际解码成功的 codec；新格式则严格校验。
        stored_codec = data.get("payload_codec", self.payload_codec.name)
        if stored_codec != self.payload_codec.name:
            raise StorageError("superblock payload 编码字段与实际编码不一致")
        # WHY：崩溃可能发生在新页写出、superblock 尚未同步之后；把物理文件边界
        # 纳入逻辑页数，才能让 WAL 恢复阶段释放这类孤儿页，而不是把页号判成越界。
        # === 兼容旧 superblock 缺少 next_page_id 字段 ===
        # 缺失时从 1 起步；同时纳入物理文件页数，避免旧元数据落后于实际文件边界。
        self._next_page_id = max(
            1, int(data.get("next_page_id", 1)), physical_page_count
        )
        if "free_list_head" in data:
            raw_head = data.get("free_list_head")
            self._free_list_head = None if raw_head in {None, 0} else int(raw_head)
            declared_count = int(data.get("free_page_count", 0))
            if declared_count < 0:
                raise StorageError("superblock 空闲页数量不能为负数")
            self._load_free_list(declared_count)
        else:
            # === 兼容旧 superblock 的 free_pages 数组 ===
            # 当前格式把 free-list 链接写入 FREE 页；旧格式只在 superblock 中保存数组，
            # 先保留到内存，首次真正写元数据时再由 _ensure_linked_free_list 迁移。
            raw_free_pages = data.get("free_pages", [])
            if not isinstance(raw_free_pages, list):
                raise StorageError("superblock free_pages 不是数组")
            self._free_pages = {int(value) for value in raw_free_pages}
            if any(
                page_id <= 0 or page_id >= self._next_page_id
                for page_id in self._free_pages
            ):
                raise StorageError("superblock free_pages 包含越界页号")
            self._legacy_free_pages = set(self._free_pages)
        stored_version = int(data.get("version", 0))
        if "catalog_page_id" in data:
            self._catalog_page_id = self._optional_metadata_page_id(
                data.get("catalog_page_id")
            )
            self._directory_root_page = self._optional_metadata_page_id(
                data.get("directory_root_page")
            )
            self._load_directory()
            self._superblock_dirty = False
        else:
            # === 兼容旧 superblock 内嵌的 named_pages ===
            # 旧格式把 catalog 和其它命名页都放在第 0 页；当前格式把 catalog 保留为
            # 固定指针，其它命名页迁移到可扩展的 DIRECTORY 页链。
            # HOW：v2 仍把命名页表放在 superblock；打开旧库时拆出 catalog 固定根，
            # 其它条目留在内存，首次同步时迁移到可扩展的目录页链。
            raw_named = data.get("named_pages", {})
            legacy_named = (
                {str(key).strip().lower(): int(value) for key, value in raw_named.items()}
                if isinstance(raw_named, Mapping)
                else {}
            )
            self._catalog_page_id = self._optional_metadata_page_id(
                legacy_named.pop("catalog", None)
            )
            self._named_pages = legacy_named
            self._directory_root_page = None
            self._directory_page_ids = []
            self._directory_dirty = bool(self._named_pages)
            # === 兼容旧版本的延迟迁移 ===
            # 即使没有其它命名页，也要在下一次同步时移除旧版 map，完成格式升级。
            self._superblock_dirty = stored_version < self.FORMAT_VERSION

    def _superblock_payload(self) -> bytes:
        """构造可持久化的 superblock 载荷。"""
        data = {
            "magic": "YOURSQLMS",
            "version": self.FORMAT_VERSION,
            "page_size": self.page_size,
            "next_page_id": self._next_page_id,
            "free_list_head": self._free_list_head,
            "free_page_count": len(self._free_pages),
            "catalog_page_id": self._catalog_page_id,
            "directory_root_page": self._directory_root_page,
            "payload_codec": self.payload_codec.name,
        }
        if self._legacy_free_pages:
            # === 兼容旧 free_pages 格式的只读观察 ===
            # 只读 peek 需要准确反映尚未迁移的旧页；真正写回前会先完成迁移。
            data["version"] = 1
            data.pop("free_list_head")
            data.pop("free_page_count")
            data["free_pages"] = sorted(self._legacy_free_pages)
        try:
            payload = self.payload_codec.encode(data)
        except (PayloadCodecError, TypeError, ValueError) as exc:
            raise StorageError("superblock payload 无法编码") from exc
        max_payload = self.page_size - Page.HEADER_SIZE
        if len(payload) > max_payload:
            raise StorageError(
                f"superblock 元数据过大：{len(payload)} > {max_payload} 字节"
            )
        return payload

    def _optional_metadata_page_id(self, value: object) -> int | None:
        """校验 superblock 或目录中的可选页指针。"""

        if value is None or value == 0:
            return None
        try:
            page_id = int(value)
        except (TypeError, ValueError) as exc:
            raise StorageError("元数据页号不是整数") from exc
        if page_id <= 0 or page_id >= self._next_page_id:
            raise StorageError(f"元数据页号 {page_id} 越界")
        return page_id

    def _directory_chunks(self, entries: Mapping[str, int]) -> list[bytes]:
        """把命名页目录拆成不会超过单页容量的 JSON 分片。"""

        capacity = self.page_size - Page.HEADER_SIZE
        if capacity <= _DIRECTORY_HEADER.size:
            raise StorageError("页大小不足以容纳命名页目录")
        if not entries:
            return []
        chunks: list[bytes] = []
        current: dict[str, int] = {}
        for name, page_id in sorted(entries.items()):
            candidate = dict(current)
            candidate[name] = int(page_id)
            try:
                encoded = self.payload_codec.encode(candidate)
            except (PayloadCodecError, TypeError, ValueError) as exc:
                raise StorageError("命名页目录无法编码") from exc
            if len(encoded) + _DIRECTORY_HEADER.size > capacity:
                if not current:
                    raise StorageError(f"命名页名称过长，无法写入目录：{name!r}")
                chunks.append(self.payload_codec.encode(current))
                current = {name: int(page_id)}
                try:
                    encoded = self.payload_codec.encode(current)
                except (PayloadCodecError, TypeError, ValueError) as exc:
                    raise StorageError("命名页目录无法编码") from exc
                if len(encoded) + _DIRECTORY_HEADER.size > capacity:
                    raise StorageError(f"命名页名称过长，无法写入目录：{name!r}")
            else:
                current = candidate
        if current or not chunks:
            try:
                chunks.append(self.payload_codec.encode(current))
            except (PayloadCodecError, TypeError, ValueError) as exc:
                raise StorageError("命名页目录无法编码") from exc
        return chunks

    def _load_directory(self) -> None:
        """读取 superblock 指向的可扩展命名页目录。"""

        self._named_pages = {}
        self._directory_page_ids = []
        current = self._directory_root_page
        visited: set[int] = set()
        while current is not None:
            if current in visited:
                raise StorageError("命名页目录存在循环")
            visited.add(current)
            page = Page.from_bytes(self._read_raw(current), page_size=self.page_size)
            if page.page_type is not PageType.DIRECTORY:
                raise StorageError(f"命名页目录 {current} 类型错误")
            if len(page.payload) < _DIRECTORY_HEADER.size:
                raise StorageError("命名页目录页头不完整")
            magic, raw_next = _DIRECTORY_HEADER.unpack(
                page.payload[: _DIRECTORY_HEADER.size]
            )
            if magic != _DIRECTORY_MAGIC:
                raise StorageError("命名页目录魔数错误")
            try:
                # === 兼容旧目录 payload 编码 ===
                # 目录页可能与打开参数使用不同编码，decode_payload 负责尝试兼容格式。
                data, _codec = decode_payload(
                    page.payload[_DIRECTORY_HEADER.size :], self.payload_codec
                )
            except (PayloadCodecError, TypeError, ValueError) as exc:
                raise StorageError("命名页目录 payload 损坏") from exc
            if not isinstance(data, Mapping):
                raise StorageError("命名页目录 payload 不是对象")
            for raw_name, raw_page_id in data.items():
                name = str(raw_name).strip().lower()
                if not name or name == "catalog" or name in self._named_pages:
                    raise StorageError("命名页目录包含重复或保留名称")
                page_id = self._optional_metadata_page_id(raw_page_id)
                if page_id is None:
                    raise StorageError("命名页目录包含空页号")
                self._named_pages[name] = page_id
            self._directory_page_ids.append(current)
            current = self._optional_metadata_page_id(raw_next)

    def _flush_directory(self) -> None:
        """把命名页目录写入可扩展的目录页链。"""

        chunks = self._directory_chunks(self._named_pages)
        previous_page_ids = list(self._directory_page_ids)
        # WHY：目录页采用 copy-on-write，superblock 切换根指针前不覆盖旧链，
        # 避免目录页写到一半时崩溃而损坏仍被旧 superblock 引用的链。
        page_ids: list[int] = []
        while len(page_ids) < len(chunks):
            page_ids.append(self.allocate(PageType.DIRECTORY).page_id)
        for index, chunk in enumerate(chunks):
            next_page_id = page_ids[index + 1] if index + 1 < len(page_ids) else 0
            payload = _DIRECTORY_HEADER.pack(_DIRECTORY_MAGIC, next_page_id) + chunk
            self._write_raw(
                Page(page_ids[index], self.page_size, PageType.DIRECTORY, payload)
            )
        self._directory_page_ids = page_ids
        self._directory_root_page = page_ids[0] if page_ids else None
        self._pending_directory_reclaim.extend(previous_page_ids)
        self._directory_dirty = False

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
                raise StorageError(
                    f"free-list 页头页号不匹配：文件偏移={current}，页头={page.page_id}"
                )
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
        # === 兼容旧 free_pages 数组到链式 free-list 的迁移 ===
        # 迁移只在需要写元数据时触发，单纯打开或 peek 旧库不会产生写入。
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
        if self._directory_dirty:
            self._flush_directory()
        self._write_raw(
            Page(0, self.page_size, PageType.SUPERBLOCK, self._superblock_payload())
        )
        self._superblock_dirty = False

    def register_named_page(self, name: str, page_id: int) -> None:
        """注册带名称的页并记录其页号。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            key = name.strip().lower()
            if not key:
                raise StorageError("命名页名称不能为空")
            if key == "catalog":
                self._catalog_page_id = int(page_id)
            else:
                previous = self._named_pages.get(key)
                self._named_pages[key] = int(page_id)
                try:
                    self._directory_chunks(self._named_pages)
                except StorageError:
                    if previous is None:
                        self._named_pages.pop(key, None)
                    else:
                        self._named_pages[key] = previous
                    raise
                self._directory_dirty = True
            self._superblock_dirty = True

    def named_page(self, name: str) -> int | None:
        """按名称查找已注册的页。"""
        with self._lock:
            key = name.strip().lower()
            return self._catalog_page_id if key == "catalog" else self._named_pages.get(key)

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
            reused_free_page = self._free_list_head is not None
            previous_head = self._free_list_head
            previous_next = self._free_page_next.get(page_id)
            if reused_free_page:
                self._free_list_head = next_page_id
                self._free_page_next.pop(page_id, None)
                self._free_pages.remove(page_id)
            else:
                self._next_page_id += 1
            try:
                self._superblock_payload()
            except StorageError:
                if reused_free_page:
                    self._free_list_head = previous_head
                    self._free_pages.add(page_id)
                    if previous_next is not None:
                        self._free_page_next[page_id] = previous_next
                else:
                    self._next_page_id -= 1
                raise
            self._write_raw(page)
            # WHY：先写出实际页，再在事务/关闭同步时发布元数据；崩溃时物理文件边界
            # 仍会被下一次打开识别，WAL 可以回收未提交分配的孤儿页。
            self._superblock_dirty = True
            if self.allocate_hook is not None:
                self.allocate_hook(page_id)
            return page

    def free(self, page_id: int) -> None:
        """释放数据库页并回收其页号。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            if page_id == 0:
                raise StorageError("不能释放 superblock")
            if page_id in self._directory_page_ids:
                raise StorageError("不能直接释放命名页目录")
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
            if normalized == self._catalog_page_id:
                self._catalog_page_id = None
            if normalized in self._named_pages.values():
                self._named_pages = {
                    key: value
                    for key, value in self._named_pages.items()
                    if value != normalized
                }
                self._directory_dirty = True
            self._superblock_dirty = True

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
                if page_id in self._directory_page_ids:
                    raise StorageError("不能直接释放命名页目录")
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
            if self._catalog_page_id in normalized_ids:
                self._catalog_page_id = None
            retained = {
                key: value
                for key, value in self._named_pages.items()
                if value not in normalized_ids
            }
            if len(retained) != len(self._named_pages):
                self._named_pages = retained
                self._directory_dirty = True
            self._superblock_dirty = True

    def _check_page_id(self, page_id: int) -> None:
        """校验页号是否位于有效范围内。"""
        if page_id < 0 or page_id >= self.page_count:
            raise StorageError(f"页号 {page_id} 越界")

    def read(self, page_id: int) -> Page:
        """读取指定页并恢复为页对象。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(int(page_id))
            if int(page_id) == 0:
                return Page(
                    0, self.page_size, PageType.SUPERBLOCK, self._superblock_payload()
                )
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

    def reset_io_stats(self) -> None:
        """【前端特供】归零进程内页 I/O 计数，不改动数据库文件。"""

        with self._lock:
            self._reads = 0
            self._writes = 0

    def peek(self, page_id: int) -> Page:
        """只读调试页，恢复文件游标且不改变正常读写计数。"""
        with self._lock:
            self._ensure_open()
            self._check_page_id(page_id)
            if page_id == 0:
                # === 兼容旧 free_pages 格式的只读 peek ===
                # peek 不应改写磁盘；旧版数组会在这里原样展示，首次写入时才迁移。
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
            if self._superblock_dirty or self._directory_dirty:
                self._write_superblock()
            self._file.flush()
            os.fsync(self._file.fileno())
            if self._pending_directory_reclaim:
                # WHY：新根已经完成一次 fsync 后，旧目录链才允许回收；若此处崩溃，
                # 最坏只是遗留可重用的孤儿页，不会破坏新旧任一份有效目录。
                reclaim = tuple(self._pending_directory_reclaim)
                self._pending_directory_reclaim.clear()
                self.free_many(reclaim)
                self._write_superblock()
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
    # === 兼容旧类名 ===
    # 保留旧名称，不新增行为；新代码统一使用 DiskManager 表达职责。
    """更具描述性的兼容名称。"""
