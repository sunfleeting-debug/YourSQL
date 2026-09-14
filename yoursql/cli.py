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


def _format_result(result: ExecutionResult) -> str:
    """将执行结果格式化为命令行可读的文本。"""
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
    """构造命令行参数解析器。"""
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令行参数并启动对应的工作模式。"""
    load_dotenv()
    args = build_parser().parse_args(argv)
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
        ) as database:
            if args.stdio:
                return serve_ssh_stdio(database)
            sql = args.sql
            if args.file is not None:
                sql = args.file.read_text(encoding="utf-8")
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
