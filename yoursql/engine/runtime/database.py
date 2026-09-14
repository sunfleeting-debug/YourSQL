"""Database 运行时协调器：编译、优化、执行和页式持久化。"""

from __future__ import annotations

import json
import os
import struct
import tempfile
from dataclasses import replace
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Iterable

from ...common import (
    CatalogError,
    DatabaseConfig,
    ExecutionResult,
    ExecutionError,
    SqlValue,
    YourSQLError,
    StorageError,
)
from ...common.types import PageId, RowId
from ...sql.ast import Explain, Select, Statement
from ...sql.binder import Binder
from ...sql.compiler import CompilationResult, Compiler
from ...sql.lexer import tokenize
from ...sql.diagnostics import ParseOutcome
from ...sql.parser import Parser, parse_recovering
from ...planner.logical import LogicalPlanNode, plan_from_statement
from ...planner.optimizer import CostEstimate, Optimizer, StatisticsStore
from ...planner.physical import PhysicalPlanNode, PlanNode
from ...execution.evaluator import ExpressionEvaluator
from ...execution.query import QueryExecutionMixin, _RowContextTemplate
from ...storage import BufferPool, DiskManager, IndexManager, Page, PageType, TableHeap
from ...storage.page import HEADER_SIZE
from ..security.audit import AuditLog
from ..security.auth import RBAC
from ..catalog import Catalog, IndexMetadata, TableMetadata
from ..security.session import Session
from ..system_catalog import SystemCatalog
from .commands import DatabaseCommandMixin


# HOW：批量导入按块处理，既让页写入成批（减少整页重编码），又不把全部行都堆在内存里。
_INSERT_BATCH_ROWS = 4096

_CATALOG_CHAIN_MAGIC = b"MCAT2"
_CATALOG_CHAIN_HEADER = struct.Struct("<5sQ")


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
                payload = (
                    json.loads(page.payload.decode("utf-8")) if page.payload else {}
                )
                stored_size = int(payload.get("page_size", 0))
                if stored_size >= HEADER_SIZE and stored_size & (stored_size - 1) == 0:
                    return stored_size
            except (OSError, StorageError, UnicodeDecodeError, ValueError, TypeError):
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
        self.disk = DiskManager(self.path, page_size=self.config.page_size)
        self.buffer_pool = BufferPool(
            self.disk, self.config.buffer_pool_size, self.config.replacement_policy
        )
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
        self.audit = AuditLog(audit_path)
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
    def _load_catalog(self) -> Catalog:
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
            next_page_id, chunk = self._decode_catalog_page(page.payload)
            chunks.append(chunk)
            if next_page_id is None:
                break
            current_page_id = next_page_id or None
        try:
            data = json.loads(b"".join(chunks).decode("utf-8")) if chunks else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise CatalogError("catalog 页链 JSON 损坏") from exc
        if not isinstance(data, dict):
            raise CatalogError("catalog 页不是对象")
        return Catalog.from_dict(data)

    @staticmethod
    def _catalog_payload(catalog: Catalog) -> bytes:
        return json.dumps(
            catalog.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

    @staticmethod
    def _catalog_page_payload(chunk: bytes, next_page_id: int) -> bytes:
        """为 Catalog 分片添加链指针；旧版未带前缀的单页仍可读取。"""

        return (
            _CATALOG_CHAIN_HEADER.pack(_CATALOG_CHAIN_MAGIC, int(next_page_id)) + chunk
        )

    @staticmethod
    def _decode_catalog_page(payload: bytes) -> tuple[int | None, bytes]:
        """读取链式 Catalog 页，兼容旧版裸 JSON 页。"""

        if not payload.startswith(_CATALOG_CHAIN_MAGIC):
            return None, payload
        if len(payload) < _CATALOG_CHAIN_HEADER.size:
            raise CatalogError("catalog 页链头部不完整")
        magic, next_page_id = _CATALOG_CHAIN_HEADER.unpack(
            payload[: _CATALOG_CHAIN_HEADER.size]
        )
        if magic != _CATALOG_CHAIN_MAGIC:
            raise CatalogError("catalog 页链魔数错误")
        return int(next_page_id), payload[_CATALOG_CHAIN_HEADER.size :]

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
            next_page_id, _chunk = self._decode_catalog_page(page.payload)
            current_page_id = None if next_page_id in {None, 0} else next_page_id
        return page_ids

    def _persist_catalog(self) -> None:
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
        metadata_changed = False
        for metadata in self.catalog.indexes():

            def update_root(page_id: int, metadata: IndexMetadata = metadata) -> None:
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
        for table in self.catalog.tables():
            self.optimizer.statistics.update(table.name, table.stats)
        # WHY：计划同时依赖行数与索引元数据；写入或 DDL 后不能继续复用旧访问路径。
        self.optimizer.cache.invalidate()

    def _heap(self, table: TableMetadata) -> TableHeap:
        key = int(table.table_id)
        heap = self._heaps.get(key)
        if heap is None:
            heap = TableHeap(
                self.buffer_pool, [int(page_id) for page_id in table.page_ids]
            )
            self._heaps[key] = heap
        return heap

    # ----- 对外 SQL 管线与批量写入入口 -----
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
                existing_rows = [row for _row_id, row in heap.scan()]
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
            pending_entries: dict[
                str, list[tuple[object, RowId, tuple[object, ...]]]
            ] = {metadata.name: [] for metadata in fresh_indexes}
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

    def execute_script(self, sql: str) -> list[ExecutionResult]:
        tokens = tuple(tokenize(sql))
        statements = Parser(tokens).parse_script()
        results: list[ExecutionResult] = []
        cache_sql = sql if len(statements) == 1 else None
        for statement in statements:
            bound = Binder(self.catalog).bind(statement)
            compilation = CompilationResult(
                tokens, statement, bound, plan_from_statement(statement)
            )
            results.append(self._execute_compilation(compilation, sql=cache_sql))
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

    def _execute_compilation(
        self,
        compilation: CompilationResult,
        *,
        sql: str | None = None,
        optimized_plan: PhysicalPlanNode | None = None,
    ) -> ExecutionResult:
        statement = compilation.statement
        action = self._action_for(statement)
        object_name = self._object_for(statement)
        try:
            self._authorize_statement(statement, action)
            cache_sql = sql if isinstance(statement, (Select, Explain)) else None
            active_plan = (
                optimized_plan
                if optimized_plan is not None
                else self.optimize_plan(compilation.plan, sql=cache_sql)
            )
            # HOW：表达式折叠和谓词下推后的 AST 挂在优化计划根节点上；执行时
            # 使用这份 AST，避免优化结果只停留在 EXPLAIN 展示层。
            active_statement = (
                active_plan.statement
                if isinstance(active_plan.statement, Statement)
                else statement
            )
            result = self._execute_statement(
                active_statement, compilation.bound, active_plan
            )
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

    # ----- 计划估算、运行状态与资源释放 -----
    def health(self) -> dict[str, object]:
        return {
            "status": "ok" if not self._closed else "closed",
            "path": str(self.path),
            "page_count": self.disk.page_count,
            "tables": len(self.catalog),
        }

    def metrics(self) -> dict[str, object]:
        return {
            "health": self.health(),
            "buffer_pool": self.buffer_pool.stats(),
            "catalog": {
                "tables": len(self.catalog),
                "indexes": len(self.catalog.indexes()),
            },
            "plan_cache": len(self.optimizer.cache),
        }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._persist_rbac()
            self.buffer_pool.close()
            self.disk.close()
            self._closed = True

    def __enter__(self) -> "Database":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
