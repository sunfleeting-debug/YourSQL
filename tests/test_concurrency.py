"""并发控制测试：表级封锁、隔离级别、死锁检测与锁超时。

并发用例全部依赖"事件"做同步点，不靠 sleep 猜时序；每个用例都带超时，
卡住时以失败结束而不是把测试挂死。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from yoursql.common import ConcurrencyError, DatabaseConfig
from yoursql.engine.runtime.database import Database

TIMEOUT = 20.0


def _rows(db: Database, sql: str) -> list[tuple[object, ...]]:
    return db.execute(sql).rows


def _seed(db: Database, table: str = "t", extra: str = "") -> None:
    db.execute(f"CREATE TABLE {table} (id INT PRIMARY KEY, v VARCHAR(20)){extra}")
    db.execute(f"INSERT INTO {table} VALUES (1, 'a')")


def _run(func, *args) -> threading.Thread:
    thread = threading.Thread(target=func, args=args, daemon=True)
    thread.start()
    return thread


def _wait_until(predicate, deadline: float = TIMEOUT) -> bool:
    """轮询等待条件成立；比 sleep 猜时序更确定，且不会无限挂起。"""

    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_exclusive_lock_serializes_two_writers(tmp_path: Path) -> None:
    """A 持有 X 锁期间 B 的写语句必须阻塞，直到 A 提交才拿到锁。"""

    with Database(tmp_path / "xlock.db") as db:
        _seed(db)
        a_locked = threading.Event()
        b_started = threading.Event()
        release_a = threading.Event()
        blocked_before_release: list[bool] = []
        acquired: list[str] = []

        def writer_a() -> None:
            db.execute("BEGIN")
            db.execute("UPDATE t SET v = 'A' WHERE id = 1")
            a_locked.set()
            assert release_a.wait(TIMEOUT)
            db.execute("COMMIT")
            acquired.append("a")

        def writer_b() -> None:
            assert a_locked.wait(TIMEOUT)
            db.execute("BEGIN")
            b_started.set()
            db.execute("UPDATE t SET v = 'B' WHERE id = 1")
            acquired.append("b")
            db.execute("COMMIT")

        ta, tb = _run(writer_a), _run(writer_b)
        assert a_locked.wait(TIMEOUT)
        assert b_started.wait(TIMEOUT)
        # B 已经进入等锁状态，且此刻尚未拿到锁
        assert _wait_until(lambda: db.lock_manager.waiters("t") != ())
        blocked_before_release.append(acquired == [])
        release_a.set()
        ta.join(TIMEOUT)
        tb.join(TIMEOUT)

        assert blocked_before_release == [True]
        assert acquired == ["a", "b"]
        assert _rows(db, "SELECT v FROM t WHERE id = 1") == [("B",)]
        assert db.lock_manager.snapshot()["resources"] == {}


def test_reader_waits_for_writer_exclusive_lock(tmp_path: Path) -> None:
    """自动提交的 SELECT 会取 S 锁，遇到未提交的写事务必须等待。"""

    with Database(tmp_path / "reader.db") as db:
        _seed(db)
        writer_locked = threading.Event()
        release_writer = threading.Event()
        read_started = threading.Event()
        observed: list[str] = []

        def writer() -> None:
            db.execute("BEGIN")
            db.execute("UPDATE t SET v = 'w' WHERE id = 1")
            writer_locked.set()
            assert release_writer.wait(TIMEOUT)
            db.execute("COMMIT")

        def reader() -> None:
            assert writer_locked.wait(TIMEOUT)
            read_started.set()
            observed.append(str(_rows(db, "SELECT v FROM t WHERE id = 1")[0][0]))

        tw, tr = _run(writer), _run(reader)
        assert writer_locked.wait(TIMEOUT)
        assert read_started.wait(TIMEOUT)
        # 读线程被写锁挡住，还没有产出结果
        assert _wait_until(lambda: db.lock_manager.waiters("t") != ())
        assert observed == []
        release_writer.set()
        tw.join(TIMEOUT)
        tr.join(TIMEOUT)
        # 写事务提交后读线程才拿到 S 锁，因此读到的是提交后的新值
        assert observed == ["w"]


def test_deadlock_is_detected_and_victim_is_rolled_back(tmp_path: Path) -> None:
    """两个事务互相等待时，等待图检出环并把较年轻的事务回滚。"""

    with Database(tmp_path / "deadlock.db") as db:
        db.execute("CREATE TABLE t1 (id INT PRIMARY KEY)")
        db.execute("CREATE TABLE t2 (id INT PRIMARY KEY)")
        db.execute("INSERT INTO t1 VALUES (1)")
        db.execute("INSERT INTO t2 VALUES (1)")
        ready = {1: threading.Event(), 2: threading.Event()}
        go = {1: threading.Event(), 2: threading.Event()}
        victims: list[int] = []
        survivors: list[int] = []

        def worker(index: int, first: str, second: str) -> None:
            db.execute("BEGIN")
            txn = db.current_transaction()
            assert txn is not None
            db.execute(f"INSERT INTO {first} VALUES (100 + {index})")
            ready[index].set()
            if not go[index].wait(TIMEOUT):
                return
            try:
                db.execute(f"INSERT INTO {second} VALUES (200 + {index})")
            except ConcurrencyError:
                victims.append(txn.txn_id)
                return
            survivors.append(txn.txn_id)
            db.execute("COMMIT")

        t1 = _run(worker, 1, "t1", "t2")
        t2 = _run(worker, 2, "t2", "t1")
        for event in ready.values():
            assert event.wait(TIMEOUT)
        for event in go.values():
            event.set()
        t1.join(TIMEOUT)
        t2.join(TIMEOUT)

        assert len(victims) == 1, (victims, survivors)
        assert len(survivors) == 1, (victims, survivors)
        # 事务号更大的是"更年轻"的一方，应当被选为牺牲者。
        assert victims[0] == max(victims[0], survivors[0])
        assert db.lock_manager.stats.deadlocks >= 1
        assert db.txn_manager.active_count == 0
        assert db.lock_manager.snapshot()["resources"] == {}


def test_lock_timeout_raises_concurrency_error(tmp_path: Path) -> None:
    with Database(
        tmp_path / "timeout.db", config=DatabaseConfig(lock_timeout_seconds=0.4)
    ) as db:
        _seed(db)
        db.execute("BEGIN")
        db.execute("UPDATE t SET v = 'held' WHERE id = 1")
        errors: list[str] = []

        def blocked() -> None:
            db.execute("BEGIN")
            try:
                db.execute("UPDATE t SET v = 'blocked' WHERE id = 1")
            except ConcurrencyError as exc:
                errors.append(exc.message)
                # 死锁/超时后事务已被整体回滚，不需要也不能再 ROLLBACK
                assert db.current_transaction() is None

        thread = _run(blocked)
        thread.join(TIMEOUT)
        assert errors and "超时" in errors[0]
        assert db.lock_manager.stats.timeouts >= 1
        db.execute("ROLLBACK")


def test_lock_mode_none_disables_locking(tmp_path: Path) -> None:
    with Database(
        tmp_path / "nolock.db", config=DatabaseConfig(lock_mode="none")
    ) as db:
        _seed(db)
        db.execute("BEGIN")
        db.execute("UPDATE t SET v = 'x' WHERE id = 1")
        assert db.current_transaction() is not None
        assert db.lock_manager.snapshot()["resources"] == {}
        db.execute("COMMIT")


def test_serializable_holds_shared_locks_until_commit(tmp_path: Path) -> None:
    with Database(tmp_path / "ser.db") as db:
        _seed(db)
        db.execute("BEGIN")
        txn = db.current_transaction()
        assert txn is not None
        db.execute("SELECT COUNT(*) FROM t")
        assert "t" in db.lock_manager.held_by(txn.txn_id)
        db.execute("COMMIT")
        assert db.lock_manager.held_by(txn.txn_id) == {}


def test_read_committed_releases_shared_locks_per_statement(tmp_path: Path) -> None:
    with Database(tmp_path / "rc.db") as db:
        _seed(db)
        db.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        db.execute("BEGIN")
        txn = db.current_transaction()
        assert txn is not None
        db.execute("SELECT COUNT(*) FROM t")
        assert db.lock_manager.held_by(txn.txn_id) == {}
        # X 锁不受读已提交影响，仍保持到事务结束
        db.execute("UPDATE t SET v = 'z' WHERE id = 1")
        assert db.lock_manager.held_by(txn.txn_id) == {"t": "X"}
        db.execute("COMMIT")


def test_concurrent_transactions_all_commit(tmp_path: Path) -> None:
    """多线程并发写入同一张表，最终行数必须完整。"""

    with Database(tmp_path / "stress.db") as db:
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, who INT)")
        threads_count, per_thread = 4, 6
        failures: list[str] = []

        def worker(index: int) -> None:
            for row in range(per_thread):
                value = index * per_thread + row
                for _attempt in range(5):
                    try:
                        db.execute("BEGIN")
                        db.execute(f"INSERT INTO t VALUES ({value}, {index})")
                        db.execute("COMMIT")
                        break
                    except ConcurrencyError as exc:
                        failures.append(f"{value}:{exc.message}")
                        if db.current_transaction() is not None:
                            db.execute("ROLLBACK")
                else:
                    failures.append(f"{value}:retry-exhausted")

        workers = [_run(worker, index) for index in range(threads_count)]
        for worker in workers:
            worker.join(TIMEOUT)

        assert failures == []
        assert _rows(db, "SELECT COUNT(*) FROM t") == [
            (threads_count * per_thread,)
        ]
        assert db.txn_manager.active_count == 0
        assert db.lock_manager.snapshot()["resources"] == {}


@pytest.mark.parametrize("isolation", ["serializable", "read_committed"])
def test_transaction_control_statements_need_no_object_privilege(
    tmp_path: Path, isolation: str
) -> None:
    """事务控制语句只影响会话状态，不做对象级授权——任何用户都能开事务。"""

    with Database(tmp_path / f"auth_{isolation}.db") as db:
        db.execute("CREATE USER bob IDENTIFIED BY 'pw'")
        session_type = type(db.session)
        # 换成没有任何对象权限的普通用户
        db.session = session_type(db.rbac.authenticate("bob", "pw"), db.rbac)
        level = isolation.upper().replace("_", " ")
        db.execute(f"SET TRANSACTION ISOLATION LEVEL {level}")
        db.execute("BEGIN")
        assert db.current_transaction() is not None
        db.execute("ROLLBACK")
