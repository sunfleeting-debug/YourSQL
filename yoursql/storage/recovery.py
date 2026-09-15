"""崩溃恢复：用预写日志把未提交事务的改动撤销掉。

恢复策略
--------
日志采用"页先落盘、commit 后落盘"的提交顺序（见 ``wal`` 模块说明），
因此:

* 有 ``commit`` 记录的事务 → 其数据页在崩溃前已经全部落盘，**无需 redo**；
* 没有 ``commit`` 记录的事务 → 其改动可能已随缓冲池淘汰写入磁盘，
  必须用**首次前像**逐页撤销，并回收它新分配的页。

恢复完成后写一条 ``abort`` 记录并做一次 checkpoint 截断日志，
数据库即可回到"所有已落盘内容都属于已提交事务"的一致状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yoursql.storage.buffer import BufferPool
from yoursql.storage.disk import DiskManager
from yoursql.storage.wal import ABORT, ALLOCATE, COMMIT, PAGE, WriteAheadLog


@dataclass
class RecoveryReport:
    """一次崩溃恢复的结果，供日志、测试与工作台展示。"""

    records: int = 0
    committed: tuple[int, ...] = ()
    rolled_back: tuple[int, ...] = ()
    pages_restored: int = 0
    pages_reclaimed: int = 0
    truncated: bool = False
    recovered: bool = False
    details: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "records": self.records,
            "committed": list(self.committed),
            "rolled_back": list(self.rolled_back),
            "pages_restored": self.pages_restored,
            "pages_reclaimed": self.pages_reclaimed,
            "truncated": self.truncated,
            "recovered": self.recovered,
        }


def recover(
    wal: WriteAheadLog, buffer_pool: BufferPool, disk: DiskManager
) -> RecoveryReport:
    """重放日志并对未提交事务做撤销；返回恢复报告。"""

    report = RecoveryReport()
    if not wal.enabled:
        return report

    records = [record for record in wal.records() if record.kind != "checkpoint"]
    report.records = len(records)
    if not records:
        return report

    committed: set[int] = set()
    pending: dict[int, list] = {}
    for record in records:
        if record.txn_id == 0:
            continue
        if record.kind == COMMIT:
            committed.add(record.txn_id)
            pending.pop(record.txn_id, None)
            continue
        if record.kind == ABORT:
            continue
        pending.setdefault(record.txn_id, []).append(record)

    report.committed = tuple(sorted(committed))
    incomplete = sorted(txn for txn in pending if txn not in committed)
    report.rolled_back = tuple(incomplete)
    if not incomplete:
        return report

    report.recovered = True
    for txn_id in incomplete:
        txn_records = pending[txn_id]
        # HOW：同一页只认最早的那份前像，等价于"本次事务第一次改它之前的样子"。
        first_image: dict[int, tuple[int, bytes]] = {}
        for record in txn_records:
            if record.kind == PAGE and record.image is not None:
                if record.page_id not in first_image:
                    first_image[record.page_id] = (record.lsn, record.image)
        for page_id, (_lsn, image) in sorted(first_image.items()):
            buffer_pool.restore_page(page_id, image)
            report.pages_restored += 1
        for record in txn_records:
            if record.kind == ALLOCATE and record.page_id >= 0:
                disk.free(record.page_id)
                report.pages_reclaimed += 1

    buffer_pool.flush_all()
    disk.sync()
    # 撤销完成后事务已经不存在，写 abort 留痕再做一次 checkpoint 截断日志。
    for txn_id in incomplete:
        wal.append(txn_id, ABORT, description="crash recovery rollback")
    wal.flush()
    wal.checkpoint()
    report.truncated = True
    report.details = {
        "txn_records": sum(len(pending[txn]) for txn in incomplete),
    }
    return report
