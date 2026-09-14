"""兼容标准库 HTTP 服务与工作台 REST API，默认只监听本机。"""

from __future__ import annotations

import mimetypes
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

from ...common import JsonObject, JsonValue
from ...common.codec import MANUAL_CONTENT_TYPE, PayloadCodecError, payload_codec
from ...common.config import RuntimeConfig
from ...common.errors import YourSQLError
from ..runtime.database import Database
from .inspection import (
    inspect_index,
    inspect_page,
    storage_cache_snapshot,
    storage_index_snapshot,
    storage_page_changes,
    storage_snapshot,
)
from .workbench import WebSession, Workbench
from .workbench_sql import MAX_SQL

ERROR_STATUS = {
    "BAD_REQUEST": 400,
    "UNAUTHENTICATED": 401,
    "AUTHORIZATION_ERROR": 403,
    "NOT_FOUND": 404,
    "SESSION_BUSY": 409,
    "RESOURCE_LIMIT": 429,
    "SERVICE_BUSY": 503,
    "TIMEOUT": 504,
    "INTERNAL_ERROR": 500,
}
MAX_BODY = 262_144
MAX_DATABASE_IMPORT = 64 * 1024 * 1024
# 页面地图单次响应上限：工作台按此批大小分批拉取，调大可减少往返次数。
STORAGE_PAGE_LIMIT = 500


class _RequestHandler(BaseHTTPRequestHandler):
    """协议层只负责校验、认证和路由，不重新实现 SQL。"""

    server: DatabaseHTTPServer

    def setup(self) -> None:
        """初始化 HTTP 请求处理器的响应状态。"""
        super().setup()
        self.connection.settimeout(self.server.settings.request_timeout_seconds)

    def log_message(self, format: str, *args: object) -> None:
        """抑制底层 HTTP 服务器的默认控制台日志。"""
        return None

    def _headers(
        self,
        status: int,
        content_type: str,
        length: int,
        extra: dict[str, str] | None = None,
    ) -> None:
        """构造 payload 响应所需的通用响应头。"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Request-ID", self.request_id)
        self.send_header(
            "X-YourSQL-Payload-Codec",
            "manual" if content_type.startswith("application/x-yoursql") else "json",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "same-origin")
        content_security_policy = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        self.send_header(
            "Content-Security-Policy",
            content_security_policy,
        )
        origin = self.headers.get("Origin", "")
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send_json(
        self,
        status: int,
        payload: object,
        *,
        envelope: bool = False,
        extra: dict[str, str] | None = None,
    ) -> None:
        """按 Accept 发送 JSON 或 manual 响应。"""
        if envelope:
            payload = {
                "ok": status < 400,
                "request_id": self.request_id,
                "data": payload if status < 400 else None,
                "error": payload if status >= 400 else None,
            }
        accepts = {
            item.split(";", 1)[0].strip().lower()
            for item in self.headers.get("Accept", "").split(",")
        }
        if "application/x-yoursql" in accepts:
            codec = payload_codec("manual")
            content_type = MANUAL_CONTENT_TYPE
        elif "application/json" in accepts:
            codec = payload_codec("json")
            content_type = "application/json; charset=utf-8"
        else:
            codec = payload_codec(self.server.settings.payload_codec)
            content_type = (
                MANUAL_CONTENT_TYPE
                if codec.name == "manual"
                else "application/json; charset=utf-8"
            )
        try:
            encoded = codec.encode(payload)
        except PayloadCodecError as exc:
            raise YourSQLError("响应 payload 无法编码", "INTERNAL_ERROR") from exc
        self._headers(status, content_type, len(encoded), extra)
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _origin(self) -> None:
        """读取并校验当前请求的 Origin。"""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if (
            origin
            and origin not in self.server.allowed_origins
            and origin != f"http://{host}"
        ):
            raise YourSQLError("请求来源未获允许", "AUTHORIZATION_ERROR")
        if (
            self.headers.get("Sec-Fetch-Site") == "cross-site"
            and origin not in self.server.allowed_origins
        ):
            raise YourSQLError("拒绝跨站请求", "AUTHORIZATION_ERROR")

    def _body(self) -> JsonObject:
        """读取 JSON 或 manual 请求体并解析为对象。"""
        content_type = self.headers.get_content_type()
        if content_type not in {"application/json", "application/x-yoursql"}:
            raise YourSQLError("请使用 application/json 或 application/x-yoursql", "BAD_REQUEST")
        if self.headers.get("Transfer-Encoding"):
            raise YourSQLError("不支持分块请求体", "BAD_REQUEST")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise YourSQLError("Content-Length 无效", "BAD_REQUEST") from exc
        if length <= 0 or length > self.server.max_body_bytes:
            raise YourSQLError(
                f"请求体为空或超过 {self.server.max_body_bytes} B", "BAD_REQUEST"
            )
        codec = payload_codec(
            "manual" if content_type == "application/x-yoursql" else "json"
        )
        try:
            value = codec.decode(self.rfile.read(length))
        except PayloadCodecError as exc:
            raise YourSQLError("请求 payload 格式无效", "BAD_REQUEST") from exc
        if not isinstance(value, dict):
            raise YourSQLError("请求 payload 必须是对象", "BAD_REQUEST")
        return value

    def _binary_body(self) -> bytes:
        """读取资源管理器选中的数据库文件。"""
        if self.headers.get_content_type() != "application/octet-stream":
            raise YourSQLError("请使用数据库文件上传格式", "BAD_REQUEST")
        if self.headers.get("Transfer-Encoding"):
            raise YourSQLError("不支持分块请求体", "BAD_REQUEST")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise YourSQLError("Content-Length 无效", "BAD_REQUEST") from exc
        if length <= 0 or length > MAX_DATABASE_IMPORT:
            raise YourSQLError(
                f"数据库文件大小应为 1–{MAX_DATABASE_IMPORT // (1024 * 1024)} MiB",
                "BAD_REQUEST",
            )
        content = self.rfile.read(length)
        if len(content) != length:
            raise YourSQLError("数据库文件上传不完整", "BAD_REQUEST")
        return content

    def _upload_name(self) -> str:
        """从上传头中读取安全的数据库文件名。"""
        raw = unquote(self.headers.get("X-YourSQL-File-Name", "")).strip()
        name = Path(raw.replace("\\", "/")).name
        if (
            not name
            or len(name) > 255
            or "\x00" in name
            or not name.lower().endswith(".db")
        ):
            raise YourSQLError("请选择 .db 数据库文件", "BAD_REQUEST")
        return name

    @staticmethod
    def _string(body: JsonObject, name: str, maximum: int = MAX_SQL) -> str:
        """扫描或读取字符串输入。"""
        value = body.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise YourSQLError(
                f"{name} 必须为 1–{maximum} 字符的非空字符串", "BAD_REQUEST"
            )
        return value

    @staticmethod
    def _integer(
        value: JsonValue | None, default: int, minimum: int, maximum: int
    ) -> int:
        """从请求体读取受范围限制的整数。"""
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise YourSQLError("分页/超时参数必须是整数", "BAD_REQUEST")
        try:
            result = int(value)
        except ValueError as exc:
            raise YourSQLError("分页/超时参数必须是整数", "BAD_REQUEST") from exc
        if result < minimum or result > maximum:
            raise YourSQLError(f"参数范围应为 {minimum}–{maximum}", "BAD_REQUEST")
        return result

    def _session(self) -> WebSession:
        """读取当前请求绑定的工作台会话。"""
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return self.server.workbench.authenticate(authorization[7:])
        cookie: SimpleCookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        value = cookie.get("yoursql_session")
        return self.server.workbench.authenticate(value.value if value else "")

    def do_OPTIONS(self) -> None:
        """处理 CORS 预检请求。"""
        self.request_id = uuid4().hex
        try:
            self._origin()
            self._send_json(
                200,
                {},
                envelope=True,
                extra={
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, X-YourSQL-Client, X-YourSQL-File-Name",
                    "Access-Control-Max-Age": "600",
                },
            )
        except YourSQLError as exc:
            self._send_json(
                403, {"code": exc.code, "message": exc.message}, envelope=True
            )

    def do_GET(self) -> None:
        """分发 GET 请求。"""
        self._dispatch(False)

    def do_POST(self) -> None:
        """分发 POST 请求。"""
        self._dispatch(True)

    def do_DELETE(self) -> None:
        """分发 DELETE 请求。"""
        self._unsupported_method()

    def do_PUT(self) -> None:
        """分发 PUT 请求。"""
        self._unsupported_method()

    def do_PATCH(self) -> None:
        """分发 PATCH 请求。"""
        self._unsupported_method()

    def _unsupported_method(self) -> None:
        """返回不支持的 HTTP 方法错误。"""
        self.request_id = uuid4().hex
        self._send_json(
            405,
            {"code": "METHOD_NOT_ALLOWED", "message": "接口只允许声明的 GET/POST 方法"},
            envelope=True,
        )

    def _dispatch(self, post: bool) -> None:
        """根据请求路径和方法分发处理逻辑。"""
        self.request_id = uuid4().hex
        route = urlparse(self.path).path
        modern = route.startswith("/api/")
        try:
            self._origin()
            if not modern:
                self._legacy(route, post)
                return
            query = parse_qs(urlparse(self.path).query)
            offset = self._integer(query.get("offset", [None])[0], 0, 0, 1_000_000)
            limit = self._integer(query.get("limit", [None])[0], 100, 1, 500)
            upload_route = route in {
                "/api/databases/import",
                "/api/databases/import-before-login",
            }
            body = self._body() if post and not upload_route else {}
            workbench = self.server.workbench
            if post and route == "/api/auth/login":
                username = self._string(body, "username", 128)
                password = self._string(body, "password", 1024)
                try:
                    token, session = workbench.login(username, password)
                except YourSQLError as exc:
                    if exc.code == "AUTHORIZATION_ERROR":
                        raise YourSQLError(
                            "用户名或密码错误", "UNAUTHENTICATED"
                        ) from exc
                    raise
                self._send_json(
                    200,
                    workbench.me(session),
                    envelope=True,
                    extra={
                        "Set-Cookie": f"yoursql_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(workbench.session_ttl)}"
                    },
                )
                return
            if not post and route == "/api/databases/available-before-login":
                data = workbench.public_database_files()
                self._send_json(200, data, envelope=True)
                return
            if post and route == "/api/databases/select-before-login":
                path = (
                    self._string(body, "path", 4096)
                    if "path" in body
                    else self._string(body, "name", 255)
                )
                data = workbench.select_database_before_login(path)
                self._send_json(
                    200,
                    data,
                    envelope=True,
                    extra={
                        "Set-Cookie": "yoursql_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    },
                )
                return
            if post and route == "/api/databases/import-before-login":
                data = workbench.import_database_before_login(
                    self._upload_name(), self._binary_body()
                )
                self._send_json(
                    201,
                    data,
                    envelope=True,
                    extra={
                        "Set-Cookie": "yoursql_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    },
                )
                return
            session = self._session()
            parts = [unquote(part) for part in route.split("/") if part]
            if not post and route in {"/api/session", "/api/permissions"}:
                data = workbench.me(session)
            elif post and route == "/api/auth/logout":
                workbench.logout(session)
                self._send_json(
                    200,
                    {"logged_out": True},
                    envelope=True,
                    extra={
                        "Set-Cookie": "yoursql_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    },
                )
                return
            elif not post and route == "/api/dialect":
                data = workbench.dialect()
            elif not post and route == "/api/databases/available":
                data = workbench.database_files(session)
            elif post and route == "/api/databases/select":
                path = (
                    self._string(body, "path", 4096)
                    if "path" in body
                    else self._string(body, "name", 255)
                )
                data = workbench.select_database(session, path)
                extra = (
                    {
                        "Set-Cookie": "yoursql_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    }
                    if data.get("requires_login")
                    else None
                )
                self._send_json(200, data, envelope=True, extra=extra)
                return
            elif post and route == "/api/databases/create":
                path = self._string(body, "path", 4096)
                options = {
                    key: body[key]
                    for key in (
                        "page_size",
                        "buffer_pool_size",
                        "replacement_policy",
                        "payload_codec",
                    )
                    if key in body
                }
                data = workbench.create_database(session, path, options)
                self._send_json(
                    201,
                    data,
                    envelope=True,
                    extra={
                        "Set-Cookie": "yoursql_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    },
                )
                return
            elif post and route == "/api/databases/import":
                data = workbench.import_database(
                    session, self._upload_name(), self._binary_body()
                )
                self._send_json(
                    201,
                    data,
                    envelope=True,
                    extra={
                        "Set-Cookie": "yoursql_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                    },
                )
                return
            elif post and route == "/api/storage/cache/policy":
                replacement_policy = self._string(body, "replacement_policy", 16)
                data = workbench.set_storage_policy(session, replacement_policy)
            elif not post and route in {"/api/databases", "/api/tables"}:
                data = workbench.metadata(session)
                for database in data["databases"]:
                    tables = database["tables"]
                    views = database.get("views", [])
                    database.update(
                        {
                            "tables": tables[offset : offset + limit],
                            "total": len(tables),
                            "views": views[offset : offset + limit],
                            "total_views": len(views),
                            "offset": offset,
                            "limit": limit,
                        }
                    )
            elif not post and len(parts) == 3 and parts[1] == "tables":
                data = workbench.metadata(session, parts[2])
            elif post and route == "/api/validate":
                data = workbench.validate(
                    session, self._string(body, "sql", self.server.max_sql_chars)
                )
            elif post and route == "/api/queries":
                sql = self._string(body, "sql", self.server.max_sql_chars)
                timeout = self._integer(
                    body.get("timeout_seconds"),
                    self.server.settings.default_query_timeout_seconds,
                    1,
                    self.server.settings.max_query_timeout_seconds,
                )
                rows = self._integer(
                    body.get("row_limit"),
                    self.server.settings.default_result_rows,
                    1,
                    self.server.settings.max_result_rows,
                )
                task = workbench.submit(session, sql, timeout, rows)
                self._send_json(202, {"id": task.id, "status": "queued"}, envelope=True)
                return
            elif not post and len(parts) == 3 and parts[1] == "queries":
                data = workbench.task(session, parts[2])
            elif (
                post
                and len(parts) == 4
                and parts[1] == "queries"
                and parts[3] == "cancel"
            ):
                data = workbench.cancel(session, parts[2])
            elif (
                not post
                and len(parts) == 5
                and parts[1] == "queries"
                and parts[3] == "results"
            ):
                index = self._integer(parts[4], 0, 0, 31)
                data = workbench.task(
                    session, parts[2], result_index=index, offset=offset, limit=limit
                )
            elif not post and route == "/api/history":
                data = workbench.history(session, offset, limit)
            elif not post and route.startswith("/api/storage"):
                with workbench.connection(session):
                    if route == "/api/storage":
                        # WHY：默认接口必须保持有界响应；工作台明确请求 all=1 时再加载完整页地图。
                        all_pages = query.get("all", [""])[0].lower() in {"1", "true"}
                        # HOW：fields=map 只取画地图必需的页头；表/索引标签留到选中页单独请求。
                        map_only = "map" in query.get("fields", [""])[0].lower()
                        page_limit = (
                            min(self.server.database.disk.page_count, 10_000)
                            if all_pages
                            else min(limit, STORAGE_PAGE_LIMIT)
                        )
                        data = storage_snapshot(
                            self.server.database,
                            0 if all_pages else offset,
                            page_limit,
                            map_only=map_only,
                        )
                    elif route == "/api/storage/changes":
                        since = self._integer(
                            query.get("since", [None])[0], 0, 0, 2**31 - 1
                        )
                        data = storage_page_changes(
                            self.server.database, since, min(limit, 500)
                        )
                    elif route == "/api/storage/cache":
                        data = storage_cache_snapshot(
                            self.server.database, offset, min(limit, 100)
                        )
                    elif route == "/api/storage/indexes":
                        data = storage_index_snapshot(
                            self.server.database, min(limit, 100)
                        )
                    elif len(parts) == 4 and parts[2] == "pages":
                        page_id = self._integer(parts[3], 0, 0, 2**31 - 1)
                        data = inspect_page(
                            self.server.database, page_id, offset, min(limit, 100)
                        )
                    elif len(parts) == 4 and parts[2] == "indexes":
                        data = inspect_index(
                            self.server.database, parts[3], offset, min(limit, 100)
                        )
                    else:
                        raise YourSQLError("接口不存在", "NOT_FOUND")
            else:
                raise YourSQLError("接口不存在或方法不匹配", "NOT_FOUND")
            self._send_json(200, data, envelope=True)
        except YourSQLError as exc:
            self._send_json(
                ERROR_STATUS.get(exc.code, 400),
                {"code": exc.code, "message": exc.message} if modern else exc.as_dict(),
                envelope=modern,
            )
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._send_json(
                400,
                {"code": "BAD_REQUEST", "message": "请求参数或 JSON 格式无效"},
                envelope=modern,
            )
        except TimeoutError:
            self._send_json(
                408,
                {"code": "REQUEST_TIMEOUT", "message": "读取请求超时"},
                envelope=modern,
            )
        except Exception:
            self._send_json(
                500,
                {
                    "code": "INTERNAL_ERROR",
                    "message": "服务异常，请根据请求 ID 检查服务状态",
                },
                envelope=modern,
            )

    def _legacy(self, route: str, post: bool) -> None:
        """处理旧版兼容 REST 路由。"""
        if not post and route == "/health":
            self._send_json(200, self.server.database.health())
        elif not post and route == "/metrics":
            if not self.server.legacy_anonymous:
                self._session().connection.authorize("SECURITY")
            with self.server.workbench.connection(None):
                self._send_json(200, self.server.database.metrics())
        elif post and route == "/sql":
            body = self._body()
            sql = self._string(body, "sql", self.server.max_sql_chars)
            session = None
            if (
                not self.server.legacy_anonymous
                or self.headers.get("Cookie")
                or self.headers.get("Authorization")
            ):
                session = self._session()
            if session and session.active_task:
                raise YourSQLError("当前连接已有执行任务", "SESSION_BUSY")
            with self.server.workbench.connection(session):
                result = self.server.database.execute(sql)
            self._send_json(200, result.as_dict())
        elif not post:
            root = self.server.static_dir.resolve()
            relative = unquote(route).lstrip("/") or "index.html"
            file = (root / relative).resolve()
            if not file.is_relative_to(root) or not file.is_file():
                self._send_json(
                    404,
                    {
                        "error": "NOT_FOUND",
                        "message": "页面不存在；请先在 web 目录运行 npm run build",
                    },
                )
                return
            content = file.read_bytes()
            content_type = (
                mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            )
            if file.suffix == ".js":
                content_type = "application/javascript"
            self._headers(200, content_type, len(content))
            self.wfile.write(content)
        else:
            self._send_json(404, {"error": "NOT_FOUND", "message": "unknown endpoint"})


class DatabaseHTTPServer(ThreadingHTTPServer):
    """保留旧构造方式；工作台启动器默认关闭匿名 /sql。"""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        database: Database,
        *,
        legacy_anonymous: bool = True,
        allowed_origins: tuple[str, ...] = (),
        static_dir: Path | None = None,
        settings: RuntimeConfig | None = None,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self._initial_database = database
        self.settings = settings or RuntimeConfig(
            payload_codec=database.config.payload_codec,
            database_config=database.config,
        )
        self.max_body_bytes = self.settings.max_request_body_bytes
        self.max_sql_chars = self.settings.max_sql_chars
        self.workbench = Workbench(database, settings=self.settings)
        self.legacy_anonymous = legacy_anonymous
        self.allowed_origins = frozenset(allowed_origins)
        self.static_dir = (
            # WHY：HTTP 模块位于 yoursql/engine/services，前端产物和 package-data 位于 yoursql/workbench_static。
            static_dir or Path(__file__).resolve().parents[2] / "workbench_static"
        )
        super().__init__(address, _RequestHandler)

    @property
    def database(self) -> Database:
        """始终返回工作台当前实例，保证切库后旧兼容接口同步生效。"""

        return self.workbench.database

    def server_close(self) -> None:
        """关闭 HTTP 服务器并释放关联资源。"""
        self.workbench.close()
        if self.database is not self._initial_database:
            self.database.close()
        super().server_close()


class HTTPService:
    """可嵌入测试或应用的 HTTP 服务生命周期封装。"""

    def __init__(
        self,
        database: Database,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        legacy_anonymous: bool = True,
        allowed_origins: tuple[str, ...] = (),
        static_dir: Path | None = None,
        settings: RuntimeConfig | None = None,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.server = DatabaseHTTPServer(
            (host, port),
            database,
            legacy_anonymous=legacy_anonymous,
            allowed_origins=allowed_origins,
            static_dir=static_dir,
            settings=settings,
        )
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """返回 HTTP 服务监听地址。"""
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        """启动服务或后台任务。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = Thread(
            target=self.server.serve_forever, name="yoursql-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """停止服务并释放运行资源。"""
        self.server.shutdown()
        self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def __enter__(self) -> HTTPService:
        """进入上下文管理器并返回当前对象。"""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """退出上下文管理器并完成资源清理。"""
        self.stop()


def serve_http(database: Database, host: str = "127.0.0.1", port: int = 8080) -> None:
    """兼容原始教学服务；工作台推荐 python -m yoursql.web。"""
    server = DatabaseHTTPServer((host, port), database)
    try:
        server.serve_forever()
    finally:
        server.server_close()


__all__ = [
    "DatabaseHTTPServer",
    "ERROR_STATUS",
    "HTTPService",
    "MAX_BODY",
    "MAX_DATABASE_IMPORT",
    "STORAGE_PAGE_LIMIT",
    "serve_http",
]
