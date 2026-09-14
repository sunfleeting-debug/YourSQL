"""从绑定 AST 构造逻辑计划。"""

from __future__ import annotations

from dataclasses import dataclass

from ..sql.ast import (
    BeginTransaction,
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
    Grant,
    Insert,
    Revoke,
    Rollback,
    Select,
    SetTransaction,
    Show,
    ShowGrants,
    Statement,
    TableRef,
    Update,
)
from .physical import PlanNode


@dataclass(frozen=True)
class LogicalPlanNode(PlanNode):
    """编译器输出的逻辑计划节点。"""


LogicalPlan = LogicalPlanNode


def _source_node(reference: TableRef) -> LogicalPlanNode:
    """构造一个 FROM 来源的扫描节点。

    HOW：派生表/CTE 用独立的 ``DerivedScan`` 而不是 ``SeqScan``——执行层的
    ``_scan_plans`` 只把 SeqScan/IndexScan 当作堆表扫描，用错 kind 会让派生表
    混进按表名匹配的计划列表里。
    """

    if reference.is_derived:
        return LogicalPlanNode(
            "DerivedScan",
            {
                "table": reference.effective_name,
                "alias": reference.alias,
                "query": reference.query,
            },
        )
    return LogicalPlanNode(
        "SeqScan", {"table": reference.name, "alias": reference.alias}
    )


def _select_plan(statement: Select) -> LogicalPlanNode:
    """按 SQL 子句顺序组装逻辑计划，不在此处选择具体访问路径。"""

    # HOW：先建立扫描和连接，再依次叠加过滤、聚合、投影、排序与分页，
    # 让计划树结构直接对应 SQL 的数据处理阶段。
    if statement.from_table is None:
        root = LogicalPlanNode("Values", {"rows": 1})
    else:
        root = _source_node(statement.from_table)
        for join in statement.joins:
            right = _source_node(join.table)
            root = LogicalPlanNode(
                "Join", {"join_type": join.join_type, "on": join.on}, (root, right)
            )
        if statement.where is not None:
            root = LogicalPlanNode("Filter", {"predicate": statement.where}, (root,))
    if statement.group_by or statement.having is not None:
        root = LogicalPlanNode(
            "Aggregate",
            {"group_by": statement.group_by, "having": statement.having},
            (root,),
        )
    root = LogicalPlanNode(
        "Project", {"items": statement.items, "distinct": statement.distinct}, (root,)
    )
    if statement.order_by:
        root = LogicalPlanNode("Sort", {"order_by": statement.order_by}, (root,))
    if statement.limit is not None or statement.offset:
        root = LogicalPlanNode(
            "Limit", {"limit": statement.limit, "offset": statement.offset}, (root,)
        )
    return root


def plan_from_statement(statement: Statement) -> LogicalPlanNode:
    """把绑定 AST 映射为可解释的逻辑计划。"""

    if isinstance(statement, Select):
        root = _select_plan(statement)
    elif isinstance(statement, CreateTable):
        root = LogicalPlanNode(
            "CreateTable", {"table": statement.name, "columns": statement.columns}
        )
    elif isinstance(statement, CreateView):
        root = LogicalPlanNode(
            "CreateView", {"view": statement.name, "query": statement.query}
        )
    elif isinstance(statement, DropTable):
        root = LogicalPlanNode("DropTable", {"table": statement.name})
    elif isinstance(statement, DropView):
        root = LogicalPlanNode("DropView", {"view": statement.name})
    elif isinstance(statement, Insert):
        root = LogicalPlanNode(
            "Insert",
            {
                "table": statement.table,
                "columns": statement.columns,
                "rows": statement.values,
            },
        )
    elif isinstance(statement, Update):
        root = LogicalPlanNode(
            "Update",
            {
                "table": statement.table,
                "assignments": statement.assignments,
                "where": statement.where,
            },
        )
    elif isinstance(statement, Delete):
        root = LogicalPlanNode(
            "Delete", {"table": statement.table, "where": statement.where}
        )
    elif isinstance(statement, CreateIndex):
        root = LogicalPlanNode(
            "CreateIndex",
            {
                "index": statement.name,
                "table": statement.table,
                "columns": statement.columns,
                "unique": statement.unique,
            },
        )
    elif isinstance(statement, DropIndex):
        root = LogicalPlanNode("DropIndex", {"index": statement.name})
    elif isinstance(statement, CreateRole):
        root = LogicalPlanNode("CreateRole", {"role": statement.name})
    elif isinstance(statement, CreateUser):
        root = LogicalPlanNode(
            "CreateUser", {"user": statement.name, "roles": statement.roles}
        )
    elif isinstance(statement, Grant):
        root = LogicalPlanNode(
            "Grant",
            {
                "privileges": statement.privileges,
                "object": statement.object_name,
                "target": statement.target_name,
            },
        )
    elif isinstance(statement, Revoke):
        root = LogicalPlanNode(
            "Revoke",
            {
                "privileges": statement.privileges,
                "object": statement.object_name,
                "target": statement.target_name,
            },
        )
    elif isinstance(statement, Show):
        root = LogicalPlanNode(
            "Show", {"target": statement.target, "object": statement.object_name}
        )
    elif isinstance(statement, ShowGrants):
        root = LogicalPlanNode(
            "ShowGrants",
            {"target_kind": statement.target_kind, "target": statement.target_name},
        )
    elif isinstance(statement, BeginTransaction):
        root = LogicalPlanNode(
            "BeginTransaction",
            {"isolation": statement.isolation or "session default"},
        )
    elif isinstance(statement, Commit):
        root = LogicalPlanNode("Commit", {})
    elif isinstance(statement, Rollback):
        root = LogicalPlanNode("Rollback", {})
    elif isinstance(statement, SetTransaction):
        root = LogicalPlanNode(
            "SetTransaction", {"isolation": statement.isolation}
        )
    elif isinstance(statement, Explain):
        root = LogicalPlanNode(
            "Explain",
            {"statement": statement.statement},
            (plan_from_statement(statement.statement),),
        )
    else:
        root = LogicalPlanNode(type(statement).__name__, {})
    return LogicalPlanNode(root.kind, root.properties, root.children, statement)


__all__ = ["LogicalPlan", "LogicalPlanNode", "plan_from_statement"]
