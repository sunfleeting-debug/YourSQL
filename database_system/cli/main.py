"""MiniSQL 命令行入口。

用法：
    python -m database_system.cli.main                     # 交互式
    python -m database_system.cli.main --db test.db
    python -m database_system.cli.main --file demo.sql
    python -m database_system.cli.main --sql "SELECT * FROM t;"

交互模式内置命令（以 . 开头）：
    .help            显示帮助
    .tables          列出所有表
    .schema <表名>   查看表结构
    .plan on|off     是否显示执行计划（优化前后对比）
    .optimize on|off 是否启用优化器
    .stats           查看缓冲区命中率等统计
    .log             查看页面替换日志
    .policy LRU|FIFO 切换缓存替换策略
    .exit / .quit    退出
"""

from __future__ import annotations

import argparse
import os
import sys

# 支持直接以脚本方式运行：python database_system/cli/main.py
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from database_system.engine.database import Database
    from database_system.sql_compiler.lexer import format_tokens
    from database_system.utils.helpers import format_table
else:  # pragma: no cover
    from database_system.engine.database import Database
    from database_system.sql_compiler.lexer import format_tokens
    from database_system.utils.helpers import format_table

BANNER = r"""
  __  __ _       _  ____  _     _
 |  \/  (_)_ __ (_)/ ___|| |   | |
 | |\/| | | '_ \| |\___ \| |   | |
 | |  | | | | | | | ___) | |___| |___
 |_|  |_|_|_| |_|_||____/|_____|_____|
 MiniSQL — 编译原理 + 操作系统 + 数据库 综合实践
"""

HELP = """内置命令：
  .help              显示本帮助
  .tables            列出所有表
  .schema <表名>     查看表结构
  .plan on|off       显示执行计划（优化前后对比）
  .optimize on|off   启用 / 关闭优化器
  .tokens on|off     显示 Token 流
  .stats             缓冲区统计（命中率）
  .log               页面替换日志
  .policy LRU|FIFO   切换缓存替换策略
  .exit / .quit      退出

SQL 支持：CREATE TABLE / INSERT / SELECT / DELETE / UPDATE / DROP TABLE / EXPLAIN
输入可跨行，以分号 ';' 结束；注释支持 -- 与 /* */。
"""


class Shell:
    def __init__(self, db: Database, show_plan: bool = False, show_tokens: bool = False):
        self.db = db
        self.show_plan = show_plan
        self.show_tokens = show_tokens

    # ------------------------------ 主循环 ------------------------------

    def run(self) -> int:
        print(BANNER)
        print(f"数据库文件: {os.path.abspath(self.db.path)}")
        print("输入 .help 查看帮助，.exit 退出\n")
        buffer = ""
        while True:
            try:
                prompt = "MiniSQL> " if not buffer else "      ... "
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                break
            line = line.strip()
            if not buffer and line.startswith("."):
                if self._command(line):
                    break
                continue
            if not line:
                continue
            buffer = (buffer + " " + line).strip() if buffer else line
            if not _contains_semicolon(buffer):
                continue
            self._execute(buffer)
            buffer = ""
        self.db.close()
        return 0

    # ------------------------------ 内置命令 ------------------------------

    def _command(self, line: str) -> bool:
        """返回 True 表示退出。"""
        parts = line.split()
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in (".exit", ".quit", ".q"):
            return True
        if cmd == ".help":
            print(HELP)
        elif cmd == ".tables":
            names = self.db.tables()
            print("\n".join(names) if names else "(空)")
        elif cmd == ".schema":
            if not arg:
                print("用法: .schema <表名>")
            else:
                schema = self.db.schema(arg)
                print(schema if schema else f"表 '{arg}' 不存在")
        elif cmd == ".plan":
            self.show_plan = _toggle(self.show_plan, arg, "显示执行计划")
        elif cmd == ".tokens":
            self.show_tokens = _toggle(self.show_tokens, arg, "显示 Token 流")
        elif cmd == ".optimize":
            self.db.enable_optimizer = _toggle(self.db.enable_optimizer, arg, "优化器")
        elif cmd == ".stats":
            stats = self.db.buffer_stats()
            for k, v in stats.items():
                print(f"  {k:<12} {v}")
        elif cmd == ".log":
            log = self.db.buffer.log
            print("\n".join(log[-50:]) if log else "(无日志)")
        elif cmd == ".policy":
            if arg.upper() in ("LRU", "FIFO"):
                self.db.buffer.set_policy(arg.upper())
                print(f"缓存替换策略已切换为 {arg.upper()}")
            else:
                print("用法: .policy LRU|FIFO")
        else:
            print(f"未知命令: {cmd}（输入 .help 查看帮助）")
        return False

    # ------------------------------ 执行 ------------------------------

    def _execute(self, sql: str) -> None:
        for result in self.db.execute(sql):
            self._print_result(result)

    def _print_result(self, result) -> None:
        if result.error is not None:
            print(f"{result.error}  [{result.stage} 阶段]")
            return
        if self.show_tokens and result.tokens:
            print("--- Token 流 ---")
            print(format_tokens(result.tokens))
        if self.show_plan and result.plan_after:
            print("--- AST ---")
            print(result.ast_tree)
            print("--- 逻辑计划（优化前）---")
            print(result.plan_before)
            print("--- 逻辑计划（优化后）---")
            print(result.plan_after)
            if result.rules:
                print("生效的优化规则: " + "; ".join(result.rules))
            print("S 表达式:", result.plan_sexpr)
        if result.rows and result.columns:
            print(format_table(result.columns, result.rows))
        if result.message:
            print(result.message)


def _contains_semicolon(text: str) -> bool:
    return ";" in text


def _toggle(current: bool, arg: str, label: str) -> bool:
    if arg == "on":
        print(f"{label}: 开")
        return True
    if arg == "off":
        print(f"{label}: 关")
        return False
    print(f"{label}: {'开' if current else '关'}（用法: on|off）")
    return current


# ------------------------------ 入口 ------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniSQL 小型数据库系统")
    parser.add_argument("--db", default="mini.db", help="数据库文件路径（默认 mini.db）")
    parser.add_argument("--file", help="执行 SQL 脚本文件后退出")
    parser.add_argument("--sql", help="执行一条 SQL 后退出")
    parser.add_argument("--pool-size", type=int, default=16, help="缓冲区帧数（默认 16）")
    parser.add_argument("--policy", default="LRU", choices=["LRU", "FIFO"],
                        help="缓存替换策略（默认 LRU）")
    parser.add_argument("--plan", action="store_true", help="显示执行计划")
    parser.add_argument("--tokens", action="store_true", help="显示 Token 流")
    parser.add_argument("--no-optimize", action="store_true", help="关闭优化器")
    parser.add_argument("--buffer-log", action="store_true", help="打印缓冲区替换日志")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db = Database(
        path=args.db,
        pool_size=args.pool_size,
        policy=args.policy,
        buffer_verbose=args.buffer_log,
        enable_optimizer=not args.no_optimize,
    )
    shell = Shell(db, show_plan=args.plan, show_tokens=args.tokens)

    if args.sql:
        shell._execute(args.sql)
        db.close()
        return 0
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            shell._execute(f.read())
        db.close()
        return 0

    try:
        return shell.run()
    finally:
        pass


if __name__ == "__main__":
    sys.exit(main())
