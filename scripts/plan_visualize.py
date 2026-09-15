"""把优化后的查询计划导出为 Mermaid / DOT / HTML，便于离线查看与讲解。

用法示例::

    python -m scripts.plan_visualize --database data/demo.db \
        --sql "SELECT * FROM t WHERE id = 1" --format html --out plan.html

WHY：Web 工作台已经能画计划，但验收现场经常只有终端 + 浏览器。这个脚本把
"优化后的计划"直接落成一张自包含的 HTML（Mermaid 从 CDN 加载），不依赖服务端。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yoursql.common.config import DEFAULT_DATABASE_PATH, DatabaseConfig, load_dotenv
from yoursql.common.errors import YourSQLError
from yoursql.engine.runtime.database import Database
from yoursql.planner.physical import PhysicalPlanNode, PlanNode


def _render(plan: PhysicalPlanNode, fmt: str) -> str:
    if fmt == "mermaid":
        return plan.to_mermaid()
    if fmt == "dot":
        return plan.to_dot()
    if fmt == "json":
        return plan.to_json()
    return plan.explain()


def _wrap_html(plan: PlanNode, sql: str, *, source: str, explanation: str) -> str:
    """生成自包含 HTML：左边看图，右边看 Mermaid 源与文本 EXPLAIN。

    HOW：Mermaid 源放在 ``<script type="text/plain">`` 里——script 内容是原始文本，
    不会被 HTML 解码，因此标签里的 ``&gt;`` 等实体能原样交给 mermaid。
    """

    rules = plan.properties.get("rules") or ()
    rules_text = "、".join(str(rule) for rule in rules) if rules else "（本次未命中任何具名规则）"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>YourSQL 查询计划可视化</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f6f7f9; color: #1f2328; }}
  header {{ padding: 16px 24px; background: #fff; border-bottom: 1px solid #e3e6ea; }}
  h1 {{ margin: 0 0 6px; font-size: 18px; }}
  .sql {{ font-family: Consolas, "Courier New", monospace; font-size: 13px; color: #0b5cad;
          white-space: pre-wrap; word-break: break-all; }}
  .rules {{ margin-top: 8px; font-size: 13px; color: #57606a; }}
  main {{ display: flex; gap: 16px; padding: 16px 24px; align-items: flex-start;
          flex-wrap: wrap; }}
  .panel {{ background: #fff; border: 1px solid #e3e6ea; border-radius: 10px;
            padding: 16px; min-width: 320px; flex: 1 1 420px; }}
  .panel h2 {{ margin: 0 0 10px; font-size: 14px; color: #57606a; font-weight: 600; }}
  pre {{ margin: 0; font-family: Consolas, "Courier New", monospace; font-size: 12.5px;
         line-height: 1.6; white-space: pre; overflow: auto; color: #1f2328; }}
</style>
</head>
<body>
<header>
  <h1>YourSQL 优化后查询计划</h1>
  <div class="sql">{_html_escape(sql)}</div>
  <div class="rules">本次命中规则：{_html_escape(rules_text)}</div>
</header>
<main>
  <section class="panel">
    <h2>计划图（Mermaid）</h2>
    <div id="graph">渲染中…</div>
  </section>
  <section class="panel">
    <h2>文本 EXPLAIN</h2>
    <pre>{_html_escape(explanation)}</pre>
  </section>
  <section class="panel">
    <h2>Mermaid 源码</h2>
    <pre>{_html_escape(source)}</pre>
  </section>
</main>
<script type="text/plain" id="mermaid-src">{source}</script>
<script>
  mermaid.initialize({{ startOnLoad: false, theme: "default", securityLevel: "loose" }});
  const source = document.getElementById("mermaid-src").textContent.trim();
  mermaid.render("planGraph", source)
    .then(({{ svg }}) => {{ document.getElementById("graph").innerHTML = svg; }})
    .catch((error) => {{
      document.getElementById("graph").textContent = "Mermaid 渲染失败：" + error;
    }});
</script>
</body>
</html>
"""


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_parser() -> argparse.ArgumentParser:
    load_dotenv()
    parser = argparse.ArgumentParser(description="导出 YourSQL 查询计划")
    parser.add_argument(
        "--database",
        default=os.getenv("YOURSQL_DATABASE") or str(DEFAULT_DATABASE_PATH),
        help="数据库文件路径",
    )
    parser.add_argument("--sql", help="要解析的 SQL")
    parser.add_argument("--file", type=Path, help="从文件读取 SQL")
    parser.add_argument(
        "--format",
        choices=("text", "mermaid", "dot", "json", "html"),
        default="mermaid",
        help="输出格式（默认 mermaid）",
    )
    parser.add_argument("--out", type=Path, help="输出文件；缺省时打印到标准输出")
    parser.add_argument(
        "--disable-rule",
        action="append",
        default=[],
        metavar="NAME",
        help="临时关闭一条优化规则，可重复",
    )
    parser.add_argument("--user", default=os.getenv("YOURSQL_CLI_USER", "admin"))
    parser.add_argument(
        "--password", default=os.getenv("YOURSQL_CLI_PASSWORD", "admin")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sql = args.sql
    if args.file is not None:
        try:
            sql = args.file.read_text(encoding="utf-8")
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if sql is None:
        print("需要 --sql 或 --file 指定查询", file=sys.stderr)
        return 1
    try:
        with Database(
            args.database,
            config=DatabaseConfig.from_environment(),
            user=args.user,
            password=args.password,
            disabled_rules=args.disable_rule,
        ) as database:
            plan = database.compile(sql).optimized_plan
            if plan is None:
                print("该语句没有可展示的执行计划", file=sys.stderr)
                return 1
            if args.format == "html":
                text = _wrap_html(
                    plan,
                    sql,
                    source=plan.to_mermaid(),
                    explanation=plan.explain(),
                )
            else:
                text = _render(plan, args.format)
    except (YourSQLError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
