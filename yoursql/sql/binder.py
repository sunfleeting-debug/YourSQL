"""基于目录只读接口的 SQL 语义绑定和基础校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..common.errors import BinderError
from ..common.types import Schema, Value
from .ast import (
    BetweenPredicate,
    BinaryOp,
    ColumnRef,
    CreateIndex,
    CreateView,
    CreateTable,
    Delete,
    Explain,
    Expr,
    FunctionCall,
    InPredicate,
    Insert,
    IsNull,
    Literal,
    Node,
    Select,
    Show,
    Star,
    Statement,
    Subquery,
    TableRef,
    UnaryOp,
    Update,
)


class TableProtocol(Protocol):
    """Binder 需要的最小表元数据接口。"""

    schema: Schema


class CatalogProtocol(Protocol):
    """Binder 需要的最小目录读接口。"""

    def get_table(self, name: str) -> TableProtocol: ...

    def get_relation(self, name: str) -> TableProtocol: ...


@dataclass(frozen=True)
class BoundStatement:
    """保留原 AST 和绑定后的输出列/插入列位置。"""

    statement: Statement
    output_columns: tuple[str, ...] = ()
    insert_indexes: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "statement": self.statement.to_dict(),
            "output_columns": list(self.output_columns),
            "insert_indexes": list(self.insert_indexes),
        }


class Binder:
    """完成表列存在性、值数量和字面量类型检查。"""

    def __init__(self, catalog: CatalogProtocol | None = None) -> None:
        self.catalog = catalog

    def bind(self, statement: Statement) -> BoundStatement:
        # 编译器可以在没有 Catalog 的情况下先生成 AST/计划；真正执行前再做语义绑定。
        if self.catalog is None:
            return BoundStatement(statement)
        if isinstance(statement, CreateTable):
            self._bind_create_table(statement)
            return BoundStatement(statement)
        if isinstance(statement, CreateView):
            bound = self._bind_select(statement.query)
            return BoundStatement(statement, bound.output_columns)
        if isinstance(statement, Insert):
            return self._bind_insert(statement)
        if isinstance(statement, Select):
            return self._bind_select(statement)
        if isinstance(statement, Update):
            table = self._table(statement.table, statement, "table")
            self._bind_assignments(statement.assignments, table.schema, statement)
            self._bind_expression(
                statement.where, self._relations((TableRef(statement.table),))
            )
            return BoundStatement(statement)
        if isinstance(statement, Delete):
            table = self._table(statement.table, statement, "table")
            self._bind_expression(
                statement.where, self._relations((TableRef(statement.table),))
            )
            return BoundStatement(statement)
        if isinstance(statement, CreateIndex):
            table = self._table(statement.table, statement, "table")
            for index, column in enumerate(statement.columns):
                try:
                    table.schema.column(column)
                except BinderError as exc:
                    raise self._located_error(
                        exc.message, statement, f"column:{index}"
                    ) from exc
            return BoundStatement(statement)
        if isinstance(statement, Show):
            if statement.object_name is not None:
                self._relation(statement.object_name, statement)
            return BoundStatement(statement)
        if isinstance(statement, Explain):
            return BoundStatement(
                statement, self.bind(statement.statement).output_columns
            )
        return BoundStatement(statement)

    def _bind_create_table(self, statement: CreateTable) -> None:
        seen: set[str] = set()
        for column in statement.columns:
            key = column.name.lower()
            if key in seen:
                raise self._located_error("CREATE TABLE 中列名重复", column)
            seen.add(key)
        primary_count = sum(column.primary_key for column in statement.columns)
        if primary_count > 1:
            raise self._located_error(
                "暂不支持多列 PRIMARY KEY，请使用 CREATE UNIQUE INDEX",
                statement,
                "primary:1",
            )
        for column in statement.columns:
            if column.default is not None and isinstance(column.default, Literal):
                try:
                    Value.infer(column.default.value).coerce(column.data_type)
                except BinderError as exc:
                    raise self._located_error(exc.message, column.default) from exc

    def _bind_insert(self, statement: Insert) -> BoundStatement:
        table = self._table(statement.table, statement, "table")
        schema: Schema = table.schema
        indexes: list[int] = []
        if statement.columns:
            seen: set[str] = set()
            for position, column in enumerate(statement.columns):
                key = column.lower()
                if key in seen:
                    raise self._located_error(
                        f"INSERT 列 {column!r} 重复", statement, f"column:{position}"
                    )
                seen.add(key)
                try:
                    indexes.append(schema.index(column))
                except BinderError as exc:
                    raise self._located_error(
                        exc.message, statement, f"column:{position}"
                    ) from exc
        else:
            indexes = list(range(len(schema)))
        for row_index, row in enumerate(statement.values):
            if len(row) != len(indexes):
                raise self._located_error(
                    f"INSERT 需要 {len(indexes)} 个值，实际得到 {len(row)} 个",
                    statement,
                    f"row:{row_index}",
                )
            for position, expression in enumerate(row):
                if isinstance(expression, Literal):
                    try:
                        Value.infer(expression.value).coerce(
                            schema.columns[indexes[position]].data_type
                        )
                    except BinderError as exc:
                        raise self._located_error(exc.message, expression) from exc
        return BoundStatement(statement, insert_indexes=tuple(indexes))

    def _bind_select(self, statement: Select) -> BoundStatement:
        relations = self._relations(self._all_table_refs(statement))
        for item in statement.items:
            self._bind_expression(item.expression, relations)
        self._bind_expression(statement.where, relations)
        for expression in statement.group_by:
            self._bind_expression(expression, relations)
        self._bind_expression(statement.having, relations)
        aliases = {item.alias.lower() for item in statement.items if item.alias}
        for item in statement.order_by:
            if (
                isinstance(item.expression, ColumnRef)
                and item.expression.table is None
                and item.expression.name.lower() in aliases
            ):
                continue
            self._bind_expression(item.expression, relations)
        if statement.union is not None:
            self._bind_select(statement.union)
        output: list[str] = []
        for item in statement.items:
            if isinstance(item.expression, Star):
                for alias, schema in relations:
                    if (
                        item.expression.table is None
                        or item.expression.table.lower() == alias.lower()
                    ):
                        output.extend(
                            f"{alias}.{name}" if len(relations) > 1 else name
                            for name in schema.names()
                        )
            elif isinstance(item.expression, ColumnRef):
                output.append(item.alias or item.expression.name)
            else:
                output.append(item.alias or self._expression_name(item.expression))
        return BoundStatement(statement, tuple(output))

    def _all_table_refs(self, statement: Select) -> tuple[TableRef, ...]:
        refs: list[TableRef] = []
        if statement.from_table is not None:
            refs.append(statement.from_table)
        refs.extend(join.table for join in statement.joins)
        return tuple(refs)

    def _relations(self, refs: tuple[TableRef, ...]) -> tuple[tuple[str, Schema], ...]:
        relations: list[tuple[str, Schema]] = []
        for ref in refs:
            table = self._relation(ref.name, ref)
            alias = ref.alias or ref.name
            relations.append((alias, table.schema))
        return tuple(relations)

    def _relation(
        self, name: str, node: Node | None = None, key: str | None = None
    ) -> TableProtocol:
        if self.catalog is None:
            return _SchemaHolder(Schema.from_iterable(()))
        try:
            get_relation = getattr(self.catalog, "get_relation", None)
            return (
                get_relation(name)
                if get_relation is not None
                else self.catalog.get_table(name)
            )
        except Exception as exc:
            if isinstance(exc, BinderError):
                raise
            raise self._located_error(f"表或视图 {name!r} 不存在", node, key) from exc

    def _table(
        self, name: str, node: Node | None = None, key: str | None = None
    ) -> TableProtocol:
        if self.catalog is None:
            return _SchemaHolder(Schema.from_iterable(()))
        try:
            return self.catalog.get_table(name)
        except Exception as exc:
            if isinstance(exc, BinderError):
                raise
            raise self._located_error(f"表 {name!r} 不存在", node, key) from exc

    def _bind_assignments(
        self,
        assignments: tuple[tuple[str, Expr], ...],
        schema: Schema,
        statement: Update | None = None,
    ) -> None:
        for index, (name, expression) in enumerate(assignments):
            try:
                schema.column(name)
            except BinderError as exc:
                raise self._located_error(
                    exc.message, statement, f"assignment:{index}"
                ) from exc
            self._bind_expression(expression, (("", schema),))

    def _bind_expression(
        self, expression: Expr | None, relations: tuple[tuple[str, Schema], ...]
    ) -> None:
        if expression is None or isinstance(expression, (Literal, Star)):
            return
        if isinstance(expression, ColumnRef):
            self._resolve_column(expression, relations)
            return
        if isinstance(expression, FunctionCall):
            for argument in expression.args:
                self._bind_expression(argument, relations)
            return
        if isinstance(expression, UnaryOp):
            self._bind_expression(expression.operand, relations)
            return
        if isinstance(expression, BinaryOp):
            self._bind_expression(expression.left, relations)
            self._bind_expression(expression.right, relations)
            return
        if isinstance(expression, IsNull):
            self._bind_expression(expression.expression, relations)
            return
        if isinstance(expression, InPredicate):
            self._bind_expression(expression.expression, relations)
            for value in expression.values:
                if isinstance(value, Subquery):
                    self._bind_select(value.query)
                else:
                    self._bind_expression(value, relations)
            return
        if isinstance(expression, BetweenPredicate):
            self._bind_expression(expression.expression, relations)
            self._bind_expression(expression.lower, relations)
            self._bind_expression(expression.upper, relations)

    def _resolve_column(
        self, expression: ColumnRef, relations: tuple[tuple[str, Schema], ...]
    ) -> None:
        if not relations:
            raise self._located_error(
                f"列 {expression.qualified_name!r} 没有可绑定的表", expression
            )
        if expression.table:
            matching = [
                (alias, schema)
                for alias, schema in relations
                if alias.lower() == expression.table.lower()
            ]
            if not matching:
                raise self._located_error(
                    f"表或别名 {expression.table!r} 不存在", expression
                )
            try:
                matching[0][1].column(expression.name)
            except BinderError as exc:
                raise self._located_error(exc.message, expression) from exc
            return
        matches = [
            schema
            for _alias, schema in relations
            if any(column.name.lower() == expression.name.lower() for column in schema)
        ]
        if not matches:
            raise self._located_error(f"列 {expression.name!r} 不存在", expression)
        if len(matches) > 1:
            raise self._located_error(
                f"列 {expression.name!r} 存在歧义，请使用表名限定", expression
            )

    @staticmethod
    def _located_error(
        message: str, node: Node | None, key: str | None = None
    ) -> BinderError:
        """把语义错误绑定到触发它的 AST 节点；无位置节点继续走旧兜底。"""

        location = (
            node.source_location_for(key)
            if node is not None and key is not None
            else node.source_location
            if node is not None
            else None
        )
        if location is None:
            return BinderError(message)
        return BinderError(message, line=location[0], column=location[1])

    @staticmethod
    def _expression_name(expression: Expr) -> str:
        if isinstance(expression, FunctionCall):
            return expression.name.lower()
        if isinstance(expression, UnaryOp):
            return (
                f"{expression.operator} {Binder._expression_name(expression.operand)}"
            )
        return type(expression).__name__.lower()


class _SchemaHolder:
    """无目录编译时的空占位；带目录绑定会走真实表。"""

    def __init__(self, schema: Schema) -> None:
        self.schema = schema
