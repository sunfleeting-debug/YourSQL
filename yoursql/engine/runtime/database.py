"""Database 运行时协调器：编译、优化、执行和页式持久化。"""
#串联编译，计划，权限检查，执行和持久化
from __future__ import annotations

import os
import struct
import tempfile
import threading
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Iterable

from yoursql.common import (
    CatalogError,
    ConcurrencyError,
    DatabaseConfig,
    ExecutionResult,
    ExecutionError,
    SqlValue,
    TransactionError,
    YourSQLError,
    StorageError,
)
from yoursql.common.config import default_audit_path
from yoursql.common.codec import PayloadCodecError, decode_payload
from yoursql.common.types import PageId, RowId
from yoursql.sql.ast import (
    BeginTransaction,
    Commit,
    CreateIndex,
    DropIndex,
    Explain,
    Rollback,
    Select,
    SetTransaction,
    Show,
    ShowGrants,
    Statement,
)
from yoursql.sql.binder import Binder
from yoursql.sql.compiler import CompilationResult, Compiler
from yoursql.sql.lexer import tokenize
from yoursql.sql.diagnostics import ParseOutcome
from yoursql.sql.parser import Parser, parse_recovering
from yoursql.planner.logical import LogicalPlanNode, plan_from_statement
from yoursql.planner.optimizer import CostEstimate, Optimizer, StatisticsStore
from yoursql.planner.physical import PhysicalPlanNode, PlanNode
from yoursql.execution.evaluator import ExpressionEvaluator
from yoursql.execution.query import QueryExecutionMixin, _RowContextTemplate
from yoursql.storage import (
    BufferPool,
    DiskManager,
    IndexManager,
    IndexPayloadEntry,
    Page,
    PageType,
    TableHeap,
    RecoveryReport,
    WriteAheadLog,
    recover,
    wal_path_for,
)
from yoursql.storage.wal import ABORT, ALLOCATE, BEGIN, COMMIT as WAL_COMMIT, PAGE
from yoursql.engine.concurrency import (
    LockManager,
    LockMode,
    Transaction,
    TransactionManager,
    TransactionState,
)
from yoursql.storage.page import HEADER_SIZE
from yoursql.engine.security.audit import AuditLog
from yoursql.engine.security.auth import RBAC
from yoursql.engine.catalog import Catalog, IndexMetadata, TableMetadata
from yoursql.engine.security.session import Session
from yoursql.engine.system_catalog import SystemCatalog
from yoursql.engine.runtime.commands import DatabaseCommandMixin


# HOW：批量导入按块处理，既让页写入成批（减少整页重编码），又不把全部行都堆在内存里。
_INSERT_BATCH_ROWS = 4096

_CATALOG_CHAIN_MAGIC = b"MCAT2"
_CATALOG_CHAIN_HEADER = struct.Struct("<5sQ")


@dataclass(frozen=True)
class _CatalogPageChunk:
    """目录页链解码结果。"""

    next_page_id: int | None
    chunk: bytes


def _with_location(
    error: YourSQLError, location: tuple[int, int] | None
) -> YourSQLError:
    """把 SQL 内部异常补到源码位置；外部/存储异常保持原有错误信息。"""

    if location is None or error.line is not None:
        return error
    if type(error) is YourSQLError:
        return YourSQLError(
            error.message, error.code, location[0], location[1], dict(error.details)
        )
    return type(error)(
        error.message, line=location[0], column=location[1], **error.details
    )


class Database(ExpressionEvaluator, QueryExecutionMixin, DatabaseCommandMixin):
    """数据库运行时协调器。

    本类只负责生命周期、目录持久化、事务边界和跨层编排；SQL 命令细节
    由 ``DatabaseCommandMixin`` 负责，查询执行细节由 ``QueryExecutionMixin``
    负责，表达式求值由 ``ExpressionEvaluator`` 负责。
    """

    @staticmethod
    def detect_page_size(path: str | os.PathLike[str]) -> int | None:
        """读取已有数据库 superblock 中的页大小，供跨配置打开和切库使用。"""

        candidate = Path(path)
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            return None
        # 逐步尝试常见页大小；页头负载通常很小，即使候选大小小于实际页也能解析出 superblock。
        for probe_size in (
            512,
            1024,
            2048,
            4096,
            8192,
            16 * 1024,
            32 * 1024,
            64 * 1024,
            128 * 1024,
        ):
            try:
                with candidate.open("rb") as stream:
                    raw = stream.read(probe_size)
                if len(raw) != probe_size:
                    continue
                page = Page.from_bytes(raw, page_size=probe_size)
                if page.page_type is not PageType.SUPERBLOCK:
                    continue
                payload = decode_payload(page.payload)[0] if page.payload else {}
                stored_size = int(payload.get("page_size", 0))
                if stored_size >= HEADER_SIZE and stored_size & (stored_size - 1) == 0:
                    return stored_size
            except (
                OSError,
                StorageError,
                PayloadCodecError,
                UnicodeDecodeError,
                ValueError,
                TypeError,
            ):
                continue
        return None

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        config: DatabaseConfig | None = None,
        audit_path: str | os.PathLike[str] | None = None,
        user: str = "admin",
        password: str = "admin",
        disabled_rules: Iterable[str] = (),
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        selected_page_size = None
        if config is None and path is not None and str(path) != ":memory:":
            selected_page_size = self.detect_page_size(path)
        self.config = config or DatabaseConfig(page_size=selected_page_size or 4096)
        self._temporary_path: Path | None = None
        if path is None or str(path) == ":memory:":
            handle = tempfile.NamedTemporaryFile(
                prefix="yoursql-", suffix=".db", delete=False
            )
            handle.close()
            self._temporary_path = Path(handle.name)
            self.path = self._temporary_path
        else:
            self.path = Path(path)
            # HOW：默认数据库位于 ./data；首次启动时自动准备父目录，CLI、Web 和直接 API 行为保持一致。
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # HOW：打开文件并准备缓冲池，再加载目录，以恢复表结构与数据位置。
        self.disk = DiskManager(
            self.path,
            page_size=self.config.page_size,
            payload_codec=self.config.payload_codec,
        )
        # WHY：旧 JSON 库可能在配置中请求 manual，但实际格式由 superblock 决定；
        # 后续所有页必须跟随打开文件的实际编码，避免同库混写。
        self.config = replace(self.config, payload_codec=self.disk.payload_codec.name)
        self.payload_codec = self.disk.payload_codec
        # HOW：日志必须早于缓冲池建立；脏页写回时先刷日志，恢复也必须先于目录装载。
        self.wal = WriteAheadLog(
            wal_path_for(self.path), enabled=self.config.wal_enabled
        )
        self.buffer_pool = BufferPool(
            self.disk,
            self.config.buffer_pool_size,
            self.config.replacement_policy,
            protect_page_types=self.config.protect_page_types,
            wal=self.wal,
        )
        self.buffer_pool.image_sink = None
        self.disk.allocate_hook = None
        self.recovery_report: RecoveryReport = recover(
            self.wal, self.buffer_pool, self.disk
        )
        # ----- 事务与并发（必须早于目录装载：分配目录页会走 allocate_hook） -----
        self.txn_manager = TransactionManager()
        self.lock_manager = LockManager(
            timeout=self.config.lock_timeout_seconds,
            enabled=self.config.lock_mode != "none",
        )
        self._default_isolation = self.config.default_isolation
        # HOW：线程局部保存"当前线程正在执行的事务"。数据库实例可被多线程共享，
        # 每个线程各自开着一条事务，封锁冲突才真正发生。
        self._txn_local = threading.local()
        # HOW：自动提交的只读语句用负数编号临时占用锁，避免与真实事务号冲突。
        self._read_scope_seq = count(1)
        # HOW：恢复阶段的改动不属于任何事务，因此两个回调在恢复之后才挂上。
        self.buffer_pool.image_sink = self._capture_page_image
        self.disk.allocate_hook = self._note_page_allocation
        self.catalog = self._load_catalog()
        self.index_manager = IndexManager()
        self._heaps: dict[int, TableHeap] = {}
        # HOW：上下文模板按（别名, 表名, 需要列, 是否需要行序）缓存，命中时用 schema 身份校验兼容性。
        self._context_templates: dict[
            tuple[str, str, frozenset[str] | None, bool], _RowContextTemplate
        ] = {}
        # HOW：索引候选集缓存。键含索引名与约束签名（交集层用参与索引签名集合），
        # 任何写操作或索引 DDL 都会整体清空（见 _invalidate_candidate_cache）。
        self._candidate_cache: dict[tuple[object, ...], tuple[RowId, ...] | None] = {}
        # HOW：记录本次 SELECT 实际使用的扫描方式，供 ExecutionResult.stats 标记 IndexOnlyScan。
        self._last_scan_kind: str | None = None
        # HOW：记录本次 SELECT 每个连接采用的策略（HashJoin / IndexNestedLoop / NestedLoop）。
        self._join_kinds: list[str] = []
        self._lock = RLock()
        self.system_catalog = SystemCatalog(self)
        system_tables_changed = self.system_catalog.ensure_tables()
        system_views_changed = self.system_catalog.ensure_views()
        if system_tables_changed or system_views_changed:
            self._persist_catalog()
        internal_rbac = self.system_catalog.load_rbac()
        if internal_rbac is None:
            # HOW：新数据库的内部权限表为空时，写入默认 admin 账户。
            self.rbac = RBAC()
            self.system_catalog.persist_rbac(self.rbac)
        else:
            self.rbac = internal_rbac
        # HOW：默认按启动日期分文件；显式路径用于测试、嵌入式调用或独立审计目录。
        selected_audit_path = (
            default_audit_path() if audit_path is None else audit_path
        )
        self.audit = AuditLog(selected_audit_path)
        self.session = Session(self.rbac.authenticate(user, password), self.rbac)
        self.optimizer = Optimizer(
            StatisticsStore(),
            buffer_pool_pages=self.config.buffer_pool_size,
            # HOW：允许从外部（CLI / 演示脚本）关掉若干条优化规则，
            # 用来现场对比"同一语句、开/关某条规则"的计划差异。
            disabled_rules=tuple(disabled_rules),
        )
        self.compiler = Compiler()
        self._rebuild_indexes()
        self._refresh_statistics()
        self._closed = False

    # ----- Catalog 页链与存储对象生命周期 -----
    # HOW: 恢复链路：命名入口 catalog -> 目录页链 -> JSON 字典 -> 内存 Catalog。
    def _load_catalog(self) -> Catalog:
        # HOW: 找到的是目录首页；用户表的数据位置保存在目录的 page_ids 中。
        """从目录页链加载 Catalog，必要时创建空目录。"""
        page_id = self.disk.named_page("catalog")
        if page_id is None:
            catalog = Catalog()
            page = self.disk.allocate(
                PageType.CATALOG,
                self._catalog_page_payload(self._catalog_payload(catalog), 0),
            )
            self.disk.register_named_page("catalog", page.page_id)
            self.disk.sync()
            return catalog

        chunks: list[bytes] = []
        visited: set[int] = set()
        current_page_id: int | None = page_id
        while current_page_id is not None:
            if current_page_id in visited:
                raise CatalogError("catalog 页链存在循环")
            visited.add(current_page_id)
            page = self.disk.read(current_page_id)
            if page.page_type is not PageType.CATALOG:
                raise CatalogError("命名 catalog 页类型错误")
            decoded = self._decode_catalog_page(page.payload)
            chunks.append(decoded.chunk)
            if decoded.next_page_id is None:
                break
            current_page_id = decoded.next_page_id or None
        try:
            data = decode_payload(b"".join(chunks), self.payload_codec)[0] if chunks else {}
        except (PayloadCodecError, UnicodeDecodeError, ValueError) as exc:
            raise CatalogError("catalog 页链 payload 损坏") from exc
        if not isinstance(data, dict):
            raise CatalogError("catalog 页不是对象")
        # HOW: 恢复元数据对象；用户记录在后续查询时才通过表堆读取。
        return Catalog.from_dict(data)

    def _catalog_payload(self, catalog: Catalog) -> bytes:
        """将 Catalog 编码为目录页链使用的字节载荷。"""
        return self.payload_codec.encode(catalog.to_dict())

    @staticmethod
    def _catalog_page_payload(chunk: bytes, next_page_id: int) -> bytes:
        """为 Catalog 分片添加链指针；旧版未带前缀的单页仍可读取。"""

        return (
            _CATALOG_CHAIN_HEADER.pack(_CATALOG_CHAIN_MAGIC, int(next_page_id)) + chunk
        )

    @staticmethod
    def _decode_catalog_page(payload: bytes) -> _CatalogPageChunk:
        """读取链式 Catalog 页，兼容旧版裸 JSON 页。"""

        if not payload.startswith(_CATALOG_CHAIN_MAGIC):
            return _CatalogPageChunk(None, payload)
        if len(payload) < _CATALOG_CHAIN_HEADER.size:
            raise CatalogError("catalog 页链头部不完整")
        magic, next_page_id = _CATALOG_CHAIN_HEADER.unpack(
            payload[: _CATALOG_CHAIN_HEADER.size]
        )
        if magic != _CATALOG_CHAIN_MAGIC:
            raise CatalogError("catalog 页链魔数错误")
        return _CatalogPageChunk(
            int(next_page_id), payload[_CATALOG_CHAIN_HEADER.size :]
        )

    def _catalog_page_ids(self, first_page_id: int) -> list[int]:
        """返回当前 Catalog 页链，供扩容和缩容时复用物理页。"""

        page_ids: list[int] = []
        visited: set[int] = set()
        current_page_id: int | None = int(first_page_id)
        while current_page_id is not None:
            if current_page_id in visited:
                raise CatalogError("catalog 页链存在循环")
            visited.add(current_page_id)
            page_ids.append(current_page_id)
            page = (
                self.buffer_pool.peek_page(current_page_id)
                if current_page_id in self.buffer_pool
                else self.disk.read(current_page_id)
            )
            if page.page_type is not PageType.CATALOG:
                raise CatalogError("catalog 页链包含非 CATALOG 页")
            decoded = self._decode_catalog_page(page.payload)
            current_page_id = (
                None
                if decoded.next_page_id in {None, 0}
                else decoded.next_page_id
            )
        return page_ids

    # HOW: 保存链路：Catalog -> 字典 -> JSON 字节 -> 分片目录页 -> 刷新到文件。
    def _persist_catalog(self) -> None:
        """把当前 Catalog 分块写入目录页链。"""
        payload = self._catalog_payload(self.catalog)
        chunk_capacity = (
            self.config.page_size - Page.HEADER_SIZE - _CATALOG_CHAIN_HEADER.size
        )
        if chunk_capacity <= 0:
            raise CatalogError("页大小不足以容纳 catalog 链头")
        chunks = [
            payload[offset : offset + chunk_capacity]
            for offset in range(0, len(payload), chunk_capacity)
        ] or [b""]

        first_page_id = self.disk.named_page("catalog")
        existing_page_ids = (
            self._catalog_page_ids(first_page_id) if first_page_id is not None else []
        )
        page_ids = list(existing_page_ids[: len(chunks)])
        while len(page_ids) < len(chunks):
            page_ids.append(self.disk.allocate(PageType.CATALOG).page_id)

        for index, chunk in enumerate(chunks):
            next_page_id = page_ids[index + 1] if index + 1 < len(page_ids) else 0
            page = Page(
                page_ids[index],
                self.config.page_size,
                PageType.CATALOG,
                self._catalog_page_payload(chunk, next_page_id),
            )
            self.buffer_pool.put_page(page, dirty=True)
        for stale_page_id in existing_page_ids[len(chunks) :]:
            self.buffer_pool.delete_page(stale_page_id)
        if first_page_id is None:
            self.disk.register_named_page("catalog", page_ids[0])
        for page_id in page_ids:
            self.buffer_pool.flush_page(page_id)

    def _persist_rbac(self) -> None:
        """把当前 RBAC 快照写入内部权限表，供重启后的会话认证使用。"""

        self.system_catalog.persist_rbac(self.rbac)

    def _rebuild_indexes(self) -> None:
        """根据目录元数据重建索引并更新根页。"""
        metadata_changed = False
        for metadata in self.catalog.indexes():

            def update_root(page_id: int, metadata: IndexMetadata = metadata) -> None:
                """更新索引根页等关联状态。"""
                nonlocal metadata_changed
                selected = PageId(page_id)
                if metadata.root_page_id != selected:
                    metadata.root_page_id = selected
                    metadata_changed = True

            tree = self.index_manager.create(
                metadata.name,
                unique=metadata.unique,
                buffer_pool=self.buffer_pool,
                root_page_id=metadata.root_page_id,
                on_root_change=update_root,
            )
            if metadata.root_page_id != (
                None if tree.root_page_id is None else PageId(tree.root_page_id)
            ):
                metadata.root_page_id = (
                    None if tree.root_page_id is None else PageId(tree.root_page_id)
                )
                metadata_changed = True
        if metadata_changed:
            self._persist_catalog()

    def _refresh_statistics(self) -> None:
        """刷新表统计信息并使相关计划缓存失效。"""
        for table in self.catalog.tables():
            self.optimizer.statistics.update(table.name, table.stats)
        # WHY：计划同时依赖行数与索引元数据；写入或 DDL 后不能继续复用旧访问路径。
        self.optimizer.cache.invalidate()

    # HOW: 按表 ID 缓存表堆对象，首次用目录中的 page_ids 构建，不加载全表数据。
    def _heap(self, table: TableMetadata) -> TableHeap:
        """获取或创建表对应的堆表实例。"""
        key = int(table.table_id)
        heap = self._heaps.get(key)
        if heap is None:
            heap = TableHeap(
                self.buffer_pool, [int(page_id) for page_id in table.page_ids]
            )
            self._heaps[key] = heap
        return heap

    # ----- 事务生命周期 -----
    def current_transaction(self) -> Transaction | None:
        """返回当前线程的活动事务；没有则返回 ``None``。"""

        txn = getattr(self._txn_local, "txn", None)
        return txn if txn is not None and txn.active else None

    def begin_transaction(
        self,
        isolation: str | None = None,
        *,
        snapshot_catalog: bool = True,
        implicit: bool = False,
    ) -> Transaction:
        """开启一条事务并把它绑定到当前线程。"""

        if self.current_transaction() is not None:
            raise TransactionError("当前已经有活动事务，不能重复 BEGIN")
        txn = self.txn_manager.begin(isolation or self._default_isolation)
        txn.implicit = implicit
        if snapshot_catalog:
            # HOW：目录快照让 DDL 也能回滚——回滚时按快照重建目录对象，
            # 页级前像负责把目录页的物理内容恢复原状。
            txn.catalog_snapshot = self.catalog.to_dict()
        self._txn_local.txn = txn
        self.wal.append(txn.txn_id, BEGIN, description=txn.isolation)
        return txn

    def explicit_transaction(self) -> Transaction | None:
        """返回当前线程的显式事务；隐式（自动提交）事务返回 ``None``。"""

        txn = self.current_transaction()
        return txn if txn is not None and not txn.implicit else None

    def commit_transaction(self) -> dict[str, object]:
        """提交当前事务；返回事务统计。"""

        txn = self.current_transaction()
        if txn is None:
            raise TransactionError("没有活动事务，COMMIT 无处可提交")
        if txn.failed:
            raise TransactionError(
                "事务中有语句执行失败，COMMIT 被拒绝，请先 ROLLBACK",
                txn_id=txn.txn_id,
            )
        dirty = bool(txn.page_images) or txn.catalog_snapshot is not None
        if dirty:
            # 提交顺序即恢复策略：先把目录与数据页全部落盘，再写 commit 记录。
            # 这样"有 commit 记录"就等价于"改动已经全部在磁盘上"，恢复时无需 redo。
            self._persist_catalog()
            self.buffer_pool.flush_all()
            self.wal.append(txn.txn_id, WAL_COMMIT, description="commit")
            self.wal.flush()
            if self.txn_manager.active_count == 1:
                # 没有其它并发事务时，已提交内容全部落盘，日志可以整段截断。
                self.wal.checkpoint()
        stats = txn.stats()
        txn.finish(TransactionState.COMMITTED)
        self._release_transaction(
            txn, TransactionState.COMMITTED, dirty=self._is_dirty(txn)
        )
        return stats

    def rollback_transaction(self) -> dict[str, object]:
        """回滚当前事务；返回事务统计。"""

        txn = self.current_transaction()
        if txn is None:
            raise TransactionError("没有活动事务，ROLLBACK 无处可回滚")
        stats = txn.stats()
        self._undo_transaction(txn)
        self._release_transaction(
            txn, TransactionState.ABORTED, dirty=self._is_dirty(txn)
        )
        return stats

    def _release_transaction(
        self, txn: Transaction, state: TransactionState, *, dirty: bool
    ) -> None:
        """结束事务、释放锁，并只在真的改过数据时刷新统计与缓存。

        WHY：只读语句也会开事务执行；若无条件刷新统计就没收计划缓存，
        会让"同一 SQL 第二次执行命中缓存"的行为失效。
        """

        self.txn_manager.finish(txn, state)
        self.lock_manager.release_all(txn.txn_id)
        self._txn_local.txn = None
        if dirty:
            self._refresh_statistics()
            self._invalidate_candidate_cache()

    def _undo_transaction(self, txn: Transaction) -> None:
        """用前像把事务改动逐页撤销，再恢复目录快照。"""

        for image in txn.undo_plan():
            self.buffer_pool.restore_page(image.page_id, image.image)
        # HOW：事务新分配的页在回滚后不再被任何目录结构引用，精确回收它们，
        # 而不是把 next_page_id 整体回退（并发下回退页号会与其它事务撞车）。
        for page_id in reversed(txn.allocated_pages):
            try:
                self.disk.free(page_id)
            except StorageError:
                continue
        if txn.catalog_snapshot is not None:
            self._restore_catalog(txn.catalog_snapshot)
        self.buffer_pool.flush_all()
        self.wal.append(txn.txn_id, ABORT, description="rollback")
        self.wal.flush()

    def _restore_catalog(self, snapshot: dict[str, object]) -> None:
        """按快照重建目录，并让索引/堆缓存与之一致。"""

        restored = Catalog.from_dict(snapshot)
        wanted = {metadata.name.lower() for metadata in restored.indexes()}
        for name, tree in self.index_manager.items():
            self.index_manager.drop(name)
            if name not in wanted:
                # 回滚期间新建的索引就此消失，释放它的页。
                tree.destroy()
        self.catalog = restored
        self._heaps.clear()
        self._rebuild_indexes()
        self._persist_catalog()

    def set_default_isolation(self, isolation: str) -> str:
        normalized = isolation.strip().lower()
        if normalized not in {"serializable", "read_committed"}:
            raise TransactionError(f"不支持的隔离级别 {isolation!r}")
        self._default_isolation = normalized
        return normalized

    def transaction_state(self) -> dict[str, object]:
        """返回事务/封锁/日志的运行时快照，供工作台与验收演示查看。"""

        txn = self.current_transaction()
        return {
            "current": txn.stats() if txn is not None else None,
            "default_isolation": self._default_isolation,
            "transactions": self.txn_manager.stats(),
            "locks": self.lock_manager.snapshot(),
            "wal": self.wal.stats(),
            "recovery": self.recovery_report.as_dict(),
        }

    # ----- 前像捕获、页分配与语句级封锁 -----
    def _capture_page_image(self, page_id: int, raw: bytes, previous_lsn: int) -> int:
        """缓冲池回调：把页的首次前像记到当前事务并写入预写日志。"""

        txn = self.current_transaction()
        if txn is None:
            return 0
        existing = txn.page_lsn(page_id)
        if existing:
            return existing
        record = self.wal.append(
            txn.txn_id, PAGE, page_id=page_id, image=raw, page_lsn=previous_lsn
        )
        txn.record_page_image(page_id, raw, previous_lsn, record.lsn)
        return record.lsn

    def _note_page_allocation(self, page_id: int) -> None:
        """磁盘回调：页分配绕过了缓冲池，需要事务单独记账才能回滚。"""

        txn = self.current_transaction()
        if txn is None:
            return
        txn.note_allocated(page_id)
        self.wal.append(txn.txn_id, ALLOCATE, page_id=page_id)

    @staticmethod
    def _is_read_only(statement: Statement) -> bool:
        if isinstance(statement, Explain):
            return Database._is_read_only(statement.statement)
        return isinstance(statement, (Select, Show, ShowGrants))

    def _statement_resources(
        self, statement: Statement
    ) -> tuple[set[str], set[str]]:
        """把语句映射成（需要 S 锁的表, 需要 X 锁的表）。"""

        if isinstance(statement, Explain):
            statement = statement.statement
        names = set(self._object_names(statement))
        if isinstance(statement, (CreateIndex, DropIndex)):
            # HOW：CREATE/DROP INDEX 改的是表的数据结构，锁必须落在表上，
            # 而不是索引名——否则同一张表上的并发写不会被挡住。
            table_name = statement.table if isinstance(statement, CreateIndex) else None
            if table_name is None:
                metadata = self.catalog.find_index(statement.name)
                if metadata is not None:
                    table_name = next(
                        (
                            table.name
                            for table in self.catalog.tables(include_system=True)
                            if int(table.table_id) == int(metadata.table_id)
                        ),
                        None,
                    )
            names.discard(statement.name)
            if table_name:
                names.add(table_name)
        if not names:
            return set(), set()
        if self._is_read_only(statement):
            return names, set()
        return set(), names

    def _acquire_locks(self, txn: Transaction, statement: Statement) -> None:
        reads, writes = self._statement_resources(statement)
        for name in sorted(writes):
            self.lock_manager.acquire(txn.txn_id, name, LockMode.EXCLUSIVE)
            txn.write_resources.add(name.lower())
        for name in sorted(reads):
            self.lock_manager.acquire(txn.txn_id, name, LockMode.SHARED)
            txn.read_resources.add(name.lower())

    # ----- 对外 SQL 管线与批量写入入口 -----
    #用户输入SQL，生成计划并执行
    def execute(self, sql: str) -> ExecutionResult:
        """执行单条或脚本 SQL；脚本返回最后一条语句的结果。"""

        results = self.execute_script(sql)
        return results[-1] if results else ExecutionResult(message="没有可执行的 SQL")

    def insert_rows(
        self,
        table_name: str,
        rows: Iterable[Iterable[SqlValue]],
        *,
        validate_constraints: bool = True,
    ) -> ExecutionResult:
        """以单次持久化批量写入数据行；大批量导入可交给唯一索引校验。"""

        self.session.authorize("INSERT", table_name)

        def operation() -> ExecutionResult:
            """封装一次操作并返回执行结果。"""
            table = self.catalog.get_table(table_name)
            heap = self._heap(table)
            indexes = [
                metadata
                for metadata in self.catalog.indexes()
                if metadata.table_id == table.table_id
            ]
            # HOW：只有“唯一约束都有单列唯一索引兑底”时才批量写页（索引插入仍逐行执行并负责兑底校验）。
            # WHY：无索引的主键/唯一列靠全表扫描校验，批量写页会让他们看不到同批的行，因此保留逐行路径。
            indexed_unique = {
                metadata.columns[0].lower()
                for metadata in indexes
                if metadata.unique and len(metadata.columns) == 1
            }
            # HOW：无索引的主键/唯一列用“装载期集合”代替逐行全表扫描。
            # WHY：原实现每行都 `heap.scan()` 校验，显式库这类“有主键、未单独建索引”的表是 O(n²)。
            unique_positions = [
                index
                for index, column in enumerate(table.schema)
                if (column.primary_key or column.unique)
                and column.name.lower() not in indexed_unique
            ]
            unique_seen: dict[int, set[object]] = {}
            if unique_positions:
                existing_rows = [record.row for record in heap.scan()]
                for position in unique_positions:
                    unique_seen[position] = {
                        row[position]
                        for row in existing_rows
                        if row[position] is not None
                    }
            # HOW：目标是空表的整套索引可以延迟到装载结束再 bulk_load，
            # 避开“每行一次叶子重写”（显式库 13.7 万行 × 11 索引时这是主要成本）。
            fresh_indexes = [
                metadata
                for metadata in indexes
                if not self.index_manager.get(metadata.name).has_entries()
            ]
            deferred = bool(indexes) and len(fresh_indexes) == len(indexes)
            pending_entries: dict[str, list[IndexPayloadEntry]] = {
                metadata.name: [] for metadata in fresh_indexes
            }
            written_row_ids: list[RowId] = []
            inserted = 0
            buffer: list[tuple[object, ...]] = []
            for values in rows:
                row = table.schema.validate_row(tuple(values))
                if validate_constraints and not unique_seen:
                    # HOW：无索引唯一列已由装载期集合代替，这里只跑有索引支撑的唯一性校验。
                    self._check_constraints(table, row, None)
                for position in unique_positions:
                    value = row[position]
                    if value is None:
                        continue
                    if value in unique_seen[position]:
                        raise _with_location(
                            ExecutionError(
                                f"列 {table.schema.columns[position].name} 的唯一约束冲突"
                            ),
                            None,
                        )
                    unique_seen[position].add(value)
                buffer.append(row)
                if len(buffer) >= _INSERT_BATCH_ROWS:
                    inserted += self._flush_batch(
                        table, heap, buffer, pending_entries, written_row_ids
                    )
                    buffer.clear()
            if buffer:
                inserted += self._flush_batch(
                    table, heap, buffer, pending_entries, written_row_ids
                )
            if deferred:
                self._bulk_build_indexes(
                    table, heap, fresh_indexes, pending_entries, written_row_ids
                )
            table.page_ids = [PageId(page_id) for page_id in heap.page_ids]
            table.first_page_id = table.page_ids[0] if table.page_ids else None
            table.row_count += inserted
            return ExecutionResult(affected_rows=inserted, message=f"INSERT {inserted}")

        result = self._mutate(operation)
        self.audit.record(
            "INSERT",
            user=self.session.user.name,
            details={"object": table_name, "affected_rows": result.affected_rows},
        )
        return result

    # HOW: SQL 总入口：Token -> AST -> 绑定 -> 计划 -> 权限与优化 -> 逐条执行。
    def execute_script(self, sql: str) -> list[ExecutionResult]:
        # 词法分析
        # 语法分析
        # 存储每条语句的结果
        # 如果只有一条语句，缓存SQL
        # 遍历每条语句
        # 绑定表名和列名
        """执行 SQL 脚本并返回各语句结果。"""
        tokens = tuple(tokenize(sql))
        statements = Parser(tokens).parse_script()
        results: list[ExecutionResult] = []
        cache_sql = sql if len(statements) == 1 else None
        for statement in statements:
            bound = Binder(self.catalog).bind(statement)
            compilation = CompilationResult(
                tokens, statement, bound, plan_from_statement(statement)#生成计划
            )
            results.append(self._execute_compilation(compilation, sql=cache_sql))#检查权限，优化并进入执行
        return results

    def check_script(self, sql: str) -> ParseOutcome:
        """只做词法与语法检查，把脚本里的错误一次报全（不绑定、不执行、不改目录）。

        WHY：``execute_script`` 是首错即抛（生产路径要快速失败），但"改一个错跑一次"
        对准备验收脚本、批量导入 SQL 极其低效。这里给出一条把错误收全的只读通道。
        """

        return parse_recovering(sql)

    def compile(self, sql: str) -> CompilationResult:
        """使用当前目录编译一条 SQL，不执行也不修改目录。"""

        compilation = self.compiler.compile(sql, self.catalog)
        cache_sql = (
            sql if isinstance(compilation.statement, (Select, Explain)) else None
        )
        return replace(
            compilation,
            optimized_plan=self.optimize_plan(compilation.plan, sql=cache_sql),
        )

    def optimize_plan(
        self, plan: LogicalPlanNode, *, sql: str | None = None
    ) -> PhysicalPlanNode:
        """结合当前目录把逻辑计划改写为实际访问路径。

        首先按索引元数据选择候选路径；对大表再用索引候选行数做一次轻量
        选择性校正，避免 ``SELECT *`` 命中大量行时付出高昂的回表成本。
        """

        table_names = {table.table_id: table.name for table in self.catalog.tables()}
        index_columns: dict[str, set[str]] = {}
        for metadata in self.catalog.indexes():
            table_name = table_names.get(metadata.table_id)
            # HOW：联合索引只能从最左前缀开始定位；只把首列交给优化器。
            if table_name is not None and metadata.columns:
                index_columns.setdefault(table_name.lower(), set()).add(
                    metadata.columns[0]
                )
        optimized = self.optimizer.optimize(plan, sql=sql, index_columns=index_columns)
        if not isinstance(optimized, PlanNode):
            raise ExecutionError("优化器返回了无效的计划")
        adjusted = self._adjust_index_selectivity(optimized, optimized.statement)
        if sql is not None:
            # HOW：把校正后的计划重新放回缓存，避免每次执行都重复探测索引候选集。
            self.optimizer.cache.put(sql, adjusted)
        return adjusted

    def estimate_plan(self, plan: PhysicalPlanNode) -> CostEstimate:
        """返回基于当前表统计信息的优化器计划估算，不代表真实毫秒数。"""

        return self.optimizer.estimate_plan(plan)

    def _adjust_index_selectivity(
        self, plan: PlanNode, statement: Statement | None
    ) -> PlanNode:
        """用实际索引候选数修正大表的 IndexScan 选择。"""

        if isinstance(statement, Explain):
            statement = statement.statement
        if (
            not isinstance(statement, Select)
            or statement.from_table is None
            or statement.joins
        ):
            return plan
        relation = self.catalog.find_table(
            statement.from_table.name, include_system=True
        )
        if not isinstance(relation, TableMetadata):
            return plan

        def rewrite(node: PlanNode) -> PlanNode:
            """重写输入结构以应用当前规则。"""
            node_table = node.properties.get("table")
            if (
                node.kind == "IndexScan"
                and isinstance(node_table, str)
                and node_table.lower() == relation.name.lower()
            ):
                candidates = self._candidate_row_ids(
                    relation, statement.from_table, statement.where
                )
                if candidates is not None and not self.optimizer.should_use_index(
                    relation.name, len(candidates)
                ):
                    properties = {
                        key: value
                        for key, value in node.properties.items()
                        if key != "index_column"
                    }
                    properties["scan_reason"] = "索引选择性过低，改用顺序扫描"
                    properties["candidate_rows"] = len(candidates)
                    return replace(node, kind="SeqScan", properties=properties)
                return node
            children = tuple(rewrite(child) for child in node.children)
            return (
                node if children == node.children else replace(node, children=children)
            )

        return rewrite(plan)

    # HOW: 执行协调入口；命令分发在 commands.py，SELECT 编排在 query.py。
    def _execute_compilation(#检查权限，优化并进入执行
        self,
        compilation: CompilationResult,
        *,
        sql: str | None = None,
        optimized_plan: PhysicalPlanNode | None = None,
    ) -> ExecutionResult:
        """执行已完成编译和绑定的 SQL 语句。"""
        statement = compilation.statement
        action = self._action_for(statement)
        object_name = self._object_for(statement)
        try:
            # HOW: 检查会话的操作权限，再选择优化计划携带的语句交给执行层。
            self._authorize_statement(statement, action)#先检查当前用户是否有权限执行
            cache_sql = sql if isinstance(statement, (Select, Explain)) else None
            active_plan = (
                optimized_plan
                if optimized_plan is not None
                else self.optimize_plan(compilation.plan, sql=cache_sql)
            )#生成优化计划
            # HOW：表达式折叠和谓词下推后的 AST 挂在优化计划根节点上；执行时
            # 使用这份 AST，避免优化结果只停留在 EXPLAIN 展示层。
            active_statement = (
                active_plan.statement
                if isinstance(active_plan.statement, Statement)
                else statement
            )
            result = self._execute_with_transaction(
                active_statement, compilation.bound, active_plan
            )#执行语句
            self.audit.record(
                action,
                user=self.session.user.name,
                details={
                    "object": object_name or "",
                    "affected_rows": result.affected_rows,
                },
            )
            return result
        except Exception as exc:
            self.audit.record(
                action,
                user=self.session.user.name,
                success=False,
                details={"object": object_name or "", "error": str(exc)},
            )
            raise

    def _execute_with_transaction(
        self, statement: Statement, bound, plan: PlanNode
    ) -> ExecutionResult:
        """在事务范围内执行一条数据语句。

        HOW：分三种情况——
        1. 事务控制语句直接分发；
        2. 自动提交下的只读语句不开事务（开事务会白写 BEGIN 日志、还会误清计划缓存），
           只取一次临时读锁保护本次读取；
        3. 其余语句一律跑在事务里：显式事务存在时并入它（锁保持到 COMMIT/ROLLBACK），
           否则开隐式事务，成功即提交、失败即回滚，等价于自动提交。
        """

        if isinstance(
            statement, (BeginTransaction, Commit, Rollback, SetTransaction)
        ):
            return self._execute_statement(statement, bound, plan)

        explicit = self.current_transaction()
        if explicit is None and self._is_read_only(statement):
            return self._execute_read_only(statement, bound, plan)
        if explicit is not None and explicit.failed:
            raise TransactionError(
                f"事务 {explicit.txn_id} 中的语句已经失败，只能执行 ROLLBACK",
                txn_id=explicit.txn_id,
            )
        implicit = explicit is None
        txn = explicit or self.begin_transaction(
            # WHY：DML 也会修改目录中的 row_count、页链和索引元数据；自动提交失败时
            # 必须与页前像一起恢复，否则优化器会看到比实际数据更大的统计信息。
            snapshot_catalog=True,
            implicit=True,
        )
        try:
            self._acquire_locks(txn, statement)
            result = self._execute_statement(statement, bound, plan)
        except ConcurrencyError:
            # 死锁牺牲者或锁等待超时：事务已不可用，隐式/显式都必须整体回滚。
            self._rollback_now(txn)
            raise
        except Exception:
            if implicit:
                self._rollback_now(txn)
            else:
                # HOW：与 PostgreSQL 一致——显式事务里语句出错后事务进入失败态，
                # 锁继续持有，用户只能 ROLLBACK；避免"半条语句"被后续语句读走。
                txn.state = TransactionState.FAILED
            raise
        if implicit:
            self.commit_transaction()
        elif txn.isolation == "read_committed":
            # 读已提交：语句结束就放掉 S 锁，X 锁仍保持到事务结束。
            self.lock_manager.release_shared(txn.txn_id)
        txn.touch()
        return result

    def _execute_read_only(self, statement: Statement, bound, plan: PlanNode):
        """自动提交的只读语句：不加事务，只用临时锁作用域保护本次读取。

        HOW：锁归属用负数编号，与真实事务号（正整数）区分开，
        既不占用事务号也不进入事务计数器。
        """

        scope = -next(self._read_scope_seq)
        reads, _writes = self._statement_resources(statement)
        for name in sorted(reads):
            self.lock_manager.acquire(scope, name, LockMode.SHARED)
        try:
            return self._execute_statement(statement, bound, plan)
        finally:
            self.lock_manager.release_all(scope)

    def _rollback_now(self, txn: Transaction) -> None:
        """立即撤销并结束事务（不再抛错，供异常路径调用）。"""

        if not txn.active:
            return
        self._undo_transaction(txn)
        self._release_transaction(
            txn, TransactionState.ABORTED, dirty=self._is_dirty(txn)
        )

    @staticmethod
    def _is_dirty(txn: Transaction) -> bool:
        """事务是否真的改过东西（决定要不要刷新统计、清理缓存）。"""

        return bool(txn.page_images) or txn.catalog_snapshot is not None

    # ----- 计划估算、运行状态与资源释放 -----
    def health(self) -> dict[str, object]:
        """返回当前组件的健康状态。"""
        return {
            "status": "ok" if not self._closed else "closed",
            "path": str(self.path),
            "page_count": self.disk.page_count,
            "tables": len(self.catalog),
        }

    def metrics(self) -> dict[str, object]:
        """返回当前运行指标。"""
        return {
            "health": self.health(),
            "buffer_pool": self.buffer_pool.stats().to_dict(),
            "catalog": {
                "tables": len(self.catalog),
                "indexes": len(self.catalog.indexes()),
            },
            "plan_cache": len(self.optimizer.cache),
            "transactions": self.transaction_state(),
        }

    def resize_buffer_pool(self, capacity: int) -> int:
        """在线调整缓存页数并同步优化器的缓存成本参数。"""

        with self._lock:
            previous_capacity = self.buffer_pool.capacity
            evicted = self.buffer_pool.resize(capacity)
            self.config = replace(self.config, buffer_pool_size=capacity)
            # WHY：优化器用缓存页数估算随机回表成本；只调整 BufferPool 会让计划估算继续使用旧容量。
            self.optimizer.buffer_pool_pages = capacity
            if previous_capacity != capacity:
                # WHY：已有物理计划可能按旧缓存容量选择了 IndexScan/SeqScan，热更新后必须重新规划。
                self.optimizer.cache.invalidate()
            return evicted

    # HOW: 正常关闭保存权限状态并关闭缓存和文件；不等于具备断电恢复能力。
    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        with self._lock:
            if self._closed:
                return
            # HOW：关闭时还挂着的活动事务一律回滚，避免把"半成品"留在磁盘上。
            txn = self.current_transaction()
            if txn is not None:
                self._rollback_now(txn)
            self._persist_rbac()
            self.buffer_pool.close()
            if self.txn_manager.active_count == 0:
                # 干净关闭：所有改动都已落盘，日志可以整段截断。
                self.wal.checkpoint()
            self.wal.close()
            self.disk.close()
            self._closed = True

    def __enter__(self) -> "Database":
        """进入上下文管理器并返回当前对象。"""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文管理器并完成资源清理。"""
        self.close()
