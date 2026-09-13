"""YourSQL 数据库入口：编译、绑定、执行和页式持久化。"""

from __future__ import annotations

import json
import math
import os
import re
import struct
import tempfile
from dataclasses import dataclass, fields, replace
from datetime import date, timedelta
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterable

from ..common import (
    AuthorizationError,
    BinderError,
    CatalogError,
    Column,
    DataType,
    DatabaseConfig,
    ExecutionResult,
    ExecutionError,
    YourSQLError,
    Schema,
    StorageError,
    TableStats,
    Value,
    compare_values,
    sql_truth,
)
from ..common.types import PageId, RowId
from ..sql.ast import (
    BetweenPredicate,
    BinaryOp,
    ColumnDefinition,
    ColumnRef,
    CreateRole,
    CreateIndex,
    CreateTable,
    CreateView,
    CreateUser,
    Delete,
    DropIndex,
    DropTable,
    DropView,
    Explain,
    Expr,
    FunctionCall,
    Grant,
    InPredicate,
    Insert,
    IsNull,
    Literal,
    Node,
    Parameter,
    Revoke,
    Select,
    SelectItem,
    Show,
    ShowGrants,
    Star,
    Statement,
    Subquery,
    TableRef,
    UnaryOp,
    Update,
)
from ..sql.binder import Binder, BoundStatement
from ..sql.compiler import CompilationResult, Compiler
from ..sql.lexer import KEYWORDS, tokenize
from ..sql.parser import Parser
from ..sql.plan import PlanNode, plan_from_statement
from ..storage import BufferPool, DiskManager, IndexManager, Page, PageType, TableHeap
from ..storage.page import HEADER_SIZE
from .audit import AuditLog
from .auth import RBAC
from .catalog import Catalog, IndexMetadata, TableMetadata, ViewMetadata
from .optimizer import CostEstimate, Optimizer, StatisticsStore
from .session import Session
from .system_catalog import SystemCatalog
from ..common.trace import current_trace


_MISSING = object()
_AMBIGUOUS = object()
_AGGREGATE_NAMES = {"count", "sum", "avg", "min", "max"}
_CATALOG_CHAIN_MAGIC = b"MCAT2"
_CATALOG_CHAIN_HEADER = struct.Struct("<5sQ")


@dataclass
class _IndexConstraint:
    """单列索引条件的交集；用于按联合索引前缀生成扫描边界。"""

    allowed: list[object] | None = None
    lower: tuple[object, bool] | None = None
    upper: tuple[object, bool] | None = None
    not_null: bool = False


def _with_location(error: YourSQLError, location: tuple[int, int] | None) -> YourSQLError:
    """把 SQL 内部异常补到源码位置；外部/存储异常保持原有错误信息。"""

    if location is None or error.line is not None:
        return error
    if type(error) is YourSQLError:
        return YourSQLError(error.message, error.code, location[0], location[1], dict(error.details))
    return type(error)(error.message, line=location[0], column=location[1], **error.details)


def _with_node_location(error: YourSQLError, node: Node | None) -> YourSQLError:
    """把 SQL 内部异常补到 AST 节点；外部/存储异常保持原有错误信息。"""

    return _with_location(error, node.source_location if node is not None else None)


def _eval_date_function(values: list[object], source: Node | None = None) -> str | None:
    """执行 TPC-H/SQLite 风格的 DATE 文本函数。"""

    if not values or values[0] is None:
        return None
    try:
        current = date.fromisoformat(str(values[0]))
    except ValueError as exc:
        raise _with_node_location(ExecutionError(f"DATE 参数必须是 YYYY-MM-DD: {values[0]!r}"), source) from exc
    for modifier in values[1:]:
        if modifier is None:
            return None
        match = re.fullmatch(r"([+-])(\d+)\s+(day|days|month|months|year|years)", str(modifier).strip().lower())
        if match is None:
            raise _with_node_location(ExecutionError(f"不支持 DATE 修饰符 {modifier!r}"), source)
        sign = 1 if match.group(1) == "+" else -1
        amount = sign * int(match.group(2))
        unit = match.group(3)
        if unit.startswith("day"):
            current += timedelta(days=amount)
            continue
        if unit.startswith("month"):
            month_index = current.year * 12 + current.month - 1 + amount
            year, month_index = divmod(month_index, 12)
            month = month_index + 1
            day = min(current.day, _days_in_month(year, month))
            current = date(year, month, day)
            continue
        year = current.year + amount
        day = min(current.day, _days_in_month(year, current.month))
        current = date(year, current.month, day)
    return current.isoformat()


def _days_in_month(year: int, month: int) -> int:
    """返回指定月份的天数，用于日期修饰符的边界归一化。"""

    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - date(year, month, 1)).days


def _sql_identifier(name: str) -> str:
    """把目录标识符还原为可再次解析的 SQL 标识符。"""

    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name) and name.upper() not in KEYWORDS:
        return name
    return "`" + name.replace("`", "``") + "`"


def _sql_literal(value: object) -> str:
    """把默认值转换成 SHOW CREATE TABLE 可用的 SQL 字面量。"""

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


class Database:
    """一个数据库文件对应一个实例，支持 Python API 和 SQL 脚本执行。"""

    @staticmethod
    def detect_page_size(path: str | os.PathLike[str]) -> int | None:
        """读取已有数据库 superblock 中的页大小，供跨配置打开和切库使用。"""

        candidate = Path(path)
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            return None
        # 逐步尝试常见页大小；页头负载通常很小，即使候选大小小于实际页也能解析出 superblock。
        for probe_size in (512, 1024, 2048, 4096, 8192, 16 * 1024, 32 * 1024, 64 * 1024, 128 * 1024):
            try:
                with candidate.open("rb") as stream:
                    raw = stream.read(probe_size)
                if len(raw) != probe_size:
                    continue
                page = Page.from_bytes(raw, page_size=probe_size)
                if page.page_type is not PageType.SUPERBLOCK:
                    continue
                payload = json.loads(page.payload.decode("utf-8")) if page.payload else {}
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
    ) -> None:
        selected_page_size = None
        if config is None and path is not None and str(path) != ":memory:":
            selected_page_size = self.detect_page_size(path)
        self.config = config or DatabaseConfig(page_size=selected_page_size or 4096)
        self._temporary_path: Path | None = None
        if path is None or str(path) == ":memory:":
            handle = tempfile.NamedTemporaryFile(prefix="yoursql-", suffix=".db", delete=False)
            handle.close()
            self._temporary_path = Path(handle.name)
            self.path = self._temporary_path
        else:
            self.path = Path(path)
            # HOW：默认数据库位于 ./data；首次启动时自动准备父目录，CLI、Web 和直接 API 行为保持一致。
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.disk = DiskManager(self.path, page_size=self.config.page_size)
        self.buffer_pool = BufferPool(self.disk, self.config.buffer_pool_size, self.config.replacement_policy)
        self.catalog = self._load_catalog()
        self.index_manager = IndexManager()
        self._heaps: dict[int, TableHeap] = {}
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
        self.optimizer = Optimizer(StatisticsStore())
        self.compiler = Compiler()
        self._rebuild_indexes()
        self._refresh_statistics()
        self._closed = False

    def _load_catalog(self) -> Catalog:
        page_id = self.disk.named_page("catalog")
        if page_id is None:
            catalog = Catalog()
            page = self.disk.allocate(PageType.CATALOG, self._catalog_page_payload(self._catalog_payload(catalog), 0))
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
        return json.dumps(catalog.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @staticmethod
    def _catalog_page_payload(chunk: bytes, next_page_id: int) -> bytes:
        """为 Catalog 分片添加链指针；旧版未带前缀的单页仍可读取。"""

        return _CATALOG_CHAIN_HEADER.pack(_CATALOG_CHAIN_MAGIC, int(next_page_id)) + chunk

    @staticmethod
    def _decode_catalog_page(payload: bytes) -> tuple[int | None, bytes]:
        """读取链式 Catalog 页，兼容旧版裸 JSON 页。"""

        if not payload.startswith(_CATALOG_CHAIN_MAGIC):
            return None, payload
        if len(payload) < _CATALOG_CHAIN_HEADER.size:
            raise CatalogError("catalog 页链头部不完整")
        magic, next_page_id = _CATALOG_CHAIN_HEADER.unpack(payload[:_CATALOG_CHAIN_HEADER.size])
        if magic != _CATALOG_CHAIN_MAGIC:
            raise CatalogError("catalog 页链魔数错误")
        return int(next_page_id), payload[_CATALOG_CHAIN_HEADER.size:]

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
            page = self.buffer_pool.peek_page(current_page_id) if current_page_id in self.buffer_pool else self.disk.read(current_page_id)
            if page.page_type is not PageType.CATALOG:
                raise CatalogError("catalog 页链包含非 CATALOG 页")
            next_page_id, _chunk = self._decode_catalog_page(page.payload)
            current_page_id = None if next_page_id in {None, 0} else next_page_id
        return page_ids

    def _persist_catalog(self) -> None:
        payload = self._catalog_payload(self.catalog)
        chunk_capacity = self.config.page_size - Page.HEADER_SIZE - _CATALOG_CHAIN_HEADER.size
        if chunk_capacity <= 0:
            raise CatalogError("页大小不足以容纳 catalog 链头")
        chunks = [payload[offset:offset + chunk_capacity] for offset in range(0, len(payload), chunk_capacity)] or [b""]

        first_page_id = self.disk.named_page("catalog")
        existing_page_ids = self._catalog_page_ids(first_page_id) if first_page_id is not None else []
        page_ids = list(existing_page_ids[:len(chunks)])
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
        for stale_page_id in existing_page_ids[len(chunks):]:
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
            if metadata.root_page_id != (None if tree.root_page_id is None else PageId(tree.root_page_id)):
                metadata.root_page_id = None if tree.root_page_id is None else PageId(tree.root_page_id)
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
            heap = TableHeap(self.buffer_pool, [int(page_id) for page_id in table.page_ids])
            self._heaps[key] = heap
        return heap

    def execute(self, sql: str) -> ExecutionResult:
        """执行单条或脚本 SQL；脚本返回最后一条语句的结果。"""

        results = self.execute_script(sql)
        return results[-1] if results else ExecutionResult(message="没有可执行的 SQL")

    def insert_rows(
        self,
        table_name: str,
        rows: Iterable[Iterable[Any]],
        *,
        validate_constraints: bool = True,
    ) -> ExecutionResult:
        """以单次持久化批量写入数据行；大批量导入可交给唯一索引校验。"""

        self.session.authorize("INSERT", table_name)

        def operation() -> ExecutionResult:
            table = self.catalog.get_table(table_name)
            heap = self._heap(table)
            inserted = 0
            for values in rows:
                row = table.schema.validate_row(tuple(values))
                if validate_constraints:
                    self._check_constraints(table, row, None)
                row_id = heap.insert(row)
                self._update_indexes(table, row, row_id, insert=True)
                table.page_ids = [PageId(page_id) for page_id in heap.page_ids]
                table.first_page_id = table.page_ids[0] if table.page_ids else None
                table.row_count += 1
                inserted += 1
            return ExecutionResult(affected_rows=inserted, message=f"INSERT {inserted}")

        result = self._mutate(operation)
        self.audit.record("INSERT", user=self.session.user.name, details={"object": table_name, "affected_rows": result.affected_rows})
        return result

    def execute_script(self, sql: str) -> list[ExecutionResult]:
        tokens = tuple(tokenize(sql))
        statements = Parser(tokens).parse_script()
        results: list[ExecutionResult] = []
        cache_sql = sql if len(statements) == 1 else None
        for statement in statements:
            bound = Binder(self.catalog).bind(statement)
            compilation = CompilationResult(tokens, statement, bound, plan_from_statement(statement))
            results.append(self._execute_compilation(compilation, sql=cache_sql))
        return results

    def compile(self, sql: str) -> CompilationResult:
        """使用当前目录编译一条 SQL，不执行也不修改目录。"""

        compilation = self.compiler.compile(sql, self.catalog)
        cache_sql = sql if isinstance(compilation.statement, (Select, Explain)) else None
        return replace(compilation, optimized_plan=self.optimize_plan(compilation.plan, sql=cache_sql))

    def optimize_plan(self, plan: PlanNode, *, sql: str | None = None) -> PlanNode:
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
                index_columns.setdefault(table_name.lower(), set()).add(metadata.columns[0])
        optimized = self.optimizer.optimize(plan, sql=sql, index_columns=index_columns)
        if not isinstance(optimized, PlanNode):
            raise ExecutionError("优化器返回了无效的计划")
        adjusted = self._adjust_index_selectivity(optimized, optimized.statement)
        if sql is not None:
            # HOW：把校正后的计划重新放回缓存，避免每次执行都重复探测索引候选集。
            self.optimizer.cache.put(sql, adjusted)
        return adjusted

    def estimate_plan(self, plan: PlanNode) -> CostEstimate:
        """返回基于当前表统计信息的优化器计划估算，不代表真实毫秒数。"""

        return self.optimizer.estimate_plan(plan)

    def _adjust_index_selectivity(self, plan: PlanNode, statement: Statement | None) -> PlanNode:
        """用实际索引候选数修正大表的 IndexScan 选择。"""

        if isinstance(statement, Explain):
            statement = statement.statement
        if not isinstance(statement, Select) or statement.from_table is None or statement.joins:
            return plan
        relation = self.catalog.find_table(statement.from_table.name, include_system=True)
        if not isinstance(relation, TableMetadata):
            return plan

        def rewrite(node: PlanNode) -> PlanNode:
            node_table = node.properties.get("table")
            if node.kind == "IndexScan" and isinstance(node_table, str) and node_table.lower() == relation.name.lower():
                candidates = self._candidate_row_ids(relation, statement.from_table, statement.where)
                if candidates is not None and not self.optimizer.should_use_index(relation.name, len(candidates)):
                    properties = {key: value for key, value in node.properties.items() if key != "index_column"}
                    properties["scan_reason"] = "索引选择性过低，改用顺序扫描"
                    properties["candidate_rows"] = len(candidates)
                    return replace(node, kind="SeqScan", properties=properties)
                return node
            children = tuple(rewrite(child) for child in node.children)
            return node if children == node.children else replace(node, children=children)

        return rewrite(plan)

    def _execute_compilation(
        self,
        compilation: CompilationResult,
        *,
        sql: str | None = None,
        optimized_plan: PlanNode | None = None,
    ) -> ExecutionResult:
        statement = compilation.statement
        action = self._action_for(statement)
        object_name = self._object_for(statement)
        try:
            self._authorize_statement(statement, action)
            cache_sql = sql if isinstance(statement, (Select, Explain)) else None
            active_plan = optimized_plan if optimized_plan is not None else self.optimize_plan(compilation.plan, sql=cache_sql)
            # HOW：表达式折叠和谓词下推后的 AST 挂在优化计划根节点上；执行时
            # 使用这份 AST，避免优化结果只停留在 EXPLAIN 展示层。
            active_statement = active_plan.statement if isinstance(active_plan.statement, Statement) else statement
            result = self._execute_statement(active_statement, compilation.bound, active_plan)
            self.audit.record(action, user=self.session.user.name, details={"object": object_name or "", "affected_rows": result.affected_rows})
            return result
        except Exception as exc:
            self.audit.record(action, user=self.session.user.name, success=False, details={"object": object_name or "", "error": str(exc)})
            raise

    @staticmethod
    def _action_for(statement: Statement) -> str:
        if isinstance(statement, (CreateRole, CreateUser, Grant, Revoke)):
            return "SECURITY"
        if isinstance(statement, ShowGrants):
            return "SHOW_GRANTS"
        if isinstance(statement, (Select, Explain, Show)):
            return "SELECT"
        if isinstance(statement, (CreateTable, CreateView, CreateIndex)):
            return "CREATE"
        if isinstance(statement, (DropTable, DropView, DropIndex)):
            return "DROP"
        if isinstance(statement, Insert):
            return "INSERT"
        if isinstance(statement, Update):
            return "UPDATE"
        if isinstance(statement, Delete):
            return "DELETE"
        return type(statement).__name__.upper()

    @staticmethod
    def _object_for(statement: Statement) -> str | None:
        if isinstance(statement, Show):
            return statement.object_name
        if isinstance(statement, (Grant, Revoke)):
            return f"{statement.target_kind} {statement.target_name}"
        if isinstance(statement, ShowGrants):
            if statement.target_kind and statement.target_name:
                return f"{statement.target_kind} {statement.target_name}"
            return None
        for attribute in ("table", "name"):
            value = getattr(statement, attribute, None)
            if isinstance(value, str):
                return value
        if isinstance(statement, Explain):
            return Database._object_for(statement.statement)
        if isinstance(statement, Select):
            names: list[str] = []
            if statement.from_table is not None:
                names.append(statement.from_table.name)
            names.extend(join.table.name for join in statement.joins)
            if statement.union is not None:
                union_name = Database._object_for(statement.union)
                if union_name:
                    names.append(union_name)
            return ",".join(names) or None
        return None

    def _authorize_statement(self, statement: Statement, action: str, view_stack: tuple[str, ...] = ()) -> None:
        """按语句涉及的对象逐个检查权限，避免多表查询绕过对象级授权。"""

        if isinstance(statement, (CreateRole, CreateUser, Grant, Revoke)):
            try:
                self.session.authorize("SECURITY")
            except AuthorizationError as exc:
                raise _with_node_location(exc, statement) from exc
            return
        if isinstance(statement, ShowGrants):
            if statement.target_kind is None:
                return
            if statement.target_kind.upper() == "USER" and statement.target_name and statement.target_name.lower() == self.session.user.name.lower():
                return
            try:
                self.session.authorize("SECURITY")
            except AuthorizationError as exc:
                raise _with_node_location(exc, statement) from exc
            return
        if isinstance(statement, CreateView):
            try:
                self.session.authorize(action, statement.name)
            except AuthorizationError as exc:
                raise _with_node_location(exc, statement) from exc
            # WHY：创建视图会保存一个可重复执行的查询，创建者必须能读取其底层对象。
            self._authorize_statement(statement.query, "SELECT", view_stack)
            return
        object_names = self._object_names(statement)
        if object_names:
            for object_name in object_names:
                try:
                    self.session.authorize(action, object_name)
                except AuthorizationError as exc:
                    raise _with_location(exc, self._object_location(statement, object_name)) from exc
                if action.upper() == "SELECT":
                    view = self.catalog.find_view(object_name)
                    if view is not None and view.system and not self.system_catalog.is_admin():
                        raise _with_location(AuthorizationError("系统视图仅对 admin 开放"), self._object_location(statement, object_name))
                    view_key = object_name.lower()
                    if view is not None and view_key not in view_stack:
                        # HOW：采用调用者权限；访问视图还要拥有其底层表/视图的 SELECT 权限。
                        self._authorize_statement(self._view_query(view), "SELECT", (*view_stack, view_key))
        else:
            try:
                self.session.authorize(action)
            except AuthorizationError as exc:
                raise _with_node_location(exc, statement) from exc
        # WHY：IN/标量子查询的表不在外层 FROM 中，必须单独检查 SELECT 权限。
        for query in self._subqueries(statement):
            self._authorize_statement(query, "SELECT", view_stack)

    @staticmethod
    def _subqueries(node: Node) -> Iterable[Select]:
        for descriptor in fields(node):
            value = getattr(node, descriptor.name)
            values = value if isinstance(value, tuple) else (value,)
            for child in values:
                if isinstance(child, Subquery):
                    yield child.query
                elif isinstance(child, Node):
                    yield from Database._subqueries(child)

    @staticmethod
    def _object_names(statement: Statement) -> tuple[str, ...]:
        if isinstance(statement, Explain):
            return Database._object_names(statement.statement)
        if isinstance(statement, Show) and statement.object_name is not None:
            return (statement.object_name,)
        if isinstance(statement, Select):
            names: list[str] = []
            if statement.from_table is not None:
                names.append(statement.from_table.name)
            names.extend(join.table.name for join in statement.joins)
            if statement.union is not None:
                names.extend(Database._object_names(statement.union))
            return tuple(dict.fromkeys(names))
        for attribute in ("table", "name"):
            value = getattr(statement, attribute, None)
            if isinstance(value, str):
                return (value,)
        return ()

    @staticmethod
    def _object_location(statement: Statement, object_name: str) -> tuple[int, int] | None:
        """返回语句中对象名的 Token 位置，供权限错误复用。"""

        if isinstance(statement, Explain):
            return Database._object_location(statement.statement, object_name)
        if isinstance(statement, Select):
            references = ([statement.from_table] if statement.from_table else []) + [join.table for join in statement.joins]
            for reference in references:
                if reference.name.lower() == object_name.lower():
                    return reference.source_location
        if isinstance(statement, CreateIndex):
            return statement.source_location_for("table") or statement.source_location
        if isinstance(statement, (Insert, Update, Delete)):
            return statement.source_location_for("table") or statement.source_location
        return statement.source_location

    def _execute_statement(self, statement: Statement, bound: BoundStatement, plan: PlanNode) -> ExecutionResult:
        if isinstance(statement, Explain):
            child = plan.children[0] if plan.children else plan_from_statement(statement.statement)
            return ExecutionResult(columns=("plan",), rows=[(child.explain(),)], plan=child.to_dict(), message="EXPLAIN")
        if isinstance(statement, Show):
            return self._show(statement)
        if isinstance(statement, CreateRole):
            try:
                role = self.rbac.create_role(statement.name)
            except AuthorizationError as exc:
                raise _with_node_location(exc, statement) from exc
            self._persist_rbac()
            return ExecutionResult(message=f"CREATE ROLE {role.name}")
        if isinstance(statement, CreateUser):
            try:
                user = self.rbac.create_user(statement.name, statement.password, roles=statement.roles)
            except AuthorizationError as exc:
                raise _with_node_location(exc, statement) from exc
            self._persist_rbac()
            return ExecutionResult(message=f"CREATE USER {user.name}")
        if isinstance(statement, Grant):
            keys = self._permission_keys(statement.privileges, statement.object_name)
            try:
                self._grant_permissions(keys, statement.target_kind, statement.target_name)
            except AuthorizationError as exc:
                raise _with_location(exc, statement.source_location_for("target") or statement.source_location) from exc
            self._persist_rbac()
            return ExecutionResult(affected_rows=len(keys), message=f"GRANT {len(keys)}")
        if isinstance(statement, Revoke):
            keys = self._permission_keys(statement.privileges, statement.object_name)
            try:
                self._revoke_permissions(keys, statement.target_kind, statement.target_name)
            except AuthorizationError as exc:
                raise _with_location(exc, statement.source_location_for("target") or statement.source_location) from exc
            self._persist_rbac()
            return ExecutionResult(affected_rows=len(keys), message=f"REVOKE {len(keys)}")
        if isinstance(statement, ShowGrants):
            return self._show_grants(statement)
        if isinstance(statement, Select):
            return self._execute_select(statement, bound.output_columns, plan=plan)
        if isinstance(statement, CreateTable):
            return self._mutate(lambda: self._create_table(statement))
        if isinstance(statement, CreateView):
            return self._mutate(lambda: self._create_view(statement, bound))
        if isinstance(statement, DropTable):
            return self._mutate(lambda: self._drop_table(statement))
        if isinstance(statement, DropView):
            return self._mutate(lambda: self._drop_view(statement))
        if isinstance(statement, Insert):
            return self._mutate(lambda: self._insert(statement, bound))
        if isinstance(statement, Update):
            return self._mutate(lambda: self._update(statement))
        if isinstance(statement, Delete):
            return self._mutate(lambda: self._delete(statement))
        if isinstance(statement, CreateIndex):
            return self._mutate(lambda: self._create_index(statement))
        if isinstance(statement, DropIndex):
            return self._mutate(lambda: self._drop_index(statement))
        raise ExecutionError(f"不支持执行 {type(statement).__name__}")

    @staticmethod
    def _permission_keys(privileges: tuple[str, ...], object_name: str | None) -> tuple[str, ...]:
        keys: list[str] = []
        for privilege in privileges:
            action = "*" if privilege.upper() in {"ALL", "*"} else privilege.upper()
            keys.append(action if object_name is None else f"{action} {object_name}")
        return tuple(keys)

    def _grant_permissions(self, privileges: tuple[str, ...], target_kind: str, target_name: str) -> None:
        if target_kind.upper() == "ROLE":
            for privilege in privileges:
                self.rbac.grant(privilege, role=target_name)
            return
        for privilege in privileges:
            self.rbac.grant(privilege, user=target_name)

    def _revoke_permissions(self, privileges: tuple[str, ...], target_kind: str, target_name: str) -> None:
        if target_kind.upper() == "ROLE":
            for privilege in privileges:
                self.rbac.revoke(privilege, role=target_name)
            return
        for privilege in privileges:
            self.rbac.revoke(privilege, user=target_name)

    def _show_grants(self, statement: ShowGrants) -> ExecutionResult:
        target_kind = (statement.target_kind or "USER").upper()
        target_name = statement.target_name or self.session.user.name
        try:
            if target_kind == "ROLE":
                principal = self.rbac.get_role(target_name).name
                privileges = self.rbac.privileges_for(role=target_name)
            else:
                principal = self.rbac.get_user(target_name).name
                privileges = self.rbac.privileges_for(user=target_name)
        except AuthorizationError as exc:
            raise _with_location(exc, statement.source_location_for("target") or statement.source_location) from exc
        rows = [(principal, privilege) for privilege in privileges]
        return ExecutionResult(
            columns=("principal", "privilege"),
            rows=rows,
            message=f"SHOW GRANTS FOR {target_kind} {principal}",
        )

    def _show(self, statement: Show) -> ExecutionResult:
        """执行基础 Catalog 查看命令。"""

        target = statement.target.upper()
        if target in {"TABLES", "TABLE"}:
            return ExecutionResult(
                columns=("table_name",),
                rows=[(table.name,) for table in self.catalog.tables()],
                message=f"SHOW {target}",
            )
        if target == "VIEWS":
            views = tuple(view for view in self.catalog.views()
                          if not view.system or self.system_catalog.is_admin())
            return ExecutionResult(
                columns=("view_name",),
                rows=[(view.name,) for view in views],
                message="SHOW VIEWS",
            )

        if statement.object_name is None:
            raise ExecutionError(f"SHOW {target} 需要指定表或视图名")
        relation = self.catalog.get_relation(statement.object_name)
        table = relation if isinstance(relation, TableMetadata) else None
        if target == "COLUMNS":
            rows = []
            for column in relation.schema:
                key = "PRI" if column.primary_key else "UNI" if column.unique else ""
                default = None if column.default is None else column.default.unwrap()
                rows.append((column.name, column.data_type.value, "YES" if column.nullable else "NO", key, default))
            return ExecutionResult(
                columns=("field", "type", "null", "key", "default"),
                rows=rows,
                message=f"SHOW COLUMNS {relation.name}",
            )
        if target == "INDEX":
            if table is None:
                return ExecutionResult(
                    columns=("table_name", "index_name", "unique", "index_type", "columns"),
                    rows=[],
                    message=f"SHOW INDEX {relation.name}",
                )
            rows = [
                (table.name, metadata.name, metadata.unique, metadata.index_type, ", ".join(metadata.columns))
                for metadata in self.catalog.indexes()
                if metadata.table_id == table.table_id
            ]
            return ExecutionResult(
                columns=("table_name", "index_name", "unique", "index_type", "columns"),
                rows=rows,
                message=f"SHOW INDEX {table.name}",
            )
        if target == "CREATE_TABLE":
            if table is None:
                raise _with_node_location(ExecutionError(f"{relation.name!r} 是视图，不是表"), statement)
            definitions: list[str] = []
            for column in table.schema:
                definition = [_sql_identifier(column.name), column.data_type.value]
                if column.primary_key:
                    definition.append("PRIMARY KEY")
                elif not column.nullable:
                    definition.append("NOT NULL")
                if column.unique and not column.primary_key:
                    definition.append("UNIQUE")
                if column.default is not None:
                    definition.extend(("DEFAULT", _sql_literal(column.default.unwrap())))
                definitions.append(" ".join(definition))
            statement_text = f"CREATE TABLE {_sql_identifier(table.name)} ({', '.join(definitions)});"
            return ExecutionResult(
                columns=("table_name", "create_statement"),
                rows=[(table.name, statement_text)],
                message=f"SHOW CREATE TABLE {table.name}",
            )
        if target == "CREATE_VIEW":
            if not isinstance(relation, ViewMetadata):
                raise _with_node_location(ExecutionError(f"{relation.name!r} 是表，不是视图"), statement)
            statement_text = f"CREATE VIEW {_sql_identifier(relation.name)} AS {relation.definition_sql};"
            return ExecutionResult(
                columns=("view_name", "create_statement"),
                rows=[(relation.name, statement_text)],
                message=f"SHOW CREATE VIEW {relation.name}",
            )
        raise ExecutionError(f"不支持 SHOW {statement.target}")

    def _mutate(self, operation: Callable[[], ExecutionResult]) -> ExecutionResult:
        """执行写操作并立即持久化。"""

        result = operation()
        self._persist_catalog()
        self.buffer_pool.flush_all()
        self._refresh_statistics()
        return result

    def _create_table(self, statement: CreateTable) -> ExecutionResult:
        if self.catalog.find_table(statement.name) is not None:
            if statement.if_not_exists:
                return ExecutionResult(message=f"table {statement.name} already exists")
            raise _with_node_location(CatalogError(f"表 {statement.name!r} 已存在"), statement)
        columns: list[Column] = []
        for definition in statement.columns:
            default = self._default_value(definition)
            columns.append(Column(definition.name, definition.data_type, definition.nullable, definition.primary_key, definition.unique, default))
        table = self.catalog.create_table(statement.name, Schema.from_iterable(columns))
        return ExecutionResult(affected_rows=0, message=f"CREATE TABLE {table.name}")

    def _create_view(self, statement: CreateView, bound: BoundStatement) -> ExecutionResult:
        """保存视图定义和输出模式；不创建数据页，因此视图天然只读。"""

        if self.catalog.find_table(statement.name, include_system=True) is not None or self.catalog.find_view(statement.name) is not None:
            if statement.if_not_exists:
                return ExecutionResult(message=f"view {statement.name} already exists")
            raise _with_node_location(CatalogError(f"表或视图 {statement.name!r} 已存在"), statement)
        definition_sql = statement.definition_sql.strip() or self._render_select(statement.query)
        schema = self._view_schema(statement.query, bound.output_columns)
        view = self.catalog.create_view(statement.name, schema, definition_sql)
        return ExecutionResult(message=f"CREATE VIEW {view.name}")

    def _drop_view(self, statement: DropView) -> ExecutionResult:
        view = self.catalog.find_view(statement.name)
        if view is None:
            if statement.if_exists:
                return ExecutionResult(message=f"view {statement.name} does not exist")
            raise _with_node_location(CatalogError(f"视图 {statement.name!r} 不存在"), statement)
        if view.system:
            raise _with_node_location(CatalogError("系统视图不能删除"), statement)
        removed = self.catalog.drop_view(statement.name)
        return ExecutionResult(message=f"DROP VIEW {removed.name}")

    def _view_query(self, view: ViewMetadata) -> Select:
        """从目录中的 SQL 定义恢复视图查询 AST。"""

        parsed = Parser(view.definition_sql).parse_one()
        if not isinstance(parsed, Select):
            raise CatalogError(f"视图 {view.name!r} 的定义不是 SELECT")
        return parsed

    def _view_schema(self, query: Select, output_names: tuple[str, ...]) -> Schema:
        """按 SELECT 输出推导视图模式；输出列不继承底层表的约束。"""

        refs = ([query.from_table] if query.from_table else []) + [join.table for join in query.joins]
        columns: list[Column] = []
        column_sources: list[Node] = []
        output_index = 0
        for item in query.items:
            if isinstance(item.expression, Star):
                selected = refs
                if item.expression.table:
                    selected = [ref for ref in refs if item.expression.table.lower() in {ref.name.lower(), (ref.alias or "").lower()}]
                for ref in selected:
                    relation = self.catalog.get_relation(ref.name)
                    for source_column in relation.schema:
                        name = output_names[output_index] if output_index < len(output_names) else source_column.name
                        columns.append(Column(name, source_column.data_type))
                        column_sources.append(item)
                        output_index += 1
                continue
            name = output_names[output_index] if output_index < len(output_names) else item.alias or self._expression_name(item.expression)
            columns.append(Column(name, self._view_expression_type(item.expression, refs)))
            column_sources.append(item)
            output_index += 1
        try:
            return Schema.from_iterable(columns)
        except ValueError as exc:
            seen: set[str] = set()
            duplicate_index: int | None = None
            for index, column in enumerate(columns):
                key = column.name.lower()
                if key in seen:
                    duplicate_index = index
                    break
                seen.add(key)
            duplicate_item = column_sources[duplicate_index] if duplicate_index is not None and duplicate_index < len(column_sources) else None
            raise _with_node_location(BinderError("视图输出列名重复，请为列添加唯一别名"), duplicate_item) from exc

    def _view_expression_type(self, expression: Expr, refs: list[TableRef]) -> DataType:
        if isinstance(expression, ColumnRef):
            relation = self._view_column_relation(expression, refs)
            return relation.schema.column(expression.name).data_type
        if isinstance(expression, Literal):
            inferred = Value.infer(expression.value).data_type
            return DataType.VARCHAR if inferred is DataType.NULL else inferred
        if isinstance(expression, FunctionCall):
            name = expression.name.lower()
            if name == "count":
                return DataType.INT
            if name == "avg":
                return DataType.FLOAT
            if name in {"sum", "min", "max", "abs"} and expression.args:
                return self._view_expression_type(expression.args[0], refs)
            if name == "coalesce" and expression.args:
                return self._view_expression_type(expression.args[0], refs)
            return DataType.VARCHAR
        if isinstance(expression, UnaryOp):
            return DataType.BOOLEAN if expression.operator.upper() == "NOT" else self._view_expression_type(expression.operand, refs)
        if isinstance(expression, BinaryOp):
            if expression.operator.upper() in {"AND", "OR", "=", "==", "!=", "<>", "<", "<=", ">", ">=", "LIKE", "||"}:
                return DataType.BOOLEAN if expression.operator.upper() != "||" else DataType.VARCHAR
            left = self._view_expression_type(expression.left, refs)
            right = self._view_expression_type(expression.right, refs)
            return DataType.FLOAT if DataType.FLOAT in {left, right} else DataType.INT
        if isinstance(expression, (IsNull, InPredicate, BetweenPredicate)):
            return DataType.BOOLEAN
        return DataType.VARCHAR

    def _view_column_relation(self, expression: ColumnRef, refs: list[TableRef]) -> TableMetadata | ViewMetadata:
        if expression.table:
            for ref in refs:
                if expression.table.lower() in {ref.name.lower(), (ref.alias or "").lower()}:
                    return self.catalog.get_relation(ref.name)
            raise _with_node_location(BinderError(f"表或别名 {expression.table!r} 不存在"), expression)
        matches: list[TableMetadata | ViewMetadata] = []
        for ref in refs:
            relation = self.catalog.get_relation(ref.name)
            try:
                relation.schema.column(expression.name)
            except BinderError:
                continue
            matches.append(relation)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise _with_node_location(BinderError(f"列 {expression.name!r} 不存在"), expression)
        raise _with_node_location(BinderError(f"列 {expression.name!r} 存在歧义，请使用表名限定"), expression)

    @staticmethod
    def _render_select(statement: Select) -> str:
        """为手工构造 AST 提供一个可持久化的最小 SQL 渲染器。"""

        def render_expr(expression: Expr) -> str:
            if isinstance(expression, Literal):
                return _sql_literal(expression.value)
            if isinstance(expression, Star):
                return f"{_sql_identifier(expression.table)}.*" if expression.table else "*"
            if isinstance(expression, ColumnRef):
                return f"{_sql_identifier(expression.table)}.{_sql_identifier(expression.name)}" if expression.table else _sql_identifier(expression.name)
            if isinstance(expression, FunctionCall):
                distinct = "DISTINCT " if expression.distinct else ""
                return f"{expression.name}({distinct}{', '.join(render_expr(item) for item in expression.args)})"
            if isinstance(expression, UnaryOp):
                return f"{expression.operator} {render_expr(expression.operand)}"
            if isinstance(expression, BinaryOp):
                return f"({render_expr(expression.left)} {expression.operator} {render_expr(expression.right)})"
            if isinstance(expression, IsNull):
                return f"{render_expr(expression.expression)} IS {'NOT ' if expression.negated else ''}NULL"
            if isinstance(expression, BetweenPredicate):
                return f"{render_expr(expression.expression)} {'NOT ' if expression.negated else ''}BETWEEN {render_expr(expression.lower)} AND {render_expr(expression.upper)}"
            if isinstance(expression, InPredicate):
                values = ", ".join(render_expr(value) for value in expression.values)
                return f"{render_expr(expression.expression)} {'NOT ' if expression.negated else ''}IN ({values})"
            raise CatalogError(f"无法渲染视图表达式 {type(expression).__name__}")

        items = []
        for item in statement.items:
            text = render_expr(item.expression)
            items.append(f"{text} AS {_sql_identifier(item.alias)}" if item.alias else text)
        result = f"SELECT {'DISTINCT ' if statement.distinct else ''}{', '.join(items)}"
        if statement.from_table:
            result += f" FROM {_sql_identifier(statement.from_table.name)}"
            if statement.from_table.alias:
                result += f" AS {_sql_identifier(statement.from_table.alias)}"
        if statement.where:
            result += f" WHERE {render_expr(statement.where)}"
        return result

    @staticmethod
    def _default_value(definition: ColumnDefinition) -> Value | None:
        if definition.default is None:
            return None
        if not isinstance(definition.default, Literal):
            raise _with_node_location(BinderError("DEFAULT 目前只支持字面量"), definition.default)
        try:
            return Value.infer(definition.default.value).coerce(definition.data_type)
        except BinderError as exc:
            raise _with_node_location(exc, definition.default) from exc

    def _drop_table(self, statement: DropTable) -> ExecutionResult:
        table = self.catalog.find_table(statement.name)
        if table is None:
            if statement.if_exists:
                return ExecutionResult(message=f"table {statement.name} does not exist")
            raise _with_node_location(CatalogError(f"表 {statement.name!r} 不存在"), statement)
        removed_indexes = [self.catalog.get_index(name) for name in table.indexes]
        removed = self.catalog.drop_table(statement.name)
        for page_id in removed.page_ids:
            self.buffer_pool.delete_page(int(page_id))
        for metadata in removed_indexes:
            tree = self.index_manager.drop(metadata.name)
            if tree is not None:
                tree.destroy()
        self._heaps.pop(int(removed.table_id), None)
        return ExecutionResult(message=f"DROP TABLE {removed.name}")

    def _insert(self, statement: Insert, bound: BoundStatement) -> ExecutionResult:
        table = self.catalog.get_table(statement.table)
        heap = self._heap(table)
        insert_indexes = bound.insert_indexes or tuple(range(len(table.schema)))
        inserted = 0
        for row_index, expressions in enumerate(statement.values):
            supplied = [self._eval_expr(expression, {}) for expression in expressions]
            values: list[object] = []
            supplied_map = dict(zip(insert_indexes, supplied, strict=True))
            for index, column in enumerate(table.schema):
                values.append(supplied_map.get(index, column.default.unwrap() if column.default is not None else None))
            row_location = statement.source_location_for(f"row:{row_index}")
            try:
                row = table.schema.validate_row(tuple(values))
            except YourSQLError as exc:
                raise _with_location(exc, row_location) from exc
            self._check_constraints(table, row, None, location=row_location)
            row_id = heap.insert(row)
            self._update_indexes(table, row, row_id, insert=True)
            table.page_ids = [PageId(page_id) for page_id in heap.page_ids]
            table.first_page_id = table.page_ids[0] if table.page_ids else None
            table.row_count += 1
            inserted += 1
        return ExecutionResult(affected_rows=inserted, message=f"INSERT {inserted}")

    def _update(self, statement: Update) -> ExecutionResult:
        table = self.catalog.get_table(statement.table)
        heap = self._heap(table)
        targets: list[tuple[RowId, tuple[object, ...], tuple[object, ...]]] = []
        for row_id, row in heap.scan():
            context = self._table_context(TableRef(table.name), row, row_id, table)
            if statement.where is not None and not sql_truth(self._eval_expr(statement.where, context)):
                continue
            values = list(row)
            for column_name, expression in statement.assignments:
                values[table.schema.index(column_name)] = self._eval_expr(expression, context)
            assignment_location = statement.source_location_for("assignment:0")
            try:
                new_row = table.schema.validate_row(tuple(values))
            except YourSQLError as exc:
                raise _with_location(exc, assignment_location) from exc
            self._check_constraints(table, new_row, row_id, location=assignment_location)
            targets.append((row_id, row, new_row))
        for row_id, old_row, new_row in targets:
            self._update_indexes(table, old_row, row_id, insert=False)
            heap.update(row_id, new_row)
            try:
                self._update_indexes(table, new_row, row_id, insert=True)
            except Exception:
                heap.update(row_id, old_row)
                self._update_indexes(table, old_row, row_id, insert=True)
                raise
        return ExecutionResult(affected_rows=len(targets), message=f"UPDATE {len(targets)}")

    def _delete(self, statement: Delete) -> ExecutionResult:
        table = self.catalog.get_table(statement.table)
        heap = self._heap(table)
        targets: list[tuple[RowId, tuple[object, ...]]] = []
        for row_id, row in heap.scan():
            context = self._table_context(TableRef(table.name), row, row_id, table)
            if statement.where is None or sql_truth(self._eval_expr(statement.where, context)):
                targets.append((row_id, row))
        for row_id, row in targets:
            self._update_indexes(table, row, row_id, insert=False)
            heap.delete(row_id)
            table.row_count = max(0, table.row_count - 1)
        return ExecutionResult(affected_rows=len(targets), message=f"DELETE {len(targets)}")

    def _check_constraints(
        self,
        table: TableMetadata,
        row: tuple[object, ...],
        excluded: RowId | None,
        *,
        location: tuple[int, int] | None = None,
    ) -> None:
        indexed_unique_columns = {
            metadata.columns[0].lower()
            for metadata in self.catalog.indexes()
            if metadata.table_id == table.table_id and metadata.unique and len(metadata.columns) == 1
        }
        for index, column in enumerate(table.schema):
            if not (column.primary_key or column.unique) or row[index] is None:
                continue
            # HOW：生成大样本时预先建立的单列唯一索引可以 O(log n) 校验约束，
            # 避免每行都回扫整张事实表；没有索引时保留原有全表校验路径。
            if column.name.lower() in indexed_unique_columns:
                continue
            for row_id, existing in self._heap(table).scan():
                if excluded is not None and row_id == excluded:
                    continue
                if existing[index] == row[index]:
                    raise _with_location(ExecutionError(f"列 {column.name} 的唯一约束冲突"), location)
        for metadata in self.catalog.indexes():
            if metadata.table_id != table.table_id or not metadata.unique:
                continue
            key = self._index_key(table, metadata, row)
            if any(value is None for value in key):
                continue
            for row_id in self.index_manager.get(metadata.name).search(key):
                if excluded is None or row_id != excluded:
                    raise _with_location(ExecutionError(f"索引 {metadata.name} 的唯一约束冲突"), location)

    def _update_indexes(self, table: TableMetadata, row: tuple[object, ...], row_id: RowId, *, insert: bool) -> None:
        for metadata in self.catalog.indexes():
            if metadata.table_id != table.table_id:
                continue
            tree = self.index_manager.get(metadata.name)
            key = self._index_key(table, metadata, row)
            if any(value is None for value in key) and metadata.unique:
                continue
            if insert:
                tree.insert(key, row_id)
            else:
                tree.delete(key, row_id)

    @staticmethod
    def _index_key(table: TableMetadata, metadata: IndexMetadata, row: tuple[object, ...]) -> tuple[object, ...]:
        return tuple(row[table.schema.index(column)] for column in metadata.columns)

    def _create_index(self, statement: CreateIndex) -> ExecutionResult:
        if any(index.name.lower() == statement.name.lower() for index in self.catalog.indexes()):
            if statement.if_not_exists:
                return ExecutionResult(message=f"index {statement.name} already exists")
            raise _with_node_location(CatalogError(f"索引 {statement.name!r} 已存在"), statement)
        try:
            table = self.catalog.get_table(statement.table)
        except CatalogError as exc:
            raise _with_location(exc, statement.source_location_for("table")) from exc
        metadata = IndexMetadata(statement.name, table.table_id, statement.columns, statement.unique)
        tree = self.index_manager.create(
            statement.name,
            unique=statement.unique,
            buffer_pool=self.buffer_pool,
            on_root_change=lambda page_id: setattr(metadata, "root_page_id", PageId(page_id)),
        )
        try:
            tree.bulk_load(
                (key, row_id)
                for row_id, row in self._heap(table).scan()
                for key in (self._index_key(table, metadata, row),)
                if not (metadata.unique and any(value is None for value in key))
            )
            self.catalog.create_index(metadata)
        except YourSQLError as exc:
            self.index_manager.drop(statement.name)
            tree.destroy()
            raise _with_location(exc, statement.source_location_for("column:0") or statement.source_location) from exc
        except Exception:
            self.index_manager.drop(statement.name)
            tree.destroy()
            raise
        return ExecutionResult(message=f"CREATE INDEX {statement.name}")

    def _drop_index(self, statement: DropIndex) -> ExecutionResult:
        try:
            metadata = self.catalog.get_index(statement.name)
        except CatalogError:
            if statement.if_exists:
                return ExecutionResult(message=f"index {statement.name} does not exist")
            raise _with_node_location(CatalogError(f"索引 {statement.name!r} 不存在"), statement)
        self.catalog.drop_index(statement.name)
        tree = self.index_manager.drop(statement.name)
        if tree is not None:
            tree.destroy()
        elif metadata.root_page_id is not None:
            # 兼容目录存在但进程内索引尚未加载的异常场景。
            self.buffer_pool.delete_page(int(metadata.root_page_id))
        return ExecutionResult(message=f"DROP INDEX {metadata.name}")

    def _execute_select(
        self,
        statement: Select,
        output_columns: Iterable[str] = (),
        *,
        plan: PlanNode | None = None,
        allow_system_tables: bool = False,
    ) -> ExecutionResult:
        trace = current_trace.get()
        if trace is not None:
            trace.check()
        if statement.union is not None:
            left = self._execute_select(replace(statement, union=None), output_columns, plan=plan)
            right = self._execute_select(statement.union)
            rows = [*left.rows, *right.rows]
            if not statement.union_all:
                rows = list(dict.fromkeys(rows))
            return ExecutionResult(left.columns, rows, stats={"operator": "Union", "left_rows": len(left.rows), "right_rows": len(right.rows)})
        before = self.buffer_pool.stats()
        contexts = list(self._iter_select_contexts(statement, plan=plan, allow_system_tables=allow_system_tables))
        has_aggregate = any(self._contains_aggregate(item.expression) for item in statement.items) or self._contains_aggregate(statement.having)
        grouped: list[dict[str, object]] = []
        if statement.group_by or has_aggregate:
            groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
            for context in contexts:
                if trace is not None:
                    trace.step()
                key = tuple(self._eval_expr(expression, context) for expression in statement.group_by)
                groups.setdefault(key, []).append(context)
            if has_aggregate and not groups:
                groups[()] = []
            for group in groups.values():
                base = dict(group[0]) if group else {"__row_order__": [], "__row_ids__": {}}
                base["__group__"] = group
                if statement.having is None or sql_truth(self._eval_expr(statement.having, base)):
                    grouped.append(base)
        else:
            grouped = contexts
        projected: list[tuple[tuple[object, ...], dict[str, object], dict[str, object]]] = []
        names = list(output_columns)
        if not names:
            names = self._output_names(statement)
        for context in grouped:
            if trace is not None:
                trace.step()
            values: list[object] = []
            aliases: dict[str, object] = {}
            for item in statement.items:
                if isinstance(item.expression, Star):
                    values.extend(self._expand_star(context, item.expression.table))
                else:
                    value = self._eval_expr(item.expression, context)
                    values.append(value)
                    if item.alias:
                        aliases[item.alias.lower()] = value
            projected.append((tuple(values), context, aliases))
        if statement.distinct:
            unique: dict[tuple[object, ...], tuple[tuple[object, ...], dict[str, object], dict[str, object]]] = {}
            for item in projected:
                unique.setdefault(item[0], item)
            projected = list(unique.values())
        for order_item in reversed(statement.order_by):
            def key(item: tuple[tuple[object, ...], dict[str, object], dict[str, object]]) -> tuple[int, object]:
                value = _MISSING
                if isinstance(order_item.expression, ColumnRef) and not order_item.expression.table:
                    value = item[2].get(order_item.expression.name.lower(), _MISSING)
                if value is _MISSING:
                    value = self._eval_expr(order_item.expression, item[1])
                nulls_first = order_item.nulls_first if order_item.nulls_first is not None else order_item.descending
                if value is None:
                    return (0 if nulls_first else 1, 0)
                return (1 if nulls_first else 0, value)

            try:
                projected.sort(key=key, reverse=order_item.descending)
            except TypeError:
                projected.sort(key=lambda item: repr(key(item)), reverse=order_item.descending)
        if statement.offset:
            projected = projected[statement.offset :]
        if statement.limit is not None:
            projected = projected[: statement.limit]
        after = self.buffer_pool.stats()
        stats = {
            "operator": "SeqScan",
            "page_reads": int(after["misses"] - before["misses"]),
            "cache_hits": int(after["hits"] - before["hits"]),
            "rows_examined": len(contexts),
        }
        uses_index = self._plan_uses_index(plan) if plan is not None else self._uses_index(statement)
        if uses_index:
            stats["operator"] = "IndexScan"
        return ExecutionResult(tuple(names), [item[0] for item in projected], stats=stats)

    def _iter_select_contexts(
        self,
        statement: Select,
        *,
        plan: PlanNode | None = None,
        allow_system_tables: bool = False,
    ) -> Iterable[dict[str, object]]:
        trace = current_trace.get()
        if self._plan_contains_kind(plan, "EmptyScan"):
            return []
        scan_plans = self._scan_plans(plan)
        if statement.from_table is None:
            contexts: list[dict[str, object]] = [{"__row_order__": [], "__row_ids__": {}, "__schemas__": {}}]
        else:
            primary_plan = scan_plans[0] if scan_plans else None
            contexts = list(self._scan_contexts(
                statement.from_table,
                self._scan_predicate(scan_plans[0] if scan_plans else None, statement.where),
                scan_plan=primary_plan,
                allow_system_tables=allow_system_tables,
            ))
            for join_index, join in enumerate(statement.joins, start=1):
                join_plan = scan_plans[join_index] if join_index < len(scan_plans) else None
                right = list(self._scan_contexts(
                    join.table,
                    self._scan_predicate(join_plan, None),
                    scan_plan=join_plan,
                    allow_system_tables=allow_system_tables,
                ))
                combined: list[dict[str, object]] = []
                matched_right: set[int] = set()
                for left_context in contexts:
                    matched = False
                    for index, right_context in enumerate(right):
                        if trace is not None:
                            trace.step()
                        merged = self._merge_context(left_context, right_context)
                        if join.join_type == "CROSS" or sql_truth(self._eval_expr(join.on, merged)):
                            combined.append(merged)
                            matched = True
                            matched_right.add(index)
                    if not matched and join.join_type == "LEFT":
                        combined.append(self._merge_context(left_context, self._null_context(join.table)))
                if join.join_type in {"RIGHT", "FULL"}:
                    for index, right_context in enumerate(right):
                        if index not in matched_right:
                            combined.append(self._merge_context(self._null_context(statement.from_table), right_context))
                contexts = combined
        if statement.where is not None:
            contexts = [context for context in contexts if sql_truth(self._eval_expr(statement.where, context))]
        return contexts

    def _scan_contexts(
        self,
        reference: TableRef,
        predicate: Expr | None,
        *,
        scan_plan: PlanNode | None = None,
        allow_system_tables: bool = False,
    ) -> Iterable[dict[str, object]]:
        trace = current_trace.get()
        try:
            relation = self.catalog.get_relation(reference.name)
        except CatalogError:
            if not allow_system_tables:
                raise
            # HOW：只有系统视图的内部定义允许回读隐藏权限表，普通 SQL 仍由 Binder 拒绝。
            relation = self.catalog.get_table(reference.name, include_system=True)
        if isinstance(relation, ViewMetadata):
            # HOW：逻辑视图先执行定义查询得到内存行，再由外层 SELECT 继续过滤/连接。
            result = self._execute_select(
                self._view_query(relation),
                relation.schema.names(),
                allow_system_tables=relation.system,
            )
            if trace is not None:
                trace.scans.append({"table": relation.name, "operator": "ViewScan", "candidate_rows": len(result.rows)})
            for slot_id, row in enumerate(result.rows):
                if trace is not None:
                    trace.step()
                yield self._table_context(reference, tuple(row), RowId(PageId(-1), slot_id), relation)
            return

        table = relation
        heap = self._heap(table)
        effective_predicate = self._scan_predicate(scan_plan, predicate)
        candidates = None
        if scan_plan is None or scan_plan.kind == "IndexScan":
            candidates = self._candidate_row_ids(table, reference, effective_predicate)
        if trace is not None:
            trace.scans.append({"table": table.name, "operator": "SeqScan" if candidates is None else "IndexScan", "candidate_rows": None if candidates is None else len(candidates)})
        if candidates is None:
            rows = heap.scan()
        else:
            rows = ((row_id, heap.read(row_id)) for row_id in candidates)
        for row_id, row in rows:
            if trace is not None:
                trace.step()
            if row is not None:
                yield self._table_context(reference, row, row_id, table)

    @staticmethod
    def _scan_plans(plan: PlanNode | None) -> list[PlanNode]:
        """按输入顺序提取计划中的扫描节点，供 AST evaluator 使用。"""

        if plan is None:
            return []
        result: list[PlanNode] = []
        if plan.kind in {"SeqScan", "IndexScan"}:
            result.append(plan)
        for child in plan.children:
            result.extend(Database._scan_plans(child))
        return result

    @staticmethod
    def _plan_contains_kind(plan: PlanNode | None, kind: str) -> bool:
        if plan is None:
            return False
        return plan.kind == kind or any(Database._plan_contains_kind(child, kind) for child in plan.children)

    @staticmethod
    def _scan_predicate(scan_plan: PlanNode | None, fallback: Expr | None) -> Expr | None:
        if scan_plan is not None:
            pushed = scan_plan.properties.get("pushed_predicate")
            if isinstance(pushed, Expr):
                return pushed
        return fallback

    @staticmethod
    def _plan_uses_index(plan: PlanNode) -> bool:
        return any(node.kind == "IndexScan" for node in Database._scan_plans(plan))

    def _candidate_row_ids(self, table: TableMetadata, reference: TableRef, predicate: Expr | None) -> tuple[RowId, ...] | None:
        if predicate is None:
            return None
        if isinstance(predicate, BetweenPredicate) and predicate.negated:
            # WHY：NOT BETWEEN 是两个可分别定位的范围；直接把它当成不支持
            # 的谓词会错过索引，而把整个结果取反又无法避免顺序扫描。
            return self._candidate_row_ids(
                table,
                reference,
                BinaryOp(
                    BinaryOp(predicate.expression, "<", predicate.lower),
                    "OR",
                    BinaryOp(predicate.expression, ">", predicate.upper),
                ),
            )
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "OR":
            left = self._candidate_row_ids(table, reference, predicate.left)
            right = self._candidate_row_ids(table, reference, predicate.right)
            if left is None or right is None:
                return None
            return tuple(sorted(set(left) | set(right)))
        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "AND":
            atoms = self._conjunction_atoms(predicate)
            combined = self._candidate_for_atoms(table, reference, atoms)
            if combined is not None:
                return combined
            # HOW：当 AND 的一侧仍是 OR 时，先分别取得可用候选集；
            # 这样 ``(a = 1 OR a = 2) AND b = 3`` 至少能利用 a 的索引。
            left = self._candidate_row_ids(table, reference, predicate.left)
            right = self._candidate_row_ids(table, reference, predicate.right)
            if left is None:
                return right
            if right is None:
                return left
            return tuple(sorted(set(left) & set(right)))
        else:
            atoms = (predicate,)
        return self._candidate_for_atoms(table, reference, atoms)

    def _candidate_for_atoms(
        self,
        table: TableMetadata,
        reference: TableRef,
        atoms: tuple[Expr, ...],
    ) -> tuple[RowId, ...] | None:
        """按索引元数据计算一组 AND 条件的候选 RowId 交集。"""

        candidates: list[tuple[RowId, ...]] = []
        for metadata in self.catalog.indexes():
            if metadata.table_id != table.table_id:
                continue
            current = self._index_candidates_for_atoms(table, reference, metadata, atoms)
            if current is not None:
                candidates.append(current)
        if not candidates:
            return None
        result = set(candidates[0])
        for current in candidates[1:]:
            result.intersection_update(current)
        return tuple(sorted(result))

    @staticmethod
    def _conjunction_atoms(predicate: Expr) -> tuple[Expr, ...]:
        """展开 AND，便于把多个条件组合成联合索引的连续前缀。"""

        if isinstance(predicate, BinaryOp) and predicate.operator.upper() == "AND":
            return (*Database._conjunction_atoms(predicate.left), *Database._conjunction_atoms(predicate.right))
        return (predicate,)

    @staticmethod
    def _constant_expression(expression: Expr) -> tuple[bool, object]:
        """提取索引边界所需的常量，也覆盖负数等一元字面量。"""

        return Optimizer.constant_value(expression)

    @staticmethod
    def _same_index_value(left: object, right: object) -> bool:
        if left is None or right is None:
            return left is None and right is None
        return compare_values(left, right, "=") is True

    @classmethod
    def _compare_index_values(cls, left: object, right: object) -> int:
        if cls._same_index_value(left, right):
            return 0
        return -1 if compare_values(left, right, "<") is True else 1

    @staticmethod
    def _merge_allowed(existing: list[object] | None, incoming: list[object]) -> list[object]:
        if existing is None:
            return list(incoming)
        return [
            value
            for value in existing
            if any(Database._same_index_value(value, candidate) for candidate in incoming)
        ]

    @classmethod
    def _merge_lower(
        cls,
        existing: tuple[object, bool] | None,
        incoming: tuple[object, bool],
    ) -> tuple[object, bool]:
        if existing is None:
            return incoming
        comparison = cls._compare_index_values(incoming[0], existing[0])
        if comparison > 0:
            return incoming
        if comparison < 0:
            return existing
        return incoming if not incoming[1] else existing

    @classmethod
    def _merge_upper(
        cls,
        existing: tuple[object, bool] | None,
        incoming: tuple[object, bool],
    ) -> tuple[object, bool]:
        if existing is None:
            return incoming
        comparison = cls._compare_index_values(incoming[0], existing[0])
        if comparison < 0:
            return incoming
        if comparison > 0:
            return existing
        return incoming if not incoming[1] else existing

    @staticmethod
    def _index_column(column: ColumnRef, reference: TableRef) -> str | None:
        if column.table and column.table.lower() not in {reference.name.lower(), (reference.alias or "").lower()}:
            return None
        return column.name.lower()

    def _index_atom_constraint(self, atom: Expr, reference: TableRef) -> tuple[str, _IndexConstraint] | None:
        """把一个谓词转换成单列约束；无法安全定位时返回 None。"""

        if isinstance(atom, BinaryOp):
            operator = atom.operator.upper()
            left_column = atom.left if isinstance(atom.left, ColumnRef) else None
            right_column = atom.right if isinstance(atom.right, ColumnRef) else None
            if left_column is not None and right_column is None:
                column = self._index_column(left_column, reference)
                found, value = self._constant_expression(atom.right)
            elif right_column is not None and left_column is None:
                column = self._index_column(right_column, reference)
                found, value = self._constant_expression(atom.left)
                if operator in {"<", "<=", ">", ">="}:
                    operator = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}[operator]
            else:
                return None
            if column is None or not found:
                return None
            if operator == "LIKE":
                if not isinstance(value, str) or any(marker in value for marker in ("%", "_")):
                    return None
                operator = "="
            if operator == "=":
                return column, _IndexConstraint(allowed=[value])
            if operator in {"<", "<=", ">", ">="}:
                boundary = (value, operator in {">=", "<="})
                return column, _IndexConstraint(
                    lower=boundary if operator in {">", ">="} else None,
                    upper=boundary if operator in {"<", "<="} else None,
                )
            return None

        if isinstance(atom, IsNull) and isinstance(atom.expression, ColumnRef):
            column = self._index_column(atom.expression, reference)
            if column is None:
                return None
            return column, _IndexConstraint(allowed=None if atom.negated else [None], not_null=atom.negated)

        if isinstance(atom, InPredicate) and not atom.negated and isinstance(atom.expression, ColumnRef):
            column = self._index_column(atom.expression, reference)
            if column is None:
                return None
            values: list[object] = []
            for expression in atom.values:
                found, value = self._constant_expression(expression)
                if not found:
                    return None
                values.append(value)
            return column, _IndexConstraint(allowed=values)

        if isinstance(atom, BetweenPredicate) and not atom.negated and isinstance(atom.expression, ColumnRef):
            column = self._index_column(atom.expression, reference)
            if column is None:
                return None
            lower_found, lower = self._constant_expression(atom.lower)
            upper_found, upper = self._constant_expression(atom.upper)
            if not lower_found or not upper_found:
                return None
            return column, _IndexConstraint(lower=(lower, True), upper=(upper, True))
        return None

    def _index_candidates_for_atoms(
        self,
        table: TableMetadata,
        reference: TableRef,
        metadata: IndexMetadata,
        atoms: tuple[Expr, ...],
    ) -> tuple[RowId, ...] | None:
        constraints: dict[str, _IndexConstraint] = {}
        for atom in atoms:
            parsed = self._index_atom_constraint(atom, reference)
            if parsed is None:
                continue
            column, incoming = parsed
            current = constraints.setdefault(column, _IndexConstraint())
            if incoming.allowed is not None:
                current.allowed = self._merge_allowed(current.allowed, incoming.allowed)
            if incoming.lower is not None:
                current.lower = self._merge_lower(current.lower, incoming.lower)
            if incoming.upper is not None:
                current.upper = self._merge_upper(current.upper, incoming.upper)
            current.not_null = current.not_null or incoming.not_null

        tree = self.index_manager.get(metadata.name)
        columns = tuple(column.lower() for column in metadata.columns)

        def scan(position: int, prefix: tuple[object, ...]) -> tuple[RowId, ...] | None:
            if position >= len(columns):
                return tree.search(prefix)
            constraint = constraints.get(columns[position])
            if constraint is None:
                entries = tree.prefix_scan(prefix) if prefix else ()
                return tuple(row_id for _key, row_id in entries) if prefix else None

            allowed = constraint.allowed
            if allowed is not None:
                values = [
                    value
                    for value in allowed
                    if not constraint.not_null or value is not None
                ]
                if not values:
                    return ()
                result: set[RowId] = set()
                for value in values:
                    if constraint.lower is not None:
                        comparison = self._compare_index_values(value, constraint.lower[0])
                        if comparison < 0 or (comparison == 0 and not constraint.lower[1]):
                            continue
                    if constraint.upper is not None:
                        comparison = self._compare_index_values(value, constraint.upper[0])
                        if comparison > 0 or (comparison == 0 and not constraint.upper[1]):
                            continue
                    nested = scan(position + 1, (*prefix, value))
                    if nested is None:
                        entries = tree.prefix_scan((*prefix, value))
                        return tuple(row_id for _key, row_id in entries)
                    result.update(nested)
                return tuple(sorted(result))

            if constraint.lower is None and constraint.upper is None and not constraint.not_null:
                return tree.prefix_scan(prefix) if prefix else None
            if constraint.lower is not None and constraint.lower[0] is None:
                return ()
            if constraint.upper is not None and constraint.upper[0] is None:
                return ()
            entries = tree.range_scan_prefix(
                prefix,
                constraint.lower[0] if constraint.lower is not None else None,
                constraint.upper[0] if constraint.upper is not None else None,
                include_low=constraint.lower[1] if constraint.lower is not None else True,
                include_high=constraint.upper[1] if constraint.upper is not None else True,
            )
            if constraint.not_null:
                entries = tuple(
                    (key, row_id)
                    for key, row_id in entries
                    if len(key) > position and key[position] is not None
                )
            return tuple(row_id for _key, row_id in entries)

        has_leading_constraint = bool(columns) and columns[0] in constraints
        if not has_leading_constraint:
            return None
        return scan(0, ())

    def _table_context(self, reference: TableRef, row: tuple[object, ...], row_id: RowId, table: TableMetadata | ViewMetadata) -> dict[str, object]:
        alias = (reference.alias or reference.name).lower()
        table_name = reference.name.lower()
        values: dict[str, object] = {"__row_ids__": {alias: row_id, table_name: row_id}, "__schemas__": {alias: table.schema, table_name: table.schema}, "__row_order__": []}
        for column, value in zip(table.schema, row, strict=True):
            column_name = column.name.lower()
            values[f"{alias}.{column_name}"] = value
            values[f"{table_name}.{column_name}"] = value
            values["__row_order__"].append((alias, column.name, value))  # type: ignore[union-attr]
            if column_name not in values:
                values[column_name] = value
            else:
                values[column_name] = _AMBIGUOUS
        return values

    def _null_context(self, reference: TableRef) -> dict[str, object]:
        relation = self.catalog.get_relation(reference.name)
        row = tuple(None for _column in relation.schema)
        return self._table_context(reference, row, RowId(PageId(-1), -1), relation)

    def _merge_context(self, left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
        merged = {key: value for key, value in left.items() if key not in {"__row_ids__", "__schemas__", "__row_order__"}}
        for key, value in right.items():
            if key not in {"__row_ids__", "__schemas__", "__row_order__"}:
                if key in merged and "." not in key:
                    merged[key] = _AMBIGUOUS
                else:
                    merged[key] = value
        merged["__row_ids__"] = {**left.get("__row_ids__", {}), **right.get("__row_ids__", {})}
        merged["__schemas__"] = {**left.get("__schemas__", {}), **right.get("__schemas__", {})}
        merged["__row_order__"] = [*left.get("__row_order__", []), *right.get("__row_order__", [])]
        return merged

    def _expand_star(self, context: dict[str, object], table_name: str | None) -> list[object]:
        result: list[object] = []
        for alias, column, value in context.get("__row_order__", []):  # type: ignore[union-attr]
            if table_name is None or str(table_name).lower() in {str(alias).lower()}:
                result.append(value)
        return result

    def _eval_expr(self, expression: Expr | None, context: dict[str, object]) -> object:
        if expression is None:
            return None
        if isinstance(expression, Literal):
            return expression.value
        if isinstance(expression, Parameter):
            return None
        if isinstance(expression, Star):
            return self._expand_star(context, expression.table)
        if isinstance(expression, ColumnRef):
            key = f"{expression.table.lower()}.{expression.name.lower()}" if expression.table else expression.name.lower()
            value = context.get(key, _MISSING)
            if value is _MISSING or value is _AMBIGUOUS:
                raise _with_node_location(ExecutionError(f"执行时找不到列 {expression.qualified_name}"), expression)
            return value
        if isinstance(expression, UnaryOp):
            value = self._eval_expr(expression.operand, context)
            if expression.operator == "NOT":
                return None if value is None else not bool(value)
            if value is None:
                return None
            try:
                if expression.operator == "+":
                    return +value
                if expression.operator == "-":
                    return -value
            except (TypeError, ValueError, OverflowError) as exc:
                raise _with_node_location(ExecutionError(f"一元运算失败: {exc}"), expression) from exc
        if isinstance(expression, BinaryOp):
            left = self._eval_expr(expression.left, context)
            right = self._eval_expr(expression.right, context)
            operator = expression.operator.upper()
            if operator == "AND":
                if left is False or right is False:
                    return False
                if left is True and right is True:
                    return True
                return None
            if operator == "OR":
                if left is True or right is True:
                    return True
                if left is False and right is False:
                    return False
                return None
            if operator in {"=", "!=", "<>", "<", "<=", ">", ">="}:
                return compare_values(left, right, operator)
            if operator in {"LIKE", "NOT LIKE"}:
                if left is None or right is None:
                    return None
                pattern = "^" + re.escape(str(right)).replace(r"%", ".*").replace(r"_", ".") + "$"
                matched = re.match(pattern, str(left), flags=re.DOTALL) is not None
                return not matched if operator == "NOT LIKE" else matched
            if left is None or right is None:
                return None
            try:
                if operator == "+":
                    return left + right
                if operator == "-":
                    return left - right
                if operator == "*":
                    return left * right
                if operator == "/":
                    if right == 0:
                        raise _with_node_location(ExecutionError("除数不能为零"), expression)
                    return left / right
                if operator == "%":
                    return left % right
                if operator == "||":
                    return str(left) + str(right)
            except (TypeError, ValueError, OverflowError) as exc:
                raise _with_node_location(ExecutionError(f"算术运算失败: {exc}"), expression) from exc
        if isinstance(expression, IsNull):
            result = self._eval_expr(expression.expression, context) is None
            return not result if expression.negated else result
        if isinstance(expression, InPredicate):
            left = self._eval_expr(expression.expression, context)
            values: list[object] = []
            for value in expression.values:
                if isinstance(value, Subquery):
                    subquery = self._execute_select(value.query)
                    values.extend(row[0] for row in subquery.rows if row)
                else:
                    values.append(self._eval_expr(value, context))
            result: bool | None = False
            for value in values:
                compared = compare_values(left, value, "=")
                if compared is True:
                    result = True
                    break
                if compared is None:
                    result = None
            if expression.negated:
                return None if result is None else not result
            return result
        if isinstance(expression, BetweenPredicate):
            value = self._eval_expr(expression.expression, context)
            lower = self._eval_expr(expression.lower, context)
            upper = self._eval_expr(expression.upper, context)
            result = compare_values(value, lower, ">=") and compare_values(value, upper, "<=")
            if result is None:
                return None
            return not result if expression.negated else result
        if isinstance(expression, FunctionCall):
            try:
                return self._eval_function(expression, context)
            except (TypeError, ValueError, IndexError, OverflowError) as exc:
                raise _with_node_location(ExecutionError(f"函数 {expression.name} 执行失败: {exc}"), expression) from exc
        raise _with_node_location(ExecutionError(f"不支持表达式 {type(expression).__name__}"), expression)

    def _eval_function(self, function: FunctionCall, context: dict[str, object]) -> object:
        name = function.name.lower()
        group = context.get("__group__")
        if name in _AGGREGATE_NAMES and isinstance(group, list):
            if name == "count":
                if not function.args or isinstance(function.args[0], Star):
                    return len(group)
                values = [self._eval_expr(function.args[0], item) for item in group]
                return len({value for value in values if value is not None}) if function.distinct else sum(value is not None for value in values)
            values = [self._eval_expr(function.args[0], item) for item in group] if function.args else []
            values = [value for value in values if value is not None]
            if function.distinct:
                values = list(dict.fromkeys(values))
            if not values:
                return None
            if name == "sum":
                return sum(values)
            if name == "avg":
                return sum(values) / len(values)
            if name == "min":
                return min(values)
            return max(values)
        values = [self._eval_expr(argument, context) for argument in function.args]
        if name in {"lower", "upper", "length", "len", "abs"} and not values:
            raise _with_node_location(ExecutionError(f"函数 {function.name} 缺少参数"), function)
        if name == "lower":
            return str(values[0]).lower() if values and values[0] is not None else None
        if name == "upper":
            return str(values[0]).upper() if values and values[0] is not None else None
        if name in {"length", "len"}:
            return len(str(values[0])) if values and values[0] is not None else None
        if name == "abs":
            return abs(values[0]) if values and values[0] is not None else None
        if name == "coalesce":
            return next((value for value in values if value is not None), None)
        if name == "date":
            return _eval_date_function(values, function)
        raise _with_node_location(ExecutionError(f"不支持函数 {function.name}"), function)

    def _contains_aggregate(self, expression: Expr | None) -> bool:
        if expression is None:
            return False
        if isinstance(expression, FunctionCall):
            return expression.name.lower() in _AGGREGATE_NAMES or any(self._contains_aggregate(argument) for argument in expression.args)
        if isinstance(expression, UnaryOp):
            return self._contains_aggregate(expression.operand)
        if isinstance(expression, BinaryOp):
            return self._contains_aggregate(expression.left) or self._contains_aggregate(expression.right)
        if isinstance(expression, IsNull):
            return self._contains_aggregate(expression.expression)
        if isinstance(expression, InPredicate):
            return self._contains_aggregate(expression.expression) or any(self._contains_aggregate(value) for value in expression.values)
        if isinstance(expression, BetweenPredicate):
            return any(self._contains_aggregate(value) for value in (expression.expression, expression.lower, expression.upper))
        return False

    def _output_names(self, statement: Select) -> list[str]:
        names: list[str] = []
        table_refs: list[TableRef] = []
        if statement.from_table is not None:
            table_refs.append(statement.from_table)
        table_refs.extend(join.table for join in statement.joins)
        for item in statement.items:
            if isinstance(item.expression, Star):
                selected = table_refs
                if item.expression.table:
                    selected = [ref for ref in table_refs if ref.name.lower() == item.expression.table.lower() or (ref.alias or "").lower() == item.expression.table.lower()]
                for ref in selected:
                    relation = self.catalog.get_relation(ref.name)
                    names.extend(column.name for column in relation.schema)
            elif item.alias:
                names.append(item.alias)
            elif isinstance(item.expression, ColumnRef):
                names.append(item.expression.name)
            elif isinstance(item.expression, FunctionCall):
                names.append(item.expression.name.lower())
            else:
                names.append(type(item.expression).__name__.lower())
        return names

    def _uses_index(self, statement: Select) -> bool:
        if statement.from_table is None:
            return False
        relation = self.catalog.find_table(statement.from_table.name, include_system=True)
        if relation is None:
            relation = self.catalog.find_view(statement.from_table.name)
        if not isinstance(relation, TableMetadata):
            return False
        return self._candidate_row_ids(relation, statement.from_table, statement.where) is not None

    def health(self) -> dict[str, object]:
        return {"status": "ok" if not self._closed else "closed", "path": str(self.path), "page_count": self.disk.page_count, "tables": len(self.catalog)}

    def metrics(self) -> dict[str, object]:
        return {
            "health": self.health(),
            "buffer_pool": self.buffer_pool.stats(),
            "catalog": {"tables": len(self.catalog), "indexes": len(self.catalog.indexes())},
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

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
