"""预写日志与崩溃恢复测试。

"崩溃"用 ``_crash`` 模拟：先把脏页落盘（对应缓冲池的 steal 行为），再直接关闭
文件句柄，**不跑回滚、也不补 commit 记录**——这正是断电时磁盘上会留下的状态。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yoursql.common import DatabaseConfig, RecoveryError
from yoursql.engine.runtime.database import Database
from yoursql.storage import (
    BufferPool,
    DiskManager,
    Page,
    PageType,
    WriteAheadLog,
    recover,
    wal_path_for,
)
from yoursql.storage.page import HEADER_SIZE, PAGE_MAGIC
from yoursql.storage.wal import ABORT, ALLOCATE, BEGIN, COMMIT, PAGE


def _rows(db: Database, sql: str) -> list[tuple[object, ...]]:
    return db.execute(sql).rows


def _crash(db: Database) -> None:
    """模拟断电：脏页已经落到磁盘，但没有任何事务收尾动作。"""

    db.buffer_pool.flush_all()
    db.disk.close()
    db.wal.close()
    db._closed = True


def _seed(path: Path) -> None:
    with Database(path) as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v VARCHAR(20))")
        db.execute("INSERT INTO t VALUES (1, 'a')")


def test_page_lsn_is_stamped_on_modified_pages(tmp_path: Path) -> None:
    path = tmp_path / "lsn.db"
    _seed(path)
    with Database(path) as db:
        db.execute("INSERT INTO t VALUES (2, 'b')")
        table = db.catalog.get_table("t")
        page_id = int(table.page_ids[0])
        page = db.buffer_pool.peek_page(page_id)
        assert page.lsn > 0
        assert page.lsn <= db.wal.next_lsn


def test_wal_is_flushed_before_dirty_page_reaches_disk(tmp_path: Path) -> None:
    """写前日志规则：页 LSN 之前的所有日志记录必须已经 fsync。"""

    path = tmp_path / "wal_rule.db"
    _seed(path)
    with Database(path) as db:
        # 统计每个"带 LSN 的页"被写回时，日志刷到了哪个位置。
        original_write = db.disk.write
        observed: list[tuple[int, int]] = []

        def spy(page: Page) -> None:
            if page.lsn:
                observed.append((page.lsn, db.wal.persisted_lsn))
            original_write(page)

        db.disk.write = spy  # type: ignore[method-assign]
        try:
            db.execute("INSERT INTO t VALUES (2, 'b'), (3, 'c')")
        finally:
            db.disk.write = original_write  # type: ignore[method-assign]

        assert observed, "本次写入没有任何数据页落盘"
        assert all(lsn <= persisted for lsn, persisted in observed), observed


def test_commit_truncates_log_and_keeps_lsn_monotonic(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.db"
    _seed(path)
    with Database(path) as db:
        lsn_before = db.wal.next_lsn
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (2, 'b')")
        db.execute("COMMIT")
        assert db.wal.next_lsn > lsn_before
        # 提交后已全部落盘，日志被截断成只剩文件头
        assert [record.kind for record in db.wal.records()] == []
        assert db.wal.stats()["bytes"] < 200


def test_crash_rolls_back_uncommitted_transaction(tmp_path: Path) -> None:
    path = tmp_path / "crash_uncommitted.db"
    _seed(path)
    db = Database(path)
    db.execute("BEGIN")
    txn = db.current_transaction()
    assert txn is not None
    txn_id = txn.txn_id
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db.execute("UPDATE t SET v = 'zz' WHERE id = 1")
    _crash(db)

    with Database(path) as recovered:
        report = recovered.recovery_report
        assert report.rolled_back == (txn_id,)
        assert report.pages_restored >= 1
        assert recovered.recovery_report.recovered is True
        assert _rows(recovered, "SELECT id, v FROM t ORDER BY id") == [(1, "a")]


def test_crash_keeps_committed_transaction(tmp_path: Path) -> None:
    path = tmp_path / "crash_committed.db"
    _seed(path)
    db = Database(path)
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db.execute("COMMIT")
    _crash(db)

    with Database(path) as recovered:
        # 提交前所有脏页已落盘且日志已截断，因此无需恢复
        assert recovered.recovery_report.recovered is False
        assert _rows(recovered, "SELECT id, v FROM t ORDER BY id") == [
            (1, "a"),
            (2, "b"),
        ]


def test_crash_reclaims_pages_allocated_by_uncommitted_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "crash_pages.db"
    _seed(path)
    db = Database(path)
    pages_before = db.disk.page_count
    db.execute("BEGIN")
    # 足够多的行迫使事务分配新页
    db.execute(
        "INSERT INTO t VALUES "
        + ", ".join(f"({index}, 'v{index:04d}')" for index in range(2, 400))
    )
    assert db.disk.page_count > pages_before
    txn = db.current_transaction()
    assert txn is not None and txn.allocated_pages
    allocated = len(txn.allocated_pages)
    _crash(db)

    with Database(path) as recovered:
        assert recovered.recovery_report.pages_reclaimed == allocated
        assert _rows(recovered, "SELECT COUNT(*) FROM t") == [(1,)]


def test_recovery_only_undoes_incomplete_transactions(tmp_path: Path) -> None:
    """直接构造日志：带 commit 的事务不动，缺 commit 的事务按前像撤销。"""

    path = tmp_path / "grouping.db"
    _seed(path)
    with Database(path) as db:
        table = db.catalog.get_table("t")
        page_id = int(table.page_ids[0])
        original = db.disk.peek(page_id).to_bytes()

    wal = WriteAheadLog(wal_path_for(path))
    wal.append(91, BEGIN)
    wal.append(91, PAGE, page_id=page_id, image=original)
    wal.append(91, COMMIT)
    wal.append(92, BEGIN)
    wal.append(92, PAGE, page_id=page_id, image=original)
    wal.flush()
    wal.close()

    # 把页写坏，模拟"未提交事务的脏页已经落盘"
    disk = DiskManager(path)
    disk.write(
        Page(page_id, disk.page_size, PageType.HEAP, b"BROKEN-PAGE-CONTENT")
    )
    disk.close()

    with Database(path) as recovered:
        report = recovered.recovery_report
        assert report.committed == (91,)
        assert report.rolled_back == (92,)
        assert report.pages_restored == 1
        assert recovered.disk.peek(page_id).to_bytes() == original


def test_recovery_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    _seed(path)
    db = Database(path)
    db.execute("BEGIN")
    txn = db.current_transaction()
    assert txn is not None
    txn_id = txn.txn_id
    db.execute("INSERT INTO t VALUES (2, 'b')")
    _crash(db)

    with Database(path) as first:
        assert first.recovery_report.rolled_back == (txn_id,)
        rows = _rows(first, "SELECT id FROM t ORDER BY id")
    with Database(path) as second:
        assert second.recovery_report.recovered is False
        assert _rows(second, "SELECT id FROM t ORDER BY id") == rows == [(1,)]


def test_torn_tail_line_is_tolerated(tmp_path: Path) -> None:
    """崩溃时写了一半的最后一行按"尾部截断"处理，不影响前面的记录。"""

    path = tmp_path / "torn.wal"
    wal = WriteAheadLog(path)
    wal.append(1, BEGIN)
    wal.append(1, PAGE, page_id=3, image=b"x" * 8)
    wal.flush()
    wal.close()
    with path.open("ab") as stream:
        stream.write(b'{"lsn": 99, "txn": 9, "kin')

    reopened = WriteAheadLog(path)
    try:
        records = list(reopened.records())
        assert [(record.lsn, record.kind) for record in records] == [
            (1, BEGIN),
            (2, PAGE),
        ]
        # 坏行之后的 LSN 继续递增，不会复用已经出现过的号
        assert reopened.next_lsn > 2
    finally:
        reopened.close()


def test_corrupt_middle_line_raises_recovery_error(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.wal"
    wal = WriteAheadLog(path)
    wal.append(1, BEGIN)
    wal.append(1, PAGE, page_id=3, image=b"x" * 8)
    wal.append(1, COMMIT)
    wal.flush()
    wal.close()
    lines = path.read_bytes().split(b"\n")
    lines[1] = b"{not json at all"
    path.write_bytes(b"\n".join(lines))

    with pytest.raises(RecoveryError):
        reopened = WriteAheadLog(path)
        list(reopened.records())


def test_wal_can_be_disabled(tmp_path: Path) -> None:
    path = tmp_path / "nowal.db"
    with Database(path, config=DatabaseConfig(wal_enabled=False)) as db:
        assert db.wal.enabled is False
        db.execute("CREATE TABLE t (id INT PRIMARY KEY)")
        db.execute("BEGIN")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("ROLLBACK")
        # 事务语义不依赖日志文件，回滚仍由内存前像完成
        assert _rows(db, "SELECT COUNT(*) FROM t") == [(0,)]
        assert db.wal.stats()["page_images"] >= 1


def test_legacy_page_header_without_lsn_still_reads(tmp_path: Path) -> None:
    """旧文件页头末 4 字节恒为 0，按 LSN=0 读出，格式向后兼容。"""

    page = Page(7, 4096, PageType.HEAP, b"payload", 4242)
    raw = page.to_bytes()
    legacy = raw[: HEADER_SIZE - 4] + b"\x00\x00\x00\x00" + raw[HEADER_SIZE:]
    parsed = Page.from_bytes(legacy, page_size=4096)
    assert parsed.lsn == 0
    assert parsed.payload == b"payload"
    assert parsed.page_id == 7
    assert PAGE_MAGIC in legacy[:4]
    assert Page.from_bytes(raw, page_size=4096).lsn == 4242


def test_recover_on_clean_database_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "clean.db"
    _seed(path)
    with Database(path) as db:
        assert db.recovery_report.records == 0
        assert db.recovery_report.recovered is False

    disk = DiskManager(path)
    wal = WriteAheadLog(wal_path_for(path))
    pool = BufferPool(disk, capacity=4, wal=wal)
    try:
        report = recover(wal, pool, disk)
        assert report.records == 0
        assert report.rolled_back == ()
    finally:
        pool.close()
        wal.close()
        disk.close()


def test_wal_records_carry_before_images_and_allocations(tmp_path: Path) -> None:
    path = tmp_path / "records.db"
    _seed(path)
    with Database(path) as db:
        db.execute("BEGIN")
        # 写入足够多行，迫使事务申请新页，从而产生 alloc 记录
        db.execute(
            "INSERT INTO t VALUES "
            + ", ".join(f"({index}, 'v{index:04d}')" for index in range(2, 400))
        )
        records = list(db.wal.records())
        kinds = [record.kind for record in records]
        assert BEGIN in kinds
        assert PAGE in kinds
        assert ALLOCATE in kinds
        page_records = [record for record in records if record.kind == PAGE]
        assert all(record.image for record in page_records)
        assert all(len(record.image) == db.config.page_size for record in page_records)
        db.execute("ROLLBACK")
        assert ABORT in [record.kind for record in db.wal.records()]
