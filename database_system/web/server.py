"""Serve the workbench with Python's standard library."""
from __future__ import annotations

import argparse
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from database_system.engine.database import Database

STATIC = Path(__file__).parent / "static"


def pipeline_debug(result):
    """把单条语句的编译流水线中间结果整理成 Web 调试视图。"""
    stages = ("Lexer", "Parser", "Semantic", "Planner", "Optimizer", "Execute")
    reached = {name: False for name in stages}
    current = result.stage
    if current in reached:
        for name in stages[:stages.index(current) + 1]:
            reached[name] = True
    if not result.error and current == "Planner":
        reached["Optimizer"] = True
    if not result.error and current == "Execute":
        for name in stages:
            reached[name] = True

    def status(name):
        if result.error and name == current:
            return "error"
        if reached[name]:
            return "passed"
        return "skipped"

    error = result.error.to_dict() if result.error else None
    message = str(result.error) if result.error else result.message
    return {
        "lexer": {"status": status("Lexer"), "tokens": [t.to_dict() for t in result.tokens]},
        "parser": {"status": status("Parser"), "ast": result.ast_tree},
        "semantic": {
            "status": status("Semantic"),
            "message": message if status("Semantic") == "error" else "语义检查通过（名字绑定和类型检查已完成）。",
        },
        "planner": {"status": status("Planner"), "plan": result.plan_before},
        "optimizer": {
            "status": status("Optimizer"),
            "plan": result.plan_after,
            "sexpr": result.plan_sexpr,
            "rules": result.rules,
        },
        "execute": {
            "status": status("Execute"),
            "columns": result.columns,
            "rows": result.rows[:1000],
            "message": message,
        },
        "error": error,
    }


def snapshot(db):
    return {
        "database": Path(db.path).name,
        "tables": [{"name": name, "columns": [
            {"name": c.name, "type": str(c.data_type), "nullable": not c.not_null,
             "primary_key": c.primary_key}
            for c in db.catalog.get_table(name).columns
        ]} for name in db.tables()],
        "stats": db.buffer_stats(),
    }


def make_server(db, port=8765):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def send_content(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def json_response(self, status, value):
            self.send_content(status, json.dumps(value, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")

        def allowed_host(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"
            }

        def do_GET(self):
            if not self.allowed_host():
                return self.json_response(403, {"error": "Invalid host"})
            if self.path == "/api/state":
                return self.json_response(200, {**snapshot(db), "token": token})
            routes = {"/": ("index.html", "text/html"),
                      "/app.js": ("app.js", "text/javascript"),
                      "/style.css": ("style.css", "text/css")}
            if self.path not in routes:
                return self.json_response(404, {"error": "Not found"})
            name, mime = routes[self.path]
            self.send_content(200, (STATIC / name).read_bytes(), mime + "; charset=utf-8")

        def do_POST(self):
            if not self.allowed_host() or self.headers.get("X-YourSQL-Token") != token:
                return self.json_response(403, {"error": "请刷新工作台后重试。"})
            if self.path != "/api/execute":
                return self.json_response(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    return self.json_response(413, {"error": "SQL 内容不能为空或超过 1 MB。"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是 JSON 对象。")
                sql = payload.get("sql")
                if not isinstance(sql, str) or not sql.strip():
                    raise ValueError("请输入 SQL 语句。")
            except (ValueError, UnicodeDecodeError) as exc:
                return self.json_response(400, {"error": str(exc)})
            started = time.perf_counter()
            try:
                results = db.execute(sql)
                output = [{"sql": r.sql, "ok": r.ok, "stage": r.stage,
                           "columns": r.columns, "rows": r.rows[:1000],
                           "row_count": len(r.rows) if r.columns else 0,
                           "truncated": bool(r.columns and len(r.rows) > 1000),
                           "message": str(r.error) if r.error else r.message,
                           "error": r.error.to_dict() if r.error else None,
                           "debug": pipeline_debug(r),
                           "plan_before": r.plan_before, "plan_after": r.plan_after,
                           "rules": r.rules} for r in results]
                self.json_response(200, {"results": output,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    **snapshot(db)})
            except Exception as exc:
                self.log_error("Execution failure: %s", exc)
                self.json_response(500, {"error": "执行异常；部分语句可能已生效，请检查表状态及终端日志。"})

    # One request at a time: the database's buffer and catalog are not thread-safe.
    return HTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="YourSQL 本地数据库工作台")
    parser.add_argument("--db", default="data/workbench.db")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    Path(args.db).resolve().parent.mkdir(parents=True, exist_ok=True)
    with Database(args.db) as db:
        with make_server(db, args.port) as server:
            print(f"YourSQL: http://127.0.0.1:{server.server_port}", flush=True)
            print(f"Database: {Path(args.db).resolve()}\nCtrl+C to stop.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
