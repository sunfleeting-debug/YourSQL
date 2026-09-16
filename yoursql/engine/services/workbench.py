"""【前端特供】工作台服务：有界任务、持久化 RBAC 认证与查询历史。"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from time import monotonic, perf_counter
from typing import Iterator
from uuid import uuid4

from yoursql.common import (
    AuthorizationError,
    DatabaseConfig,
    JsonObject,
    YourSQLError,
    RuntimeConfig,
    StorageError,
    validate_payload_codec,
)
from yoursql.common.trace import ExecutionTrace, current_trace
from yoursql.sql.ast import Explain, Select, Show
from yoursql.sql.lexer import KEYWORDS, tokenize
from yoursql.sql.parser import Parser
from yoursql.engine.runtime.database import Database
from yoursql.engine.security.session import Session
from yoursql.engine.services.workbench_sql import (
    SQLSlice,
    compile_observed,
    error_info,
    redact_sql,
    result_columns,
    split_sql,
    stage,
)
from yoursql.engine.services.monitoring import PerformanceMonitor
from yoursql.engine.services.buffer_pool_demo import run_buffer_pool_demo

__all__ = ["QueryTask", "WebSession", "Workbench", "now"]


def now() -> str:
    """【前端特供】返回工作台协议使用的标准化时间表示。"""
    return datetime.now(timezone.utc).isoformat()


def _metric_number(value: object) -> float:
    """读取性能观测中的数字，避免异常产物破坏查询响应。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _metric_integer(value: object) -> int:
    """读取性能观测中的计数。"""
    return int(_metric_number(value))


@dataclass
class WebSession:
    """【前端特供】浏览器工作台会话及其认证连接。"""

    key: str
    connection: Session
    expires_at: float
    active_task: str | None = None
    closed: bool = False


@dataclass
class QueryTask:
    """【前端特供】工作台异步查询任务及其阶段结果。"""

    id: str
    session_key: str
    sql: str
    submitted_at: str
    timeout_seconds: float
    row_limit: int
    state: str = "queued"
    results: list[JsonObject] = field(default_factory=list)
    error: JsonObject | None = None
    elapsed_ms: float = 0
    cancel: Event = field(default_factory=Event)
    deadline: float = 0
    submitted_monotonic: float = field(default_factory=monotonic, repr=False)


class Workbench:
    """【前端特供】一个 HTTP 服务复用一个 Database。"""

    def __init__(
        self,
        database: Database,
        *,
        session_ttl: float | None = None,
        settings: RuntimeConfig | None = None,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.database = database
        self._path_root = Path.cwd().resolve()
        self.settings = settings or RuntimeConfig()
        if session_ttl is not None:
            self.settings = replace(self.settings, session_ttl_seconds=session_ttl)
        self.session_ttl = self.settings.session_ttl_seconds
        self.monitor = PerformanceMonitor(
            slow_threshold_ms=self.settings.slow_query_ms,
            log_dir=Path("logs"),
            diagnostic_sample_rate=self.settings.monitor_diagnostic_sample_rate,
        )
        self._sessions: dict[str, WebSession] = {}
        self._tasks: OrderedDict[str, QueryTask] = OrderedDict()
        self._history: dict[str, deque[JsonObject]] = {}
        self._lock = RLock()
        self._worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="yoursql-query"
        )
        self._switch_lock = RLock()
        self._stopped = False

    @staticmethod
    def _key(token: str) -> str:
        """根据输入生成稳定的内部键。"""
        return hashlib.sha256(token.encode()).hexdigest()

    def login(self, username: str, password: str) -> tuple[str, WebSession]:
        """验证凭据并创建登录会话。"""
        with self.connection(None):
            user = self.database.rbac.authenticate(username, password)
            with self._lock:
                if len(self._sessions) >= self.settings.max_sessions:
                    raise YourSQLError("会话数量达到上限", "RESOURCE_LIMIT")
                token = secrets.token_urlsafe(32)
                session = WebSession(
                    self._key(token),
                    Session(user, self.database.rbac),
                    monotonic() + self.session_ttl,
                )
                self._sessions[session.key] = session
        return token, session

    def authenticate(self, token: str) -> WebSession:
        """校验凭据并返回认证后的主体。"""
        with self._lock:
            session = self._sessions.get(self._key(token))
            if session is None or session.closed or monotonic() >= session.expires_at:
                raise YourSQLError("请登录，或会话已过期", "UNAUTHENTICATED")
            return session

    def _reap(self) -> None:
        # 调用方持有 Database 锁；过期会话和已完成任务一起清理。
        """清理已过期的会话和已完成任务。"""
        with self._lock:
            for key, session in tuple(self._sessions.items()):
                if session.closed or monotonic() >= session.expires_at:
                    if session.active_task:
                        task = self._tasks.get(session.active_task)
                        if task:
                            task.cancel.set()
                    self._sessions.pop(key, None)
                    for task_id, task in tuple(self._tasks.items()):
                        if task.session_key == key and task.state not in {
                            "queued",
                            "running",
                        }:
                            self._tasks.pop(task_id, None)

    @contextmanager
    def connection(self, session: WebSession | None) -> Iterator[None]:
        """建立或复用当前请求的数据库连接上下文。"""
        if not self.database._lock.acquire(timeout=2):
            raise YourSQLError("数据库正在执行请求，请稍后重试", "SERVICE_BUSY")
        original = self.database.session
        try:
            self._reap()
            if session and (session.closed or session.key not in self._sessions):
                raise YourSQLError("会话已失效", "UNAUTHENTICATED")
            if session:
                self.database.session = session.connection
            yield
        finally:
            self.database.session = original
            self.database._lock.release()

    def logout(self, session: WebSession) -> None:
        """注销会话并清理认证状态。"""
        with self._lock:
            session.closed = True
            if session.active_task and session.active_task in self._tasks:
                self._tasks[session.active_task].cancel.set()

    def me(self, session: WebSession) -> JsonObject:
        """返回当前会话用户及其权限摘要。"""
        return {
            "user": session.connection.user.name,
            "roles": sorted(session.connection.user.roles),
            "permissions": list(
                self.database.rbac.privileges_for(user=session.connection.user.name)
            ),
            "database": self.database.path.name,
            "database_path": self._display_path(self.database.path),
            "session_expires_in": max(0, int(session.expires_at - monotonic())),
            "active_task": session.active_task,
        }

    def metadata(self, session: WebSession, name: str | None = None) -> JsonObject:
        """返回当前数据库或对象的元数据。"""
        with self.connection(session):
            tables = []
            views = []
            if name:
                session.connection.authorize("SELECT", name)
                selected = (self.database.catalog.get_table(name),)
            else:
                selected = self.database.catalog.tables()
            for table in selected:
                try:
                    session.connection.authorize("SELECT", table.name)
                except AuthorizationError:
                    continue
                item = table.to_dict()
                item["indexes"] = [
                    index.to_dict()
                    for index in self.database.catalog.indexes()
                    if index.table_id == table.table_id
                ]
                item["create_sql"] = self.database._show(
                    Show("CREATE_TABLE", table.name)
                ).rows[0][1]
                tables.append(item)
            if name is None:
                for view in self.database.catalog.views():
                    try:
                        session.connection.authorize("SELECT", view.name)
                        self.database._authorize_statement(
                            self.database._view_query(view), "SELECT"
                        )
                    except AuthorizationError:
                        continue
                    item = view.to_dict()
                    item["create_sql"] = self.database._show(
                        Show("CREATE_VIEW", view.name)
                    ).rows[0][1]
                    views.append(item)
            return {
                "databases": [
                    {"name": self.database.path.name, "tables": tables, "views": views}
                ],
                "single_database": True,
                "filtered_by": "SELECT",
                "refreshed_at": now(),
            }

    def _display_path(self, path: Path) -> str:
        """返回适合工作台显示的路径；项目目录内优先使用相对路径。"""

        resolved = path.resolve()
        try:
            return str(resolved.relative_to(self._path_root))
        except ValueError:
            return str(resolved)

    def _database_files(self) -> JsonObject:
        """列出当前数据库所在目录中的数据库文件，不暴露绝对路径。"""

        active = self.database.path.resolve()
        root = active.parent
        try:
            candidates = sorted(
                (
                    path
                    for path in root.iterdir()
                    if path.is_file() and path.suffix.lower() == ".db"
                ),
                key=lambda path: path.name.lower(),
            )[:100]
        except OSError as exc:
            raise YourSQLError("无法读取数据库文件目录", "INTERNAL_ERROR") from exc
        files: list[JsonObject] = []
        for path in candidates:
            try:
                size_bytes = path.stat().st_size
                resolved = path.resolve()
            except OSError:
                continue
            files.append(
                {
                    "name": path.name,
                    "path": self._display_path(path),
                    "size_bytes": size_bytes,
                    "active": resolved == active,
                }
            )
        return {
            "active": self.database.path.name,
            "active_path": self._display_path(self.database.path),
            "files": files,
            "limit": 100,
            "note": "列表来自当前数据库所在目录；也可以输入本机上的 .db 路径。",
        }

    def database_files(self, session: WebSession) -> JsonObject:
        """列出已登录用户可切换的数据库文件。"""

        with self.connection(session):
            session.connection.authorize("SECURITY")
            return self._database_files()

    def public_database_files(self) -> JsonObject:
        """列出登录页可选择的数据库文件；响应只包含文件名和大小。"""

        # HOW：登录前没有用户会话，只允许读取当前服务目录的受限文件清单。
        with self.connection(None):
            return self._database_files()

    def _database_candidate(
        self, value: str, *, allow_external: bool = True, must_exist: bool = True
    ) -> Path:
        """解析数据库路径；文件名沿用当前目录，路径输入相对项目目录解析。"""

        raw = value.strip()
        if not raw or "\x00" in raw:
            raise YourSQLError("数据库路径不能为空", "BAD_REQUEST")
        input_path = Path(raw).expanduser()
        current_root = self.database.path.resolve().parent
        if input_path.is_absolute():
            candidate = input_path.resolve()
        elif len(input_path.parts) == 1:
            candidate = (current_root / input_path).resolve()
        else:
            candidate = (self._path_root / input_path).resolve()
        if candidate.suffix.lower() != ".db":
            raise YourSQLError("数据库路径必须以 .db 结尾", "BAD_REQUEST")
        if not allow_external and candidate.parent != current_root:
            raise YourSQLError("登录前只能选择当前服务目录中的 .db 文件", "BAD_REQUEST")
        if must_exist and not candidate.is_file():
            raise YourSQLError("数据库文件不存在", "NOT_FOUND")
        if not must_exist and not candidate.parent.is_dir():
            raise YourSQLError("数据库所在目录不存在", "NOT_FOUND")
        return candidate

    @staticmethod
    def _config_integer(
        options: JsonObject, name: str, default: int, minimum: int, maximum: int
    ) -> int:
        """读取创建参数中的整数，并在服务端再次限制范围。"""

        value = options.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise YourSQLError(f"{name} 必须是整数", "BAD_REQUEST")
        try:
            result = int(value)
        except ValueError as exc:
            raise YourSQLError(f"{name} 必须是整数", "BAD_REQUEST") from exc
        if result < minimum or result > maximum:
            raise YourSQLError(f"{name} 范围应为 {minimum}–{maximum}", "BAD_REQUEST")
        return result

    def _new_database_config(self, options: JsonObject) -> DatabaseConfig:
        """把工作台创建表单转换成内核配置。"""

        defaults = self.settings.database_config
        page_size = self._config_integer(
            options, "page_size", defaults.page_size, 512, 65_536
        )
        if page_size & (page_size - 1):
            raise YourSQLError("page_size 必须是 512–65536 之间的二次幂", "BAD_REQUEST")
        buffer_pool_size = self._config_integer(
            options, "buffer_pool_size", defaults.buffer_pool_size, 1, 4096
        )
        policy = options.get("replacement_policy", defaults.replacement_policy)
        if not isinstance(policy, str) or policy.lower() not in {"lru", "fifo", "2q"}:
            raise YourSQLError("replacement_policy 只能是 lru、fifo 或 2q", "BAD_REQUEST")
        protect_page_types = options.get(
            "protect_page_types", defaults.protect_page_types
        )
        if not isinstance(protect_page_types, bool):
            raise YourSQLError("protect_page_types 必须是布尔值", "BAD_REQUEST")
        raw_codec = options.get("payload_codec", defaults.payload_codec)
        if not isinstance(raw_codec, str):
            raise YourSQLError("payload_codec 必须是 json 或 manual", "BAD_REQUEST")
        try:
            selected_codec = validate_payload_codec(raw_codec)
        except ValueError as exc:
            raise YourSQLError(str(exc), "BAD_REQUEST") from exc
        return DatabaseConfig(
            page_size=page_size,
            buffer_pool_size=buffer_pool_size,
            replacement_policy=policy.lower(),
            protect_page_types=protect_page_types,
            max_varchar_length=defaults.max_varchar_length,
            payload_codec=selected_codec,
        )

    @staticmethod
    def _config_dict(config: DatabaseConfig) -> JsonObject:
        """返回创建结果中可展示的内核配置。"""

        return {
            "page_size": config.page_size,
            "buffer_pool_size": config.buffer_pool_size,
            "replacement_policy": config.replacement_policy,
            "payload_codec": config.payload_codec,
        }

    def _replace_database(self, replacement: Database) -> tuple[str, str]:
        """在无活动任务时替换实例，并使旧连接全部失效。"""

        old = self.database
        if old.path.resolve() == replacement.path.resolve():
            replacement.close()
            return old.path.name, old.path.name
        if not old._lock.acquire(timeout=2):
            replacement.close()
            raise YourSQLError("数据库正在执行请求，请稍后再切换", "SERVICE_BUSY")
        try:
            self._reap()
            with self._lock:
                if any(
                    task.state in {"queued", "running"} for task in self._tasks.values()
                ):
                    raise YourSQLError(
                        "当前仍有 SQL 任务执行，请等待完成后再切换", "SERVICE_BUSY"
                    )
                previous = old.path.name
                for item in self._sessions.values():
                    item.closed = True
                self._sessions.clear()
                self._tasks.clear()
                self._history.clear()
                self.database = replacement
        except Exception:
            replacement.close()
            raise
        finally:
            old._lock.release()
        old.close()
        return previous, replacement.path.name

    def _select_database(
        self, session: WebSession | None, path: str, *, require_security: bool
    ) -> JsonObject:
        """在指定会话上下文中切换数据库，并让旧会话失效。"""

        with self._switch_lock:
            with self.connection(session):
                if require_security:
                    if session is None:
                        raise YourSQLError("请登录后再切换数据库", "UNAUTHENTICATED")
                    session.connection.authorize("SECURITY")
                candidate = self._database_candidate(
                    path, allow_external=require_security
                )
                current = self.database
                if candidate == current.path.resolve():
                    return {
                        "database": current.path.name,
                        "path": self._display_path(current.path),
                        "previous_database": current.path.name,
                        "previous_path": self._display_path(current.path),
                        "changed": False,
                        "requires_login": False,
                    }
                try:
                    target_page_size = Database.detect_page_size(candidate)
                    target_config = current.config
                    if (
                        target_page_size is not None
                        and target_page_size != current.config.page_size
                    ):
                        target_config = replace(
                            current.config, page_size=target_page_size
                        )
                    replacement = Database(candidate, config=target_config)
                except Exception as exc:
                    raise YourSQLError(
                        "无法打开数据库文件，文件可能损坏或不是 YourSQL 数据库",
                        "BAD_REQUEST",
                    ) from exc
            previous, selected = self._replace_database(replacement)
            return {
                "database": selected,
                "path": self._display_path(self.database.path),
                "previous_database": previous,
                "changed": True,
                "requires_login": True,
            }

    def select_database(self, session: WebSession, path: str) -> JsonObject:
        """切换到用户指定的数据库；切换成功后要求在新库重新登录。"""

        return self._select_database(session, path, require_security=True)

    def select_database_before_login(self, path: str) -> JsonObject:
        """为登录页切换当前服务目录数据库；切换后必须使用目标库账号登录。"""

        return self._select_database(None, path, require_security=False)

    def create_database(
        self, session: WebSession, path: str, options: JsonObject | None = None
    ) -> JsonObject:
        """创建并切换到用户指定的新数据库。"""

        with self._switch_lock:
            with self.connection(session):
                session.connection.authorize("SECURITY")
                candidate = self._database_candidate(
                    path, allow_external=True, must_exist=False
                )
                if candidate.exists():
                    raise YourSQLError(
                        "目标数据库已存在，请换一个文件名", "BAD_REQUEST"
                    )
                config = self._new_database_config(options or {})
                try:
                    replacement = Database(candidate, config=config)
                except (OSError, ValueError, YourSQLError) as exc:
                    raise YourSQLError(
                        "无法创建数据库，请检查路径和配置", "BAD_REQUEST"
                    ) from exc
            previous, selected = self._replace_database(replacement)
            return {
                "database": selected,
                "path": self._display_path(self.database.path),
                "previous_database": previous,
                "changed": True,
                "requires_login": True,
                "config": self._config_dict(config),
            }

    def _import_database(
        self,
        session: WebSession | None,
        filename: str,
        content: bytes,
        *,
        require_security: bool,
    ) -> JsonObject:
        """导入资源管理器选中的数据库，并复制到当前数据库目录。"""
        if not isinstance(content, bytes) or not content:
            raise YourSQLError("数据库文件不能为空", "BAD_REQUEST")
        name = Path(filename.replace("\\", "/")).name.strip()
        if (
            not name
            or len(name) > 255
            or "\x00" in name
            or not name.lower().endswith(".db")
        ):
            raise YourSQLError("请选择 .db 数据库文件", "BAD_REQUEST")
        with self._switch_lock:
            with self.connection(session):
                if require_security:
                    if session is None:
                        raise YourSQLError("请登录后再导入数据库", "UNAUTHENTICATED")
                    session.connection.authorize("SECURITY")
                current = self.database
                candidate = (current.path.parent / name).resolve()
                if candidate.exists():
                    raise YourSQLError(
                        "当前数据库目录已有同名文件，请改用路径选择", "BAD_REQUEST"
                    )
                temporary = candidate.with_name(f".{candidate.stem}.{uuid4().hex}.db")
                replacement: Database | None = None
                try:
                    temporary.write_bytes(content)
                    # WHY：先用临时副本校验文件格式，避免损坏文件覆盖工作目录中的数据库。
                    probe = Database(temporary)
                    probe.close()
                    temporary.replace(candidate)
                    replacement = Database(candidate)
                except Exception as exc:
                    if replacement is not None:
                        replacement.close()
                    raise YourSQLError(
                        "无法导入数据库，文件可能损坏或不是 YourSQL 数据库",
                        "BAD_REQUEST",
                    ) from exc
                finally:
                    temporary.unlink(missing_ok=True)
            assert replacement is not None
            previous, selected = self._replace_database(replacement)
            return {
                "database": selected,
                "path": self._display_path(self.database.path),
                "previous_database": previous,
                "changed": True,
                "requires_login": True,
                "source_name": name,
            }

    def import_database(
        self, session: WebSession, filename: str, content: bytes
    ) -> JsonObject:
        """登录后导入资源管理器选中的数据库。"""
        return self._import_database(session, filename, content, require_security=True)

    def import_database_before_login(self, filename: str, content: bytes) -> JsonObject:
        """登录前导入已有数据库，导入后仍需使用目标库账号登录。"""
        return self._import_database(None, filename, content, require_security=False)

    def reset_storage_runtime(self, session: WebSession) -> JsonObject:
        """【前端特供】清空当前缓存并归零缓存与页 I/O 统计。"""

        with self._switch_lock:
            with self.connection(session):
                session.connection.authorize("SECURITY")
                with self._lock:
                    if any(
                        task.state in {"queued", "running"}
                        for task in self._tasks.values()
                    ):
                        raise YourSQLError(
                            "当前仍有 SQL 任务执行，请等待完成后再重置统计",
                            "SERVICE_BUSY",
                        )
                # HOW：reset_runtime 先写回脏页；I/O 计数随后归零，保证按钮后的数据从同一基线开始。
                self.database.buffer_pool.reset_runtime()
                self.database.disk.reset_io_stats()
                self.monitor.reset()
                return {
                    "snapshot_at": now(),
                    "readonly": True,
                    "reset": True,
                    "buffer_pool": self.database.buffer_pool.snapshot(0, 100).to_dict(),
                    "io": self.database.disk.io_stats().to_dict(),
                    "note": "当前缓存帧、查询观测、命中/缺页/淘汰统计和页 I/O 计数已归零；数据库逻辑内容不变。",
                }

    def set_storage_policy(
        self, session: WebSession, replacement_policy: str
    ) -> JsonObject:
        """切换当前数据库缓存的页淘汰策略，不清空现有缓存帧。"""

        policy = replacement_policy.strip().lower()
        if policy not in {"lru", "fifo", "2q"}:
            raise YourSQLError("replacement_policy 只能是 lru、fifo 或 2q", "BAD_REQUEST")
        with self._switch_lock:
            with self.connection(session):
                session.connection.authorize("SECURITY")
                with self._lock:
                    if any(
                        task.state in {"queued", "running"}
                        for task in self._tasks.values()
                    ):
                        raise YourSQLError(
                            "当前仍有 SQL 任务执行，请等待完成后再切换", "SERVICE_BUSY"
                        )
                previous = self.database.buffer_pool.replacement_policy
                changed = self.database.buffer_pool.set_replacement_policy(policy)
                if changed:
                    # WHY：DatabaseConfig 是启动配置，但工作台切换需要让后续数据库切换沿用新策略。
                    self.database.config = replace(
                        self.database.config, replacement_policy=policy
                    )
                buffer_pool = self.database.buffer_pool.snapshot(0, 100)
                return {
                    "snapshot_at": now(),
                    "changed": changed,
                    "previous_policy": previous,
                    "replacement_policy": policy,
                    "buffer_pool": buffer_pool.to_dict(),
                    "note": "策略仅影响当前服务进程；现有缓存帧和累计统计保持不变，下一次淘汰开始采用新策略。",
                }

    def set_storage_protection(
        self, session: WebSession, enabled: object
    ) -> JsonObject:
        """【前端特供】在线切换系统页和索引页保护，不重启服务。"""

        if not isinstance(enabled, bool):
            raise YourSQLError("protect_page_types 必须是布尔值", "BAD_REQUEST")
        with self._switch_lock:
            with self.connection(session):
                session.connection.authorize("SECURITY")
                with self._lock:
                    if any(
                        task.state in {"queued", "running"}
                        for task in self._tasks.values()
                    ):
                        raise YourSQLError(
                            "当前仍有 SQL 任务执行，请等待完成后再切换", "SERVICE_BUSY"
                        )
                previous = self.database.buffer_pool.protect_page_types
                changed = self.database.buffer_pool.set_protect_page_types(enabled)
                if changed:
                    self.database.config = replace(
                        self.database.config, protect_page_types=enabled
                    )
                return {
                    "snapshot_at": now(),
                    "changed": changed,
                    "previous_protect_page_types": previous,
                    "protect_page_types": enabled,
                    "buffer_pool": self.database.buffer_pool.snapshot(0, 100).to_dict(),
                    "note": "页面类型保护已热加载；下一次淘汰开始按新开关选择 HEAP/INDEX 页。",
                }

    def run_storage_buffer_demo(
        self,
        session: WebSession,
        *,
        compare: object = False,
        prime_rounds: int = 3,
        scan_rounds: int = 2,
        probe_rounds: int = 8,
    ) -> JsonObject:
        """【前端特供】运行固定扫描污染实验并返回可视化对照指标。"""

        if not isinstance(compare, bool):
            raise YourSQLError("compare 必须是布尔值", "BAD_REQUEST")
        with self._switch_lock:
            with self.connection(session):
                session.connection.authorize("SECURITY")
                with self._lock:
                    if any(
                        task.state in {"queued", "running"}
                        for task in self._tasks.values()
                    ):
                        raise YourSQLError(
                            "当前仍有 SQL 任务执行，请等待完成后再运行实验", "SERVICE_BUSY"
                        )
                buffer_pool = self.database.buffer_pool
                if compare:
                    # WHY：对照实例直接复用同一个磁盘文件；先写回活动缓存，避免比较读到旧页。
                    buffer_pool.flush_all()
                result = run_buffer_pool_demo(
                    self.database.disk,
                    active_pool=None if compare else buffer_pool,
                    capacity=buffer_pool.capacity,
                    policy=buffer_pool.replacement_policy,
                    protect_page_types=buffer_pool.protect_page_types,
                    prime_rounds=prime_rounds,
                    scan_rounds=scan_rounds,
                    probe_rounds=probe_rounds,
                    compare=compare,
                )
                return {
                    "snapshot_at": now(),
                    "database_page_count": self.database.disk.page_count,
                    "buffer_pool": buffer_pool.snapshot(0, 100).to_dict(),
                    **result,
                }

    def resize_storage(
        self, session: WebSession, capacity: int
    ) -> JsonObject:
        """【前端特供】在线调整缓存页数，保持可用帧和统计连续。"""

        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise YourSQLError("缓存页数必须是整数", "BAD_REQUEST")
        if not 1 <= capacity <= 4096:
            raise YourSQLError("缓存页数范围应为 1–4096", "BAD_REQUEST")
        with self._switch_lock:
            with self.connection(session):
                session.connection.authorize("SECURITY")
                with self._lock:
                    if any(
                        task.state in {"queued", "running"}
                        for task in self._tasks.values()
                    ):
                        raise YourSQLError(
                            "当前仍有 SQL 任务执行，请等待完成后再调整缓存容量",
                            "SERVICE_BUSY",
                        )
                previous = self.database.buffer_pool.capacity
                try:
                    evicted = self.database.resize_buffer_pool(capacity)
                except StorageError as exc:
                    if exc.details.get("reason") != "pinned":
                        raise
                    raise YourSQLError(
                        "目标缓存容量小于当前正在使用的 pin 页数量",
                        "SERVICE_BUSY",
                    ) from exc
                changed = previous != capacity
                buffer_pool = self.database.buffer_pool.snapshot(0, 100)
                return {
                    "snapshot_at": now(),
                    "changed": changed,
                    "previous_capacity": previous,
                    "capacity": capacity,
                    "evicted_pages": evicted,
                    "buffer_pool": buffer_pool.to_dict(),
                    "note": "容量调整仅影响当前服务进程；扩容保留已有缓存帧，缩容按当前策略淘汰未 pin 页并写回脏页。",
                }

    def dialect(self) -> JsonObject:
        """返回工作台支持的 SQL 方言信息。"""
        return {
            "payload_codec": self.database.payload_codec.name,
            "keywords": sorted(set(KEYWORDS) | {"NULL", "TRUE", "FALSE"}),
            "types": [
                "INT",
                "INTEGER",
                "FLOAT",
                "DOUBLE",
                "REAL",
                "VARCHAR",
                "TEXT",
                "STRING",
                "BOOL",
                "BOOLEAN",
            ],
            "functions": [
                "COUNT",
                "SUM",
                "AVG",
                "MIN",
                "MAX",
                "ABS",
                "LOWER",
                "UPPER",
                "LENGTH",
                "COALESCE",
                "DATE",
            ],
            "limits": {
                "sql_chars": self.settings.max_sql_chars,
                "statements": self.settings.max_statements,
                "result_rows": self.settings.max_result_rows,
                "page_size": 500,
                "history_entries": self.settings.history_entries,
                "timeout_seconds": self.settings.max_query_timeout_seconds,
            },
            "limitations": [
                "提示基于关键字和可见 Schema，不是完整语义语言服务器。",
                "AST 暂不提供完整节点区间；有 Token 位置的语义错误精确定位，其余错误回退到语句起点。",
                "表达式类型根据结果推断，全 NULL 或空表达式列显示 UNKNOWN。",
            ],
        }

    def validate(self, session: WebSession, sql: str) -> JsonObject:
        """校验输入或内部状态是否满足约束。"""
        del session  # 路由已认证；语法验证不读取目录，允许 CREATE 后 INSERT 的脚本。
        statements = split_sql(sql, max_statements=self.settings.max_statements)
        diagnostics: list[JsonObject] = []
        for source in statements:
            try:
                Parser(tokenize(source.sql)).parse_one()
            except YourSQLError as exc:
                diagnostics.append(
                    error_info(
                        exc, source, sensitive="IDENTIFIED" in source.sql.upper()
                    )
                )
        return {
            "valid": not diagnostics,
            "diagnostics": diagnostics,
            "statements": [source.location() for source in statements],
            "scope": "syntax_only",
            "note": "这里只校验语法；执行时检查权限、表列与类型。",
        }

    def submit(
        self, session: WebSession, sql: str, timeout: float, row_limit: int
    ) -> QueryTask:
        """提交查询任务并返回任务标识。"""
        if not split_sql(sql, max_statements=self.settings.max_statements):
            raise YourSQLError("没有可执行的 SQL", "BAD_REQUEST")
        with self._lock:
            if self._stopped:
                raise YourSQLError("服务正在停止", "SERVICE_BUSY")
            if session.active_task:
                raise YourSQLError("当前连接已有执行中的任务", "SESSION_BUSY")
            if len(self._tasks) >= self.settings.max_tasks:
                completed = next(
                    (
                        key
                        for key, task in self._tasks.items()
                        if task.state not in {"queued", "running"}
                    ),
                    None,
                )
                if completed is None:
                    raise YourSQLError("任务队列已满", "RESOURCE_LIMIT")
                self._tasks.pop(completed)
            task = QueryTask(
                uuid4().hex,
                session.key,
                sql,
                now(),
                timeout,
                row_limit,
                deadline=monotonic() + timeout,
            )
            self._tasks[task.id] = task
            session.active_task = task.id
            self._worker.submit(self._run, session, task)
            return task

    def _run(self, session: WebSession, task: QueryTask) -> None:
        """在线程中执行查询任务并记录阶段状态。"""
        started = perf_counter()
        try:
            with self.connection(session):
                with self._lock:
                    task.state = "running"
                remaining_bytes = self.settings.result_retention_bytes
                for statement_index, source in enumerate(split_sql(
                    task.sql, max_statements=self.settings.max_statements
                )):
                    # WHY：默认不截断流水线步数，避免复杂 JOIN 丢失执行信息；可用 YOURSQL_TRACE_MAX_STEPS 恢复上限。
                    trace = ExecutionTrace(
                        min(task.deadline, session.expires_at),
                        task.cancel,
                        max_steps=self.settings.trace_max_steps,
                    )
                    stages: list[JsonObject] = []
                    statement_started = perf_counter()
                    statement_started_at = now()
                    item: JsonObject = {
                        "sql": redact_sql(source.sql),
                        "source": source.location(),
                        "stages": stages,
                        "columns": [],
                        "rows": [],
                        "affected_rows": 0,
                    }
                    trace_token = current_trace.set(trace)
                    try:
                        trace.check()
                        compilation = compile_observed(self.database, source, stages)
                        statement = compilation.statement
                        trace.interruptible = isinstance(
                            statement, (Select, Show, Explain)
                        )
                        before_io = self.database.disk.io_stats()
                        before_buffer = self.database.buffer_pool.stats()
                        execution_started = perf_counter()
                        result = self.database._execute_compilation(
                            compilation, optimized_plan=compilation.optimized_plan
                        )
                        execution_ms = (perf_counter() - execution_started) * 1000
                        rows = []
                        retained_bytes = 0
                        for row in result.rows[: task.row_limit]:
                            size = len(
                                json.dumps(row, ensure_ascii=False).encode("utf-8")
                            )
                            if size + retained_bytes > remaining_bytes:
                                break
                            rows.append(list(row))
                            retained_bytes += size
                        remaining_bytes -= retained_bytes
                        plan_estimate = None
                        if (
                            compilation.optimized_plan is not None
                            and "IDENTIFIED" not in source.sql.upper()
                        ):
                            estimate = self.database.estimate_plan(
                                compilation.optimized_plan
                            )
                            plan_estimate = {
                                "startup_cost": estimate.startup_cost,
                                "total_cost": estimate.total_cost,
                                "rows": estimate.rows,
                                "unit": "cost",
                                "model": "optimizer_statistics",
                            }
                        item.update(
                            {
                                "status": "success",
                                "columns": result_columns(
                                    self.database, statement, result
                                ),
                                "rows": rows,
                                "total_rows": len(result.rows),
                                "retained_rows": len(rows),
                                "truncated": len(rows) < len(result.rows),
                                "affected_rows": result.affected_rows,
                                "message": result.message,
                                "execution_ms": execution_ms,
                                "plan": compilation.plan.to_dict()
                                if "IDENTIFIED" not in source.sql.upper()
                                else None,
                                "stats": result.stats,
                                "plan_estimate": plan_estimate,
                            }
                        )
                        after_io = self.database.disk.io_stats()
                        after_buffer = self.database.buffer_pool.stats()
                        statistics = {
                            "engine": result.stats,
                            "execution_ms": execution_ms,
                            "io_delta": {
                                key: after_io[key] - before_io[key] for key in after_io
                            },
                            "buffer_delta": {
                                key: after_buffer[key] - before_buffer[key]
                                for key in (
                                    "hits",
                                    "misses",
                                    "evictions",
                                    "cold_hits",
                                    "hot_hits",
                                )
                            },
                            "returned_rows": len(result.rows),
                            "affected_rows": result.affected_rows,
                            "observed_steps": trace.steps,
                        }
                        self._finish_stages(stages, source, trace, statistics)
                    except Exception as exc:
                        error = error_info(
                            exc, source, sensitive="IDENTIFIED" in source.sql.upper()
                        )
                        item.update(
                            {
                                "status": "error",
                                "error": error,
                                "total_rows": 0,
                                "retained_rows": 0,
                            }
                        )
                        if not stages or stages[-1]["status"] != "error":
                            stages.append(
                                stage(
                                    "execution",
                                    "error",
                                    source,
                                    error=error,
                                    duration_ms=(perf_counter() - statement_started)
                                    * 1000,
                                )
                            )
                        task.error = error
                    finally:
                        current_trace.reset(trace_token)
                    item["elapsed_ms"] = (perf_counter() - statement_started) * 1000
                    # 阶段产物也有上限；不把原始内部对象或无限树送到浏览器。
                    for artifact in stages:
                        if (
                            len(json.dumps(artifact, ensure_ascii=False).encode())
                            > 128_000
                        ):
                            artifact.update(
                                {
                                    "data": {"truncated": True},
                                    "text": "产物超过 128 KiB，已省略；请缩短 SQL。",
                                    "truncated": True,
                                }
                            )
                    self._record_performance(
                        session,
                        task,
                        statement_index,
                        statement_started_at,
                        item,
                        queue_wait_ms=(
                            (started - task.submitted_monotonic) * 1000
                            if statement_index == 0
                            else 0.0
                        ),
                    )
                    with self._lock:
                        task.results.append(item)
                        self._history.setdefault(
                            session.connection.user.name.lower(),
                            deque(maxlen=self.settings.history_entries),
                        ).appendleft(
                            {
                                "id": uuid4().hex,
                                "task_id": task.id,
                                "sql": item["sql"],
                                "executed_at": now(),
                                "status": item["status"],
                                "elapsed_ms": item["elapsed_ms"],
                                "error_summary": str(task.error["code"])
                                if task.error
                                else None,
                                "redaction": "注释和字面量已移除，历史仅作追溯，不可直接重放。",
                            }
                        )
                    if task.error:
                        break
                with self._lock:
                    task.state = (
                        "cancelled"
                        if task.error and task.error["code"] == "CANCELLED"
                        else "timeout"
                        if task.error and task.error["code"] == "TIMEOUT"
                        else "error"
                        if task.error
                        else "success"
                    )
        except Exception as exc:
            with self._lock:
                task.error = error_info(exc, SQLSlice("", 0, 0, 1, 1))
                task.state = "error"
        finally:
            with self._lock:
                task.sql = ""  # 执行完成不保留未脱敏源码。
                task.elapsed_ms = (perf_counter() - started) * 1000
                session.active_task = None

    def _record_performance(
        self,
        session: WebSession,
        task: QueryTask,
        statement_index: int,
        started_at: str,
        item: JsonObject,
        *,
        queue_wait_ms: float,
    ) -> None:
        """把单条语句的阶段和访问统计转换为监控观测。"""
        stages = item.get("stages")
        stage_items = stages if isinstance(stages, list) else []
        compile_names = {
            "tokens",
            "ast",
            "binding",
            "logical_plan",
            "optimized_plan",
        }
        compile_ms = sum(
            _metric_number(stage_item.get("duration_ms"))
            for stage_item in stage_items
            if isinstance(stage_item, dict)
            and stage_item.get("name") in compile_names
        )
        statistics_data: JsonObject = {}
        for stage_item in reversed(stage_items):
            if (
                not isinstance(stage_item, dict)
                or stage_item.get("name") != "statistics"
            ):
                continue
            data = stage_item.get("data")
            if isinstance(data, dict):
                statistics_data = data
            break
        result_stats = item.get("stats")
        result_stats = result_stats if isinstance(result_stats, dict) else {}
        io_delta = statistics_data.get("io_delta")
        io_delta = io_delta if isinstance(io_delta, dict) else {}
        buffer_delta = statistics_data.get("buffer_delta")
        buffer_delta = buffer_delta if isinstance(buffer_delta, dict) else {}
        statement_ms = _metric_number(item.get("elapsed_ms"))
        total_ms = statement_ms + queue_wait_ms
        execute_ms = _metric_number(item.get("execution_ms"))
        error = item.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        observation: JsonObject = {
            "query_id": f"{task.id}:{statement_index}",
            "task_id": task.id,
            "statement_index": statement_index,
            "user": session.connection.user.name,
            "sql": item.get("sql", ""),
            "status": item.get("status", "error"),
            "started_at": started_at,
            "finished_at": now(),
            "total_ms": total_ms,
            "queue_wait_ms": queue_wait_ms,
            "compile_ms": compile_ms,
            "execute_ms": execute_ms,
            "materialize_ms": max(statement_ms - compile_ms - execute_ms, 0.0),
            "rows_examined": _metric_integer(result_stats.get("rows_examined")),
            "rows_returned": _metric_integer(item.get("total_rows")),
            "affected_rows": _metric_integer(item.get("affected_rows")),
            "page_reads": _metric_integer(io_delta.get("page_reads")),
            "page_writes": _metric_integer(io_delta.get("page_writes")),
            "cache_hits": _metric_integer(buffer_delta.get("hits")),
            "cache_misses": _metric_integer(buffer_delta.get("misses")),
            "cache_evictions": _metric_integer(buffer_delta.get("evictions")),
            "cache_cold_hits": _metric_integer(buffer_delta.get("cold_hits")),
            "cache_hot_hits": _metric_integer(buffer_delta.get("hot_hits")),
            "operator": result_stats.get("operator"),
            "error_code": error_code,
            "stages": stage_items,
            "plan": item.get("plan"),
            "plan_estimate": item.get("plan_estimate"),
        }
        try:
            self.monitor.record(observation)
        except (TypeError, ValueError, RecursionError):
            # WHY：监控是旁路能力，某个异常产物不可 JSON 化时不能影响查询提交方。
            return

    @staticmethod
    def _finish_stages(
        stages: list[JsonObject],
        source: SQLSlice,
        trace: ExecutionTrace,
        statistics: JsonObject,
    ) -> None:
        """补全查询阶段统计并生成最终阶段结果。"""
        stages.extend(
            [
                stage(
                    "executor",
                    "partial",
                    source,
                    data={
                        "implementation": "Database SQL evaluator",
                        "scans": trace.scans,
                    },
                    reason="SQL evaluator 已消费优化后的扫描路径；Volcano 算子树仍未作为查询主执行器实例化。",
                ),
                stage(
                    "storage",
                    "success",
                    source,
                    data={
                        "events": trace.events,
                        "dropped_events": trace.dropped_events,
                    },
                    reason="来自 BufferPool/DiskManager 的请求内访问事件，最多 256 项；未单独测量每次存储耗时。",
                ),
                stage(
                    "statistics",
                    "success",
                    source,
                    data=statistics,
                    duration_ms=float(statistics["execution_ms"]),
                ),
            ]
        )

    def task(
        self,
        session: WebSession,
        task_id: str,
        *,
        result_index: int | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> JsonObject:
        """返回查询任务的当前状态或结果。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.session_key != session.key:
                raise YourSQLError("任务不存在或结果已淘汰", "NOT_FOUND")
            if result_index is not None:
                if result_index >= len(task.results):
                    raise YourSQLError("结果不存在", "NOT_FOUND")
                item = dict(task.results[result_index])
                item["rows"] = item["rows"][offset : offset + limit]
                return {**item, "offset": offset, "limit": limit}
            return {
                "id": task.id,
                "status": task.state,
                "submitted_at": task.submitted_at,
                "elapsed_ms": task.elapsed_ms,
                "error": task.error,
                "cancel_requested": task.cancel.is_set(),
                "results": [
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {"rows", "stages", "plan", "stats"}
                    }
                    for item in task.results
                ],
            }

    def cancel(self, session: WebSession, task_id: str) -> JsonObject:
        """取消尚未完成的查询任务。"""
        with self._lock:
            self.task(session, task_id)
            task = self._tasks[task_id]
            if task.state in {"queued", "running"}:
                task.cancel.set()
            return {
                "id": task.id,
                "status": task.state,
                "cancel_requested": task.cancel.is_set(),
                "note": "只读查询可协作取消；写语句执行中不会强制中断，后续语句将在边界停止。",
            }

    def history(self, session: WebSession, offset: int, limit: int) -> JsonObject:
        """返回当前会话保留的查询历史。"""
        with self._lock:
            items = list(self._history.get(session.connection.user.name.lower(), ()))
            return {
                "items": items[offset : offset + limit],
                "total": len(items),
                "offset": offset,
                "limit": limit,
                "retention": f"服务进程内每个用户最近 {self.settings.history_entries} 条；重启清空；不保存凭据、注释及字面量。",
            }

    def monitoring_summary(self, session: WebSession) -> JsonObject:
        """【前端特供】返回性能看板摘要和最近缓存淘汰事件。"""
        with self.connection(session):
            session.connection.authorize("SECURITY")
            summary = self.monitor.summary()
            summary["storage_events"] = self.database.buffer_pool.events(100)
            summary["storage_policy"] = self.database.buffer_pool.replacement_policy
            return summary

    def monitoring_queries(
        self,
        session: WebSession,
        *,
        slow_only: bool,
        limit: int,
        attention_only: bool = False,
    ) -> JsonObject:
        """【前端特供】返回性能看板的查询列表。"""
        with self.connection(session):
            session.connection.authorize("SECURITY")
            return self.monitor.queries(
                slow_only=slow_only,
                attention_only=attention_only,
                limit=limit,
            )

    def monitoring_detail(self, session: WebSession, query_id: str) -> JsonObject:
        """【前端特供】返回单条查询的阶段明细和执行计划。"""
        with self.connection(session):
            session.connection.authorize("SECURITY")
            return self.monitor.detail(query_id)

    def monitoring_history(
        self, session: WebSession, query_id: str, *, limit: int
    ) -> JsonObject:
        """【前端特供】返回同一 SQL 的历史执行统计序列。"""
        with self.connection(session):
            task_id, separator, raw_index = query_id.rpartition(":")
            if not separator or not raw_index.isdigit():
                raise YourSQLError("查询监控记录不存在或已淘汰", "NOT_FOUND")
            self.task(session, task_id, result_index=int(raw_index))
            return self.monitor.history(
                query_id,
                limit=limit,
                user=session.connection.user.name,
            )

    def monitoring_statistics(self, session: WebSession, query_id: str) -> JsonObject:
        """【前端特供】返回当前用户单条查询的轻量执行统计。"""
        with self.connection(session):
            task_id, separator, raw_index = query_id.rpartition(":")
            if not separator or not raw_index.isdigit():
                raise YourSQLError("查询监控记录不存在或已淘汰", "NOT_FOUND")
            self.task(session, task_id, result_index=int(raw_index))
            return self.monitor.statistics(
                query_id,
                user=session.connection.user.name,
            )

    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        with self._lock:
            self._stopped = True
            for task in self._tasks.values():
                task.cancel.set()
        self._worker.shutdown(wait=True)
        self.monitor.close()
        with self.database._lock:
            self._sessions.clear()
