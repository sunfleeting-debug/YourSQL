"""预写日志（Write-Ahead Log）。

设计取舍
--------
本模块实现的是**页级撤销日志 + 事务控制记录**，而不是完整的 ARIES 重做日志：

* 事务修改某页前，先把该页的**首次前像（before-image）**写进日志；
* 数据页落盘前，其对应的日志记录必须已经 fsync（"日志先于数据"规则）；
* 提交时先把脏页全部刷盘并 fsync，**再**写 ``commit`` 记录；
* 崩溃恢复只需**回滚未提交事务**（用前像覆盖回去），不需要 redo。

之所以不需要 redo，是因为提交顺序被刻意设计成"页先落盘、commit 后落盘"：
若在这两步之间崩溃，磁盘上是"有改动但无 commit"的状态，恢复时按未提交事务
回滚即可，不会丢失已提交的数据。代价是每次提交都要做一次整库刷盘，
对教学数据库完全可以接受，换来的是恢复逻辑足够简单、可手工验证。
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterator

from yoursql.common.errors import RecoveryError

WAL_MAGIC = "YOURSQLWAL"
WAL_VERSION = 1
WAL_SUFFIX = ".wal"

# 事务控制记录种类
BEGIN = "begin"
COMMIT = "commit"
ABORT = "abort"
PAGE = "page"
CHECKPOINT = "checkpoint"
# 页分配会改动 superblock（下一页号与空闲页集合），单独记录以便回滚。
ALLOCATE = "alloc"

CONTROL_KINDS = frozenset({BEGIN, COMMIT, ABORT, CHECKPOINT})


@dataclass(frozen=True)
class WalRecord:
    """一条日志记录；``page`` 类的记录携带页的完整前像。"""

    lsn: int
    txn_id: int
    kind: str
    page_id: int = -1
    image: bytes | None = None
    page_lsn: int = 0
    description: str = ""

    @property
    def is_control(self) -> bool:
        return self.kind in CONTROL_KINDS

    def to_json_line(self) -> str:
        data: dict[str, object] = {
            "lsn": self.lsn,
            "txn": self.txn_id,
            "kind": self.kind,
        }
        if self.page_id >= 0:
            data["page"] = self.page_id
        if self.page_lsn:
            data["plsn"] = self.page_lsn
        if self.image is not None:
            data["img"] = base64.b64encode(self.image).decode("ascii")
        if self.description:
            data["desc"] = self.description
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json_line(cls, line: str) -> "WalRecord":
        """解析一行日志；字段缺失或类型错误一律视为日志损坏。"""

        try:
            data = json.loads(line)
        except ValueError as exc:
            raise RecoveryError("日志行不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise RecoveryError("日志行不是对象")
        raw_image = data.get("img")
        image = (
            base64.b64decode(raw_image.encode("ascii"))
            if isinstance(raw_image, str)
            else None
        )
        return cls(
            lsn=int(data["lsn"]),
            txn_id=int(data.get("txn", 0)),
            kind=str(data["kind"]),
            page_id=int(data.get("page", -1)),
            image=image,
            page_lsn=int(data.get("plsn", 0)),
            description=str(data.get("desc", "")),
        )


class WriteAheadLog:
    """追加写、可截断的日志文件；LSN 跨进程单调递增。"""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        enabled: bool = True,
        sync_on_flush: bool = True,
    ) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.sync_on_flush = bool(sync_on_flush)
        self._lock = RLock()
        self._closed = False
        self._next_lsn = 1
        self._written_lsn = 0
        self._persisted_lsn = 0
        self._logical_records = 0
        self._page_images = 0
        self._flushes = 0
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("r+b" if self.path.exists() else "w+b")
            self._recover_sequence()
        else:
            self._handle = None

    # ----- 生命周期与元信息 -----
    @property
    def next_lsn(self) -> int:
        return self._next_lsn

    @property
    def persisted_lsn(self) -> int:
        """已经 fsync 到磁盘的最大 LSN；数据页落盘前必须不超过这个值。"""

        return self._persisted_lsn

    @property
    def written_lsn(self) -> int:
        """已经交给文件句柄（可能仍在用户态缓冲区）的最大 LSN。"""

        return self._written_lsn

    def stats(self) -> dict[str, object]:
        with self._lock:
            size = self.path.stat().st_size if self.path.exists() else 0
            return {
                "enabled": self.enabled,
                "path": str(self.path),
                "records": self._logical_records,
                "page_images": self._page_images,
                "next_lsn": self._next_lsn,
                "persisted_lsn": self._persisted_lsn,
                "flushes": self._flushes,
                "bytes": size,
            }

    def _ensure_open(self) -> None:
        if self._closed:
            raise RecoveryError("日志文件已经关闭")

    def _recover_sequence(self) -> None:
        """打开已有日志时恢复 LSN 计数，并忽略崩溃遗留的残缺尾行。"""

        if self._handle is None:
            return
        self._handle.seek(0)
        raw = self._handle.read().decode("utf-8", errors="replace")
        lines = [line for line in raw.splitlines() if line.strip()]
        if not lines:
            self._write_header()
            return
        try:
            header = json.loads(lines[0])
        except ValueError as exc:
            raise RecoveryError("日志文件头损坏") from exc
        if not isinstance(header, dict) or header.get("magic") != WAL_MAGIC:
            raise RecoveryError("日志文件魔数错误")
        highest = int(header.get("next_lsn", 1)) - 1
        for line in lines[1:]:
            try:
                record = WalRecord.from_json_line(line)
            except RecoveryError:
                # WHY：最后一行可能是崩溃时写了一半的记录，按"日志尾部截断"处理；
                # 中间出现坏行才是真的损坏，必须报错而不是静默跳过。
                if line is lines[-1]:
                    break
                raise
            highest = max(highest, record.lsn)
        self._next_lsn = highest + 1
        self._written_lsn = highest
        self._persisted_lsn = highest
        self._handle.seek(0, os.SEEK_END)

    # ----- 追加与刷盘 -----
    def append(
        self,
        txn_id: int,
        kind: str,
        *,
        page_id: int = -1,
        image: bytes | None = None,
        page_lsn: int = 0,
        description: str = "",
    ) -> WalRecord:
        """追加一条记录并返回它；未 ``flush`` 前不保证落盘。"""

        if not self.enabled or self._handle is None:
            # 日志关闭时仍然分配 LSN，保证上层拿到的记录对象结构一致。
            record = WalRecord(
                self._next_lsn, txn_id, kind, page_id, image, page_lsn, description
            )
            self._next_lsn += 1
            self._written_lsn = record.lsn
            self._logical_records += 1
            if image is not None:
                self._page_images += 1
            return record
        with self._lock:
            self._ensure_open()
            record = WalRecord(
                self._next_lsn, txn_id, kind, page_id, image, page_lsn, description
            )
            self._next_lsn += 1
            self._handle.write((record.to_json_line() + "\n").encode("utf-8"))
            self._written_lsn = record.lsn
            self._logical_records += 1
            if image is not None:
                self._page_images += 1
            return record

    def flush(self, lsn: int | None = None) -> int:
        """把日志刷到磁盘；``lsn`` 给定时表示"至少要保证这个 LSN 已落盘"。"""

        if not self.enabled or self._handle is None:
            if lsn is not None:
                self._persisted_lsn = max(self._persisted_lsn, min(lsn, self._written_lsn))
            else:
                self._persisted_lsn = self._written_lsn
            return self._persisted_lsn
        with self._lock:
            self._ensure_open()
            if self._persisted_lsn < self._written_lsn:
                self._handle.flush()
                if self.sync_on_flush:
                    os.fsync(self._handle.fileno())
                self._flushes += 1
                self._persisted_lsn = self._written_lsn
            return self._persisted_lsn

    def ensure_persisted(self, lsn: int) -> None:
        """实现"日志先于数据页"规则：页 LSN 之前的日志必须已经落盘。"""

        if lsn > self._persisted_lsn:
            self.flush(lsn)

    # ----- 重放与截断 -----
    def records(self) -> Iterator[WalRecord]:
        """按 LSN 顺序产出全部记录，供崩溃恢复使用。"""

        if not self.enabled or self._handle is None:
            return iter(())
        self.flush()

        def iterator() -> Iterator[WalRecord]:
            with self._lock:
                self._ensure_open()
                self._handle.seek(0)
                raw = self._handle.read().decode("utf-8", errors="replace")
                self._handle.seek(0, os.SEEK_END)
            lines = [line for line in raw.splitlines() if line.strip()]
            for line in lines[1:]:
                try:
                    yield WalRecord.from_json_line(line)
                except RecoveryError:
                    if line is lines[-1]:
                        return
                    raise

        return iterator()

    def checkpoint(self) -> int:
        """整库刷盘并重置日志：截断后仅保留文件头，LSN 继续递增。"""

        if not self.enabled or self._handle is None:
            return self._next_lsn - 1
        with self._lock:
            self._ensure_open()
            self.append(0, CHECKPOINT, description="checkpoint")
            self.flush()
            self._handle.seek(0)
            self._handle.truncate(0)
            self._write_header()
            self._handle.seek(0, os.SEEK_END)
            self._persisted_lsn = self._next_lsn - 1
            self._written_lsn = self._persisted_lsn
            return self._persisted_lsn

    def _write_header(self) -> None:
        if self._handle is None:
            return
        header = json.dumps(
            {"magic": WAL_MAGIC, "version": WAL_VERSION, "next_lsn": self._next_lsn},
            separators=(",", ":"),
        )
        self._handle.seek(0)
        self._handle.write((header + "\n").encode("utf-8"))
        self._handle.flush()
        if self.sync_on_flush:
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._handle is not None:
                try:
                    self.flush()
                finally:
                    self._handle.close()
            self._closed = True

    def __enter__(self) -> "WriteAheadLog":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def wal_path_for(database_path: str | os.PathLike[str]) -> Path:
    """返回数据文件对应的日志路径。"""

    return Path(str(database_path) + WAL_SUFFIX)
