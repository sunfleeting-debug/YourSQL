"""SQL 语句命令的授权、DDL、DML 和索引变更处理。"""
#处理SQL语句的授权、DDL、DML和索引变更
from __future__ import annotations

import re
from dataclasses import dataclass, fields
from typing import Callable, Iterable

from yoursql.common import (
    AuthorizationError,
    BinderError,
    CatalogError,
    Column,
    DataType,
    ExecutionError,
    ExecutionResult,
    Schema,
    TransactionError,
    YourSQLError,
    Value,
    sql_truth,
)
from yoursql.common.types import PageId, RowId
from yoursql.planner.logical import plan_from_statement
from yoursql.planner.physical import PlanNode
from yoursql.sql.ast import (
    BetweenPredicate,
    BeginTransaction,
    BinaryOp,
    ColumnDefinition,
    ColumnRef,
    Commit,
    CreateIndex,
    CreateRole,
    CreateTable,
    CreateUser,
    CreateView,
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
    Revoke,
    Rollback,
    Select,
    SetTransaction,
    Show,
    ShowGrants,
    Star,
    Statement,
    Subquery,
    TableRef,
    UnaryOp,
    Update,
)
from yoursql.sql.binder import BoundStatement
from yoursql.sql.lexer import KEYWORDS
from yoursql.sql.parser import Parser
from yoursql.storage import IndexPayloadEntry, TableHeap
from yoursql.engine.catalog import IndexMetadata, TableMetadata, ViewMetadata


@dataclass(frozen=True)
class _UpdateTarget:
    """UPDATE 先收集的行位置、旧值和新值。"""

    row_id: RowId
    old_row: tuple[object, ...]
    new_row: tuple[object, ...]


@dataclass(frozen=True)
class _DeleteTarget:
    """DELETE 先收集的行位置和原始行值。"""

    row_id: RowId
    row: tuple[object, ...]


@dataclass(frozen=True)
class _IndexedRow:
    """批量维护索引时已写入堆表的行。"""

    row: tuple[object, ...]
    row_id: RowId


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


def _with_node_location(error: YourSQLError, node: Node | None) -> YourSQLError:
    """把 SQL 内部异常补到 AST 节点。"""

    return _with_location(error, node.source_location if node is not None else None)


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


class DatabaseCommandMixin:
    """实现 SQL 命令；存储、目录和会话依赖由 Database 提供。"""

    # ----- 语句路由、对象定位与权限检查 -----
    @staticmethod
    def _action_for(statement: Statement) -> str:
        """根据语句类型确定所需的权限动作。"""
        if isinstance(statement, (CreateRole, CreateUser, Grant, Revoke)):
            return "SECURITY"
        if isinstance(statement, ShowGrants):
            return "SHOW_GRANTS"
        if isinstance(
            statement, (BeginTransaction, Commit, Rollback, SetTransaction)
        ):
            return "TRANSACTION"
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
        """根据语句提取受影响的对象名称。"""
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
            return DatabaseCommandMixin._object_for(statement.statement)
        # HOW: SELECT 交给 query.py；写命令经 _mutate 正常执行后统一保存。
        if isinstance(statement, Select):
            names: list[str] = []
            if statement.from_table is not None:
                names.append(statement.from_table.name)
            names.extend(join.table.name for join in statement.joins)
            if statement.union is not None:
                union_name = DatabaseCommandMixin._object_for(statement.union)
                if union_name:
                    names.append(union_name)
            return ",".join(names) or None
        return None

    def _authorize_statement(
        self, statement: Statement, action: str, view_stack: tuple[str, ...] = ()
    ) -> None:
        """按语句涉及的对象逐个检查权限，避免多表查询绕过对象级授权。"""

        if isinstance(statement, (CreateRole, CreateUser, Grant, Revoke)):
            try:
                self.session.authorize("SECURITY")
            except AuthorizationError as exc:
                raise _with_node_location(exc, statement) from exc
            return
        if isinstance(
            statement, (BeginTransaction, Commit, Rollback, SetTransaction)
        ):
            # WHY：事务控制语句只影响当前会话的运行状态，不触碰任何数据对象，
            # 因此不做对象级授权——否则普通用户连 BEGIN 都会被拦下。
            return
        if isinstance(statement, ShowGrants):
            if statement.target_kind is None:
                return
            if (
                statement.target_kind.upper() == "USER"
                and statement.target_name
                and statement.target_name.lower() == self.session.user.name.lower()
            ):
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
                    raise _with_location(
                        exc, self._object_location(statement, object_name)
                    ) from exc
                if action.upper() == "SELECT":
                    view = self.catalog.find_view(object_name)
                    if (
                        view is not None
                        and view.system
                        and not self.system_catalog.is_admin()
                    ):
                        raise _with_location(
                            AuthorizationError("系统视图仅对 admin 开放"),
                            self._object_location(statement, object_name),
                        )
                    view_key = object_name.lower()
                    if view is not None and view_key not in view_stack:
                        # HOW：采用调用者权限；访问视图还要拥有其底层表/视图的 SELECT 权限。
                        self._authorize_statement(
                            self._view_query(view), "SELECT", (*view_stack, view_key)
                        )
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
        """递归收集语句中的子查询。"""
        for descriptor in fields(node):
            value = getattr(node, descriptor.name)
            values = value if isinstance(value, tuple) else (value,)
            for child in values:
                if isinstance(child, Subquery):
                    yield child.query
                elif isinstance(child, Node):
                    yield from DatabaseCommandMixin._subqueries(child)

    @staticmethod
    def _object_names(statement: Statement) -> tuple[str, ...]:
        """提取语句涉及的对象名称。"""
        if isinstance(statement, Explain):
            return DatabaseCommandMixin._object_names(statement.statement)
        if isinstance(statement, Show) and statement.object_name is not None:
            return (statement.object_name,)
        if isinstance(statement, Select):
            names: list[str] = []
            if statement.from_table is not None:
                names.append(statement.from_table.name)
            names.extend(join.table.name for join in statement.joins)
            if statement.union is not None:
                names.extend(DatabaseCommandMixin._object_names(statement.union))
            return tuple(dict.fromkeys(names))
        for attribute in ("table", "name"):
            value = getattr(statement, attribute, None)
            if isinstance(value, str):
                return (value,)
        return ()

    @staticmethod
    def _object_location(
        statement: Statement, object_name: str
    ) -> tuple[int, int] | None:
        """返回语句中对象名的 Token 位置，供权限错误复用。"""

        if isinstance(statement, Explain):
            return DatabaseCommandMixin._object_location(
                statement.statement, object_name
            )
        if isinstance(statement, Select):
            references = ([statement.from_table] if statement.from_table else []) + [
                join.table for join in statement.joins
            ]
            for reference in references:
                if reference.name.lower() == object_name.lower():
                    return reference.source_location
        if isinstance(statement, CreateIndex):
            return statement.source_location_for("table") or statement.source_location
        if isinstance(statement, (Insert, Update, Delete)):
            return statement.source_location_for("table") or statement.source_location
        return statement.source_location

    # ----- 语句分发：命令实现只在这里汇合 -----
    def _execute_statement(
        self, statement: Statement, bound: BoundStatement, plan: PlanNode
    ) -> ExecutionResult:
        """按语句类型执行 DDL、DML、DCL 或查询命令。"""
        if isinstance(statement, BeginTransaction):
            txn = self.begin_transaction(statement.isolation)
            return ExecutionResult(
                message=f"BEGIN {txn.isolation} (txn {txn.txn_id})",
                stats={"transaction": txn.stats()},
            )
        if isinstance(statement, Commit):
            stats = self.commit_transaction()
            return ExecutionResult(
                message=f"COMMIT {stats['txn_id']}", stats={"transaction": stats}
            )
        if isinstance(statement, Rollback):
            stats = self.rollback_transaction()
            return ExecutionResult(
                message=f"ROLLBACK {stats['txn_id']}", stats={"transaction": stats}
            )
        if isinstance(statement, SetTransaction):
            level = self.set_default_isolation(statement.isolation)
            return ExecutionResult(
                message=f"SET TRANSACTION ISOLATION LEVEL {level.upper()}",
                stats={"default_isolation": level},
            )
        if isinstance(statement, Explain):
            child = (
                plan.children[0]
                if plan.children
                else plan_from_statement(statement.statement)
            )
            return ExecutionResult(
                columns=("plan",),
                rows=[(child.explain(),)],
                plan=child.to_dict(),
                message="EXPLAIN",
            )
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
                user = self.rbac.create_user(
                    statement.name, statement.password, roles=statement.roles
                )
            except AuthorizationError as exc:
                raise _with_node_location(exc, statement) from exc
            self._persist_rbac()
            return ExecutionResult(message=f"CREATE USER {user.name}")
        if isinstance(statement, Grant):
            keys = self._permission_keys(statement.privileges, statement.object_name)
            try:
                self._grant_permissions(
                    keys, statement.target_kind, statement.target_name
                )
            except AuthorizationError as exc:
                raise _with_location(
                    exc,
                    statement.source_location_for("target")
                    or statement.source_location,
                ) from exc
            self._persist_rbac()
            return ExecutionResult(
                affected_rows=len(keys), message=f"GRANT {len(keys)}"
            )
        if isinstance(statement, Revoke):
            keys = self._permission_keys(statement.privileges, statement.object_name)
            try:
                self._revoke_permissions(
                    keys, statement.target_kind, statement.target_name
                )
            except AuthorizationError as exc:
                raise _with_location(
                    exc,
                    statement.source_location_for("target")
                    or statement.source_location,
                ) from exc
            self._persist_rbac()
            return ExecutionResult(
                affected_rows=len(keys), message=f"REVOKE {len(keys)}"
            )
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
    def _permission_keys(
        privileges: tuple[str, ...], object_name: str | None
    ) -> tuple[str, ...]:
        """将语句转换为需要检查的权限键集合。"""
        keys: list[str] = []
        for privilege in privileges:
            action = "*" if privilege.upper() in {"ALL", "*"} else privilege.upper()
            keys.append(action if object_name is None else f"{action} {object_name}")
        return tuple(keys)

    def _grant_permissions(
        self, privileges: tuple[str, ...], target_kind: str, target_name: str
    ) -> None:
        """执行 GRANT 命令并持久化权限变化。"""
        if target_kind.upper() == "ROLE":
            for privilege in privileges:
                self.rbac.grant(privilege, role=target_name)
            return
        for privilege in privileges:
            self.rbac.grant(privilege, user=target_name)

    def _revoke_permissions(
        self, privileges: tuple[str, ...], target_kind: str, target_name: str
    ) -> None:
        """执行 REVOKE 命令并持久化权限变化。"""
        if target_kind.upper() == "ROLE":
            for privilege in privileges:
                self.rbac.revoke(privilege, role=target_name)
            return
        for privilege in privileges:
            self.rbac.revoke(privilege, user=target_name)

    def _show_grants(self, statement: ShowGrants) -> ExecutionResult:
        """生成指定主体的权限列表。"""
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
            raise _with_location(
                exc,
                statement.source_location_for("target") or statement.source_location,
            ) from exc
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
            views = tuple(
                view
                for view in self.catalog.views()
                if not view.system or self.system_catalog.is_admin()
            )
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
                rows.append(
                    (
                        column.name,
                        column.data_type.value,
                        "YES" if column.nullable else "NO",
                        key,
                        default,
                    )
                )
            return ExecutionResult(
                columns=("field", "type", "null", "key", "default"),
                rows=rows,
                message=f"SHOW COLUMNS {relation.name}",
            )
        if target == "INDEX":
            if table is None:
                return ExecutionResult(
                    columns=(
                        "table_name",
                        "index_name",
                        "unique",
                        "index_type",
                        "columns",
                    ),
                    rows=[],
                    message=f"SHOW INDEX {relation.name}",
                )
            rows = [
                (
                    table.name,
                    metadata.name,
                    metadata.unique,
                    metadata.index_type,
                    ", ".join(metadata.columns),
                )
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
                raise _with_node_location(
                    ExecutionError(f"{relation.name!r} 是视图，不是表"), statement
                )
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
                    definition.extend(
                        ("DEFAULT", _sql_literal(column.default.unwrap()))
                    )
                definitions.append(" ".join(definition))
            statement_text = f"CREATE TABLE {_sql_identifier(table.name)} ({', '.join(definitions)});"
            return ExecutionResult(
                columns=("table_name", "create_statement"),
                rows=[(table.name, statement_text)],
                message=f"SHOW CREATE TABLE {table.name}",
            )
        if target == "CREATE_VIEW":
            if not isinstance(relation, ViewMetadata):
                raise _with_node_location(
                    ExecutionError(f"{relation.name!r} 是表，不是视图"), statement
                )
            statement_text = f"CREATE VIEW {_sql_identifier(relation.name)} AS {relation.definition_sql};"
            return ExecutionResult(
                columns=("view_name", "create_statement"),
                rows=[(relation.name, statement_text)],
                message=f"SHOW CREATE VIEW {relation.name}",
            )
        raise ExecutionError(f"不支持 SHOW {statement.target}")

    # ----- DDL/DML 事务边界与目录失效 -----
    def _mutate(self, operation: Callable[[], ExecutionResult]) -> ExecutionResult:
        """执行写操作；在显式事务内只改内存与缓冲池，持久化推迟到 COMMIT。

        WHY：事务的原子性依赖"回滚还能把改动撤掉"。如果每条语句都立刻
        ``flush_all`` + 落目录，一旦后面 ROLLBACK，磁盘上已经留下了无法解释的中间态；
        改为提交时统一落盘，回滚只需恢复页前像即可。
        """

        # HOW: 先执行写操作，再保存目录和刷新页面；这里不提供完整事务回滚。
        result = operation()
        txn = self.current_transaction()
        if txn is not None:
            self._refresh_statistics()
            self._invalidate_candidate_cache()
            return result
        self._persist_catalog()
        self.buffer_pool.flush_all()
        self._refresh_statistics()
        # WHY：写入会改变索引候选集，缓存必须失效，否则可能用旧候选集少扫/多扫行。
        self._invalidate_candidate_cache()
        return result

    def _invalidate_candidate_cache(self) -> None:
        """清空索引候选集缓存。"""

        self._candidate_cache.clear()

    # ----- 表、视图和索引的 DDL -----
    # HOW: CREATE 入口：查重 -> 构造 Schema -> 登记 Catalog；此处不分配用户数据页。
    # 创建表.先创建表，再创建索引
    def _create_table(self, statement: CreateTable) -> ExecutionResult:
        """解析或创建表定义。"""
        if self.catalog.find_table(statement.name) is not None:
            if statement.if_not_exists:
                return ExecutionResult(message=f"table {statement.name} already exists")
            raise _with_node_location(
                CatalogError(f"表 {statement.name!r} 已存在"), statement
            )
        columns: list[Column] = []
        for definition in statement.columns:
            default = self._default_value(definition)
            columns.append(
                Column(
                    definition.name,
                    definition.data_type,
                    definition.nullable,
                    definition.primary_key,
                    definition.unique,
                    default,
                )
            )
        table = self.catalog.create_table(statement.name, Schema.from_iterable(columns))
        return ExecutionResult(affected_rows=0, message=f"CREATE TABLE {table.name}")

    def _create_view(
        self, statement: CreateView, bound: BoundStatement
    ) -> ExecutionResult:
        """保存视图定义和输出模式；不创建数据页，因此视图天然只读。"""

        if (
            self.catalog.find_table(statement.name, include_system=True) is not None
            or self.catalog.find_view(statement.name) is not None
        ):
            if statement.if_not_exists:
                return ExecutionResult(message=f"view {statement.name} already exists")
            raise _with_node_location(
                CatalogError(f"表或视图 {statement.name!r} 已存在"), statement
            )
        definition_sql = statement.definition_sql.strip() or self._render_select(
            statement.query
        )
        schema = self._view_schema(statement.query, bound.output_columns)
        view = self.catalog.create_view(statement.name, schema, definition_sql)
        return ExecutionResult(message=f"CREATE VIEW {view.name}")

    def _drop_view(self, statement: DropView) -> ExecutionResult:
        """删除视图并清理目录定义。"""
        view = self.catalog.find_view(statement.name)
        if view is None:
            if statement.if_exists:
                return ExecutionResult(message=f"view {statement.name} does not exist")
            raise _with_node_location(
                CatalogError(f"视图 {statement.name!r} 不存在"), statement
            )
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

        refs = ([query.from_table] if query.from_table else []) + [
            join.table for join in query.joins
        ]
        columns: list[Column] = []
        column_sources: list[Node] = []
        output_index = 0
        for item in query.items:
            if isinstance(item.expression, Star):
                selected = refs
                if item.expression.table:
                    selected = [
                        ref
                        for ref in refs
                        if item.expression.table.lower()
                        in {ref.name.lower(), (ref.alias or "").lower()}
                    ]
                for ref in selected:
                    relation = self.catalog.get_relation(ref.name)
                    for source_column in relation.schema:
                        name = (
                            output_names[output_index]
                            if output_index < len(output_names)
                            else source_column.name
                        )
                        columns.append(Column(name, source_column.data_type))
                        column_sources.append(item)
                        output_index += 1
                continue
            name = (
                output_names[output_index]
                if output_index < len(output_names)
                else item.alias or self._expression_name(item.expression)
            )
            columns.append(
                Column(name, self._view_expression_type(item.expression, refs))
            )
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
            duplicate_item = (
                column_sources[duplicate_index]
                if duplicate_index is not None and duplicate_index < len(column_sources)
                else None
            )
            raise _with_node_location(
                BinderError("视图输出列名重复，请为列添加唯一别名"), duplicate_item
            ) from exc

    def _view_expression_type(self, expression: Expr, refs: list[TableRef]) -> DataType:
        """推断视图表达式的输出数据类型。"""
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
            return (
                DataType.BOOLEAN
                if expression.operator.upper() == "NOT"
                else self._view_expression_type(expression.operand, refs)
            )
        if isinstance(expression, BinaryOp):
            if expression.operator.upper() in {
                "AND",
                "OR",
                "=",
                "==",
                "!=",
                "<>",
                "<",
                "<=",
                ">",
                ">=",
                "LIKE",
                "||",
            }:
                return (
                    DataType.BOOLEAN
                    if expression.operator.upper() != "||"
                    else DataType.VARCHAR
                )
            left = self._view_expression_type(expression.left, refs)
            right = self._view_expression_type(expression.right, refs)
            return DataType.FLOAT if DataType.FLOAT in {left, right} else DataType.INT
        if isinstance(expression, (IsNull, InPredicate, BetweenPredicate)):
            return DataType.BOOLEAN
        return DataType.VARCHAR

    def _view_column_relation(
        self, expression: ColumnRef, refs: list[TableRef]
    ) -> TableMetadata | ViewMetadata:
        """解析视图输出列所属的关系。"""
        if expression.table:
            for ref in refs:
                if expression.table.lower() in {
                    ref.name.lower(),
                    (ref.alias or "").lower(),
                }:
                    return self.catalog.get_relation(ref.name)
            raise _with_node_location(
                BinderError(f"表或别名 {expression.table!r} 不存在"), expression
            )
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
            raise _with_node_location(
                BinderError(f"列 {expression.name!r} 不存在"), expression
            )
        raise _with_node_location(
            BinderError(f"列 {expression.name!r} 存在歧义，请使用表名限定"), expression
        )

    @staticmethod
    def _render_select(statement: Select) -> str:
        """为手工构造 AST 提供一个可持久化的最小 SQL 渲染器。"""

        def render_expr(expression: Expr) -> str:
            """将表达式渲染为可保存的 SQL 文本。"""
            if isinstance(expression, Literal):
                return _sql_literal(expression.value)
            if isinstance(expression, Star):
                return (
                    f"{_sql_identifier(expression.table)}.*"
                    if expression.table
                    else "*"
                )
            if isinstance(expression, ColumnRef):
                return (
                    f"{_sql_identifier(expression.table)}.{_sql_identifier(expression.name)}"
                    if expression.table
                    else _sql_identifier(expression.name)
                )
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
                negated = "NOT " if expression.negated else ""
                return (
                    f"{render_expr(expression.expression)} {negated}BETWEEN "
                    f"{render_expr(expression.lower)} AND {render_expr(expression.upper)}"
                )
            if isinstance(expression, InPredicate):
                values = ", ".join(render_expr(value) for value in expression.values)
                return f"{render_expr(expression.expression)} {'NOT ' if expression.negated else ''}IN ({values})"
            raise CatalogError(f"无法渲染视图表达式 {type(expression).__name__}")

        items = []
        for item in statement.items:
            text = render_expr(item.expression)
            items.append(
                f"{text} AS {_sql_identifier(item.alias)}" if item.alias else text
            )
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
        """计算列定义对应的默认值。"""
        if definition.default is None:
            return None
        if not isinstance(definition.default, Literal):
            raise _with_node_location(
                BinderError("DEFAULT 目前只支持字面量"), definition.default
            )
        try:
            return Value.infer(definition.default.value).coerce(definition.data_type)
        except BinderError as exc:
            raise _with_node_location(exc, definition.default) from exc

    def _drop_table(self, statement: DropTable) -> ExecutionResult:
        """删除表及其关联索引和存储页。"""
        table = self.catalog.find_table(statement.name)
        if table is None:
            if statement.if_exists:
                return ExecutionResult(message=f"table {statement.name} does not exist")
            raise _with_node_location(
                CatalogError(f"表 {statement.name!r} 不存在"), statement
            )
        if self.explicit_transaction() is not None:
            # WHY：DROP TABLE 会立刻把数据页归还空闲链表，页内容随即被覆盖；
            # 页级前像只覆盖"被写过"的页，救不回已被释放的页。因此显式事务内先禁止，
            # 避免给出"能回滚"的错觉。（自动提交下该语句自身仍是原子的。）
            raise _with_node_location(
                TransactionError(
                    "显式事务内不支持 DROP TABLE：数据页会被立即释放，无法回滚；"
                    "请先 COMMIT/ROLLBACK 再执行"
                ),
                statement,
            )
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

    # ----- 行级 DML -----
    # HOW: INSERT 入口：整理校验字段 -> 写堆表 -> 维护索引与元数据 -> 返回影响行数。
    def _insert(self, statement: Insert, bound: BoundStatement) -> ExecutionResult:
        # 查Catalog：知道这张表要求什么样的数据
        # 取得操作这张表记录的工具
        # HOW: Binder 提供插入列的位置，例如 (name,id) 对应表下标 (1,0)。
        # 确定 INSERT 提供的值分别属于哪一列
        """执行插入操作并维护关联状态。"""
        table = self.catalog.get_table(statement.table)
        heap = self._heap(table)
        insert_indexes = bound.insert_indexes or tuple(range(len(table.schema)))
        inserted = 0
        for row_index, expressions in enumerate(statement.values):#把表达式变成实际值
            supplied = [self._eval_expr(expression, {}) for expression in expressions]
            values: list[object] = []
            # HOW: 按列下标映射输入值，后续补遗漏列的默认值；显式 NULL 不替换。
            supplied_map = dict(zip(insert_indexes, supplied, strict=True))
            #按表的正式列顺序遍历
            for index, column in enumerate(table.schema):
                values.append(
                    supplied_map.get(
                        index,
                        column.default.unwrap() if column.default is not None else None,
                    )
                )
            row_location = statement.source_location_for(f"row:{row_index}")
            try:
                #校验：这条记录能不能放进这张表
                row = table.schema.validate_row(tuple(values))
            except YourSQLError as exc:
                raise _with_location(exc, row_location) from exc
            self._check_constraints(table, row, None, location=row_location)
            #真正写进页面
            # HOW: 用返回的物理地址维护索引，并将新增页和行数同步到表元数据。
            row_id = heap.insert(row)
            # 维护索引：让索引也能找到新记录
            # WHY：只有堆表写入后才能得到最终页号和槽号；索引条目必须指向这个稳定的 RowId。
            self._update_indexes(table, row, row_id, insert=True)
            #⑨ 更新目录中的页号列表和行数
            table.page_ids = [PageId(page_id) for page_id in heap.page_ids]
            table.first_page_id = table.page_ids[0] if table.page_ids else None
            table.row_count += 1
            inserted += 1
        return ExecutionResult(affected_rows=inserted, message=f"INSERT {inserted}")

    def _update(self, statement: Update) -> ExecutionResult:
        """执行更新操作并维护关联状态。"""
        table = self.catalog.get_table(statement.table)
        heap = self._heap(table)
        targets: list[_UpdateTarget] = []
        for record in heap.scan():
            context = self._table_context(
                TableRef(table.name), record.row, record.row_id, table
            )
            if statement.where is not None and not sql_truth(
                self._eval_expr(statement.where, context)
            ):
                continue
            values = list(record.row)
            for column_name, expression in statement.assignments:
                values[table.schema.index(column_name)] = self._eval_expr(
                    expression, context
                )
            assignment_location = statement.source_location_for("assignment:0")
            try:
                new_row = table.schema.validate_row(tuple(values))
            except YourSQLError as exc:
                raise _with_location(exc, assignment_location) from exc
            self._check_constraints(
                table, new_row, record.row_id, location=assignment_location
            )
            targets.append(_UpdateTarget(record.row_id, record.row, new_row))
        for target in targets:
            # WHY：更新可能改变索引键或覆盖索引携带的列值，因此先移除旧入口，
            # 堆表更新成功后再按新行值建立入口；失败时下面的异常分支恢复两者。
            self._update_indexes(table, target.old_row, target.row_id, insert=False)
            heap.update(target.row_id, target.new_row)
            try:
                self._update_indexes(
                    table, target.new_row, target.row_id, insert=True
                )
            except Exception:
                heap.update(target.row_id, target.old_row)
                self._update_indexes(
                    table, target.old_row, target.row_id, insert=True
                )
                raise
        return ExecutionResult(
            affected_rows=len(targets), message=f"UPDATE {len(targets)}"
        )

    # HOW: 先收集符合 WHERE 的地址和原记录，再删除索引与堆记录，避免边扫边改。
    def _delete(self, statement: Delete) -> ExecutionResult:
        # 查目录，取得表堆
        # 扫描记录，判断 WHERE
        # 如果没有where就走这个
        # 先收集目标，不立即删除
        # 先维护索引，再删除堆记录
        # insert=False 表示删除索引条目
        # 按页号和槽号删除
        """执行删除操作并维护关联状态。"""
        table = self.catalog.get_table(statement.table)
        heap = self._heap(table)
        targets: list[_DeleteTarget] = []
        for record in heap.scan():
            context = self._table_context(
                TableRef(table.name), record.row, record.row_id, table
            )
            if statement.where is None or sql_truth(
                self._eval_expr(statement.where, context)
            ):
                targets.append(_DeleteTarget(record.row_id, record.row))
        touched_page_ids: set[int] = set()
        for target in targets:
            self._update_indexes(table, target.row, target.row_id, insert=False)
            heap.delete(target.row_id)
            touched_page_ids.add(int(target.row_id.page_id))
            table.row_count = max(0, table.row_count - 1)
        heap.reclaim_empty_pages(touched_page_ids)
        table.page_ids = [PageId(page_id) for page_id in heap.page_ids]
        table.first_page_id = table.page_ids[0] if table.page_ids else None
        return ExecutionResult(
            affected_rows=len(targets), message=f"DELETE {len(targets)}"
        )

    def _flush_batch(
        self,
        table: TableMetadata,
        heap: TableHeap,
        rows: list[tuple[object, ...]],
        pending_entries: dict[str, list[IndexPayloadEntry]]
        | None = None,
        written_row_ids: list[RowId] | None = None,
    ) -> int:
        """批量写一页组记录，再维护索引；返回写入行数。

        WHY：批量写页会让“索引维护失败”影响整批行，因此失败时必须把本批已写入的堆行
        连同已插入的索引条目一起回滚，否则会出现堆里有行、索引里没有的不可见数据。
        """

        row_ids = heap.append_batch(rows)
        if written_row_ids is not None:
            written_row_ids.extend(row_ids)
        if pending_entries:
            # HOW：空索引改为只登记条目，装载结束后一次性 bulk_load。
            for metadata in self.catalog.indexes():
                if (
                    metadata.table_id != table.table_id
                    or metadata.name not in pending_entries
                ):
                    continue
                pending_entries[metadata.name].extend(
                    IndexPayloadEntry(
                        self._index_key(table, metadata, row),
                        row_id,
                        list(self._index_payload(table, metadata, row)),
                    )
                    for row, row_id in zip(rows, row_ids, strict=True)
                    if not (
                        metadata.unique
                        and any(
                            value is None
                            for value in self._index_key(table, metadata, row)
                        )
                    )
                )
            return len(row_ids)
        indexed: list[_IndexedRow] = []
        try:
            for row, row_id in zip(rows, row_ids, strict=True):
                self._update_indexes(table, row, row_id, insert=True)
                indexed.append(_IndexedRow(row, row_id))
        except Exception:
            for indexed_row in indexed:
                self._update_indexes(
                    table,
                    indexed_row.row,
                    indexed_row.row_id,
                    insert=False,
                )
            for row_id in row_ids:
                heap.delete(row_id)
            raise
        return len(row_ids)

    def _bulk_build_indexes(
        self,
        table: TableMetadata,
        heap: TableHeap,
        fresh_indexes: list[IndexMetadata],
        pending_entries: dict[str, list[IndexPayloadEntry]],
        written_row_ids: list[RowId],
    ) -> None:
        """把装载期间登记的索引入口一次性建树；失败时回滚本次写入的堆行。"""

        try:
            for metadata in fresh_indexes:
                self.index_manager.get(metadata.name).bulk_load(
                    pending_entries[metadata.name]
                )
        except Exception:
            # WHY：bulk_load 的唯一性校验在中途报错时索引尚未建好，回滚堆行避免留下无索引数据。
            for row_id in written_row_ids:
                heap.delete(row_id)
            raise

    def _check_constraints(
        self,
        table: TableMetadata,
        row: tuple[object, ...],
        excluded: RowId | None,
        *,
        location: tuple[int, int] | None = None,
    ) -> None:
        """检查插入或更新行是否满足列和唯一性约束。"""
        indexed_unique_columns = {
            metadata.columns[0].lower()
            for metadata in self.catalog.indexes()
            if metadata.table_id == table.table_id
            and metadata.unique
            and len(metadata.columns) == 1
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
                    raise _with_location(
                        ExecutionError(f"列 {column.name} 的唯一约束冲突"), location
                    )
        for metadata in self.catalog.indexes():
            if metadata.table_id != table.table_id or not metadata.unique:
                continue
            key = self._index_key(table, metadata, row)
            if any(value is None for value in key):
                continue
            for row_id in self.index_manager.get(metadata.name).search(key):
                if excluded is None or row_id != excluded:
                    raise _with_location(
                        ExecutionError(f"索引 {metadata.name} 的唯一约束冲突"), location
                    )

    def _update_indexes(
        self,
        table: TableMetadata,
        row: tuple[object, ...],
        row_id: RowId,
        *,
        insert: bool,
    ) -> None:
        """根据行变更维护相关索引条目。"""
        for metadata in self.catalog.indexes():
            if metadata.table_id != table.table_id:
                continue
            tree = self.index_manager.get(metadata.name)
            key = self._index_key(table, metadata, row)
            if any(value is None for value in key) and metadata.unique:
                continue
            if insert:
                tree.insert(
                    key, row_id, self._index_payload(table, metadata, row) or None
                )
            else:
                tree.delete(key, row_id)

    @staticmethod
    def _index_key(
        table: TableMetadata, metadata: IndexMetadata, row: tuple[object, ...]
    ) -> tuple[object, ...]:
        """从一行数据构造索引键。"""
        return tuple(row[table.schema.index(column)] for column in metadata.columns)

    @staticmethod
    def _index_payload(
        table: TableMetadata, metadata: IndexMetadata, row: tuple[object, ...]
    ) -> tuple[object, ...]:
        """覆盖索引携带的列值（CREATE INDEX ... INCLUDE）；纯键索引为空。"""

        return tuple(
            row[table.schema.index(column)] for column in metadata.payload_columns
        )

    # ----- 索引维护 -----
    def _create_index(self, statement: CreateIndex) -> ExecutionResult:
        """解析或创建索引定义。"""
        if any(
            index.name.lower() == statement.name.lower()
            for index in self.catalog.indexes()
        ):
            if statement.if_not_exists:
                return ExecutionResult(message=f"index {statement.name} already exists")
            raise _with_node_location(
                CatalogError(f"索引 {statement.name!r} 已存在"), statement
            )
        try:
            table = self.catalog.get_table(statement.table)
        except CatalogError as exc:
            raise _with_location(exc, statement.source_location_for("table")) from exc
        metadata = IndexMetadata(
            statement.name,
            table.table_id,
            statement.columns,
            statement.unique,
            payload_columns=statement.include,
        )
        for column in statement.include:
            try:
                table.schema.index(column)
            except YourSQLError as exc:
                raise _with_node_location(
                    CatalogError(f"覆盖列 {column!r} 不存在"), statement
                ) from exc
        duplicates = {column.lower() for column in statement.columns} & {
            column.lower() for column in statement.include
        }
        if duplicates:
            raise _with_node_location(
                CatalogError(f"覆盖列不能与索引键重复: {sorted(duplicates)}"), statement
            )
        tree = self.index_manager.create(
            statement.name,
            unique=statement.unique,
            buffer_pool=self.buffer_pool,
            on_root_change=lambda page_id: setattr(
                metadata, "root_page_id", PageId(page_id)
            ),
        )
        try:
            tree.bulk_load(
                (
                    key,
                    record.row_id,
                    self._index_payload(table, metadata, record.row),
                )
                for record in self._heap(table).scan()
                for key in (self._index_key(table, metadata, record.row),)
                if not (metadata.unique and any(value is None for value in key))
            )
            self.catalog.create_index(metadata)
        except YourSQLError as exc:
            self.index_manager.drop(statement.name)
            tree.destroy()
            raise _with_location(
                exc,
                statement.source_location_for("column:0") or statement.source_location,
            ) from exc
        except Exception:
            self.index_manager.drop(statement.name)
            tree.destroy()
            raise
        return ExecutionResult(message=f"CREATE INDEX {statement.name}")

    def _drop_index(self, statement: DropIndex) -> ExecutionResult:
        """删除索引并释放其存储资源。"""
        try:
            metadata = self.catalog.get_index(statement.name)
        except CatalogError:
            if statement.if_exists:
                return ExecutionResult(message=f"index {statement.name} does not exist")
            raise _with_node_location(
                CatalogError(f"索引 {statement.name!r} 不存在"), statement
            )
        self.catalog.drop_index(statement.name)
        try:
            # WHY：先提交逻辑删除，再回收物理页；物理回收中断时最多留下孤儿页，
            # 不能让重启后的目录继续指向已经写成 FREE 的索引根页。
            self._persist_catalog()
            self.buffer_pool.flush_all()
        except Exception:
            # 目录写入失败时恢复当前进程的目录对象，物理索引仍由 manager 保留。
            self.catalog.create_index(metadata)
            raise
        tree = self.index_manager.drop(statement.name)
        if tree is not None:
            try:
                tree.destroy()
            finally:
                # 即使物理回收中途失败，内存中也不再暴露已逻辑删除的索引。
                self.index_manager.drop(statement.name)
        elif metadata.root_page_id is not None:
            # 兼容目录存在但进程内索引尚未加载的异常场景。
            self.buffer_pool.delete_page(int(metadata.root_page_id))
        return ExecutionResult(message=f"DROP INDEX {metadata.name}")
