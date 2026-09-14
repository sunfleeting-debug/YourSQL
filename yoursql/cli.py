"""YourSQL 命令行：单条 SQL、脚本文件和交互模式。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .common.config import DEFAULT_DATABASE_PATH, DatabaseConfig, load_dotenv
from .common.errors import YourSQLError
from .common.types import ExecutionResult
from .engine.runtime.database import Database
from .engine.services.ssh import serve_ssh_stdio
from .planner.optimizer import Optimizer
from .planner.physical import PhysicalPlanNode
from .sql.parser import parse_recovering


def _render_plan(plan: PhysicalPlanNode, fmt: str) -> str:
    """把优化后的计划渲染成指定格式（文本 / Mermaid / DOT / JSON）。"""

    if fmt == "mermaid":
        return plan.to_mermaid()
    if fmt == "dot":
        return plan.to_dot()
    if fmt == "json":
        return plan.to_json()
    return plan.explain()


def _format_result(result: ExecutionResult) -> str:
    data = result.as_dict()
    if not data["columns"]:
        return str(data["message"] or f"affected rows: {data['affected_rows']}")
    columns = [str(column) for column in data["columns"]]
    rows = [
        ["NULL" if value is None else str(value) for value in row]
        for row in data["rows"]
    ]
    widths = [len(column) for column in columns]
    for row in rows:
        widths = [
            max(width, len(value)) for width, value in zip(widths, row, strict=True)
        ]
    separator = "+" + "+".join("-" * (width + 2) for width in widths) + "+"
    lines = [
        separator,
        "|"
        + "|".join(
            f" {column:<{width}} "
            for column, width in zip(columns, widths, strict=True)
        )
        + "|",
        separator,
    ]
    lines.extend(
        "|"
        + "|".join(
            f" {value:<{width}} " for value, width in zip(row, widths, strict=True)
        )
        + "|"
        for row in rows
    )
    lines.append(separator)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    load_dotenv()
    parser = argparse.ArgumentParser(description="YourSQL educational database")
    database_default = (
        os.getenv("YOURSQL_CLI_DATABASE")
        or os.getenv("YOURSQL_DATABASE")
        or str(DEFAULT_DATABASE_PATH)
    )
    parser.add_argument("--database", default=database_default, help="数据库文件路径")
    parser.add_argument("--sql", help="执行一条 SQL 或脚本")
    parser.add_argument("--file", type=Path, help="执行 SQL 文件")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--user", default=os.getenv("YOURSQL_CLI_USER", "admin"))
    parser.add_argument(
        "--password", default=os.getenv("YOURSQL_CLI_PASSWORD", "admin")
    )
    parser.add_argument("--stdio", action="store_true", help="以 SSH stdio 协议运行")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只做词法与语法检查：把脚本里的错误一次报全，不执行",
    )
    parser.add_argument(
        "--rules",
        action="store_true",
        help="列出全部优化规则（名称 / 阶段 / 说明）后退出",
    )
    parser.add_argument(
        "--disable-rule",
        action="append",
        default=[],
        metavar="NAME",
        help="临时关闭一条优化规则，可重复；用 --rules 查看可用名称",
    )
    parser.add_argument(
        "--plan",
        choices=("text", "mermaid", "dot", "json"),
        help="只编译并输出优化后的查询计划（不执行），支持文本 / Mermaid / DOT / JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    sql = args.sql
    if args.file is not None:
        try:
            sql = args.file.read_text(encoding="utf-8")
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.check:
        # WHY：语法检查是纯前端动作，不需要数据库（也就不该因为库文件损坏而失败）。
        if sql is None:
            print("--check 需要配合 --sql 或 --file 使用", file=sys.stderr)
            return 1
        outcome = parse_recovering(sql)
        if args.json:
            print(json.dumps(outcome.as_dict(), ensure_ascii=False))
        else:
            print(outcome.report(), file=sys.stdout if outcome.ok else sys.stderr)
        return 0 if outcome.ok else 1
    if args.rules:
        # WHY：规则清单是纯静态信息，同样不该依赖数据库文件。
        print(Optimizer().explain_rules())
        return 0
    try:
        database_config = DatabaseConfig.from_environment()
        detected_page_size = Database.detect_page_size(args.database)
        if detected_page_size is not None:
            # WHY：保留已有数据库的页格式；环境变量的页大小只影响新数据库。
            database_config = replace(database_config, page_size=detected_page_size)
        with Database(
            args.database,
            config=database_config,
            user=args.user,
            password=args.password,
            disabled_rules=args.disable_rule,
        ) as database:
            if args.plan is not None:
                # WHY：EXPLAIN 是可执行的 SQL，但"看计划"经常比"跑一次"更早发生；
                # 这里给出一条只编译、不执行的入口，方便演示和排查。
                if sql is None:
                    print("--plan 需要配合 --sql 或 --file 使用", file=sys.stderr)
                    return 1
                compilation = database.compile(sql)
                plan = compilation.optimized_plan
                if plan is None:
                    print("该语句没有可展示的执行计划", file=sys.stderr)
                    return 1
                print(_render_plan(plan, args.plan))
                return 0
            if args.stdio:
                return serve_ssh_stdio(database)
            if sql is not None:
                results = database.execute_script(sql)
                for result in results:
                    print(
                        json.dumps(result.as_dict(), ensure_ascii=False)
                        if args.json
                        else _format_result(result)
                    )
                return 0
            while True:
                try:
                    line = input("yoursql> ")
                except EOFError:
                    print()
                    return 0
                if line.strip().lower() in {"quit", "exit", "\\q"}:
                    return 0
                if not line.strip():
                    continue
                try:
                    result = database.execute(line)
                    print(
                        json.dumps(result.as_dict(), ensure_ascii=False)
                        if args.json
                        else _format_result(result)
                    )
                except YourSQLError as exc:
                    print(str(exc), file=sys.stderr)
    except (YourSQLError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
