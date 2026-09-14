"""数据库运行配置和日志默认值。"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .codec import PayloadCodecName, validate_payload_codec

# HOW：命令行和 Web 工作台共用这个相对项目目录的默认文件位置，避免把运行数据散落在仓库根目录。
DEFAULT_DATABASE_PATH = Path("data") / "workbench.db"
DEFAULT_AUDIT_LOG_DIR = Path("logs")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def default_audit_path(current_date: date | None = None) -> Path:
    """返回按日期分文件的默认安全审计日志路径。"""
    log_date = date.today() if current_date is None else current_date
    return DEFAULT_AUDIT_LOG_DIR / f"audit-{log_date.isoformat()}.jsonl"


def load_dotenv(path: str | Path = ".env") -> Path | None:
    """加载本地 dotenv 文件；已有系统环境变量不会被覆盖。"""

    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    if not env_path.is_file():
        return None
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, raw_value = (part.strip() for part in line.split("=", 1))
        if not _ENV_NAME.fullmatch(name):
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
            if raw_value.strip().startswith('"'):
                value = value.replace('\\"', '"').replace("\\n", "\n")
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(name, value)
    return env_path


def _env_text(name: str, default: str) -> str:
    """读取字符串环境变量，并在未设置时返回默认值。"""
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量，并接受常见的 true/false 写法。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false")


def _env_int(
    name: str, default: int, *, minimum: int = 1, maximum: int | None = None
) -> int:
    """读取整数环境变量，并校验范围和默认值。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        bound = f"{minimum}–{maximum}" if maximum is not None else f"不小于 {minimum}"
        raise ValueError(f"{name} 必须在 {bound} 范围内")
    return parsed


def _env_float(name: str, default: float, *, minimum: float = 0.001) -> float:
    """读取浮点环境变量，并校验最小值。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if parsed < minimum:
        raise ValueError(f"{name} 必须不小于 {minimum}")
    return parsed


def _env_optional_int(name: str, default: int | None = None) -> int | None:
    """读取可选整数环境变量；空值表示未启用限制。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return _env_int(name, 1)


def _env_origins(name: str = "YOURSQL_ALLOWED_ORIGINS") -> tuple[str, ...]:
    """读取并规范化允许的跨域来源列表。"""
    value = os.getenv(name, "")
    return tuple(origin.strip() for origin in value.split(",") if origin.strip())


def _env_payload_codec(name: str = "YOURSQL_PAYLOAD_CODEC") -> PayloadCodecName:
    """读取 payload 编码开关。"""

    return validate_payload_codec(_env_text(name, "json"))


@dataclass(frozen=True)
class DatabaseConfig:
    """控制页大小、缓存和字符串长度限制。"""

    page_size: int = 4096
    buffer_pool_size: int = 64
    replacement_policy: str = "lru"
    max_varchar_length: int = 1_000_000
    payload_codec: PayloadCodecName = "json"

    @classmethod
    def from_environment(cls) -> "DatabaseConfig":
        """从 YOURSQL_* 环境变量构造存储配置。"""

        return cls(
            page_size=_env_int("YOURSQL_PAGE_SIZE", cls.page_size, minimum=512),
            buffer_pool_size=_env_int("YOURSQL_BUFFER_POOL_SIZE", cls.buffer_pool_size),
            replacement_policy=_env_text(
                "YOURSQL_REPLACEMENT_POLICY", cls.replacement_policy
            ),
            max_varchar_length=_env_int(
                "YOURSQL_MAX_VARCHAR_LENGTH", cls.max_varchar_length
            ),
            payload_codec=_env_payload_codec(),
        )

    def __post_init__(self) -> None:
        """完成数据类初始化后的派生状态设置。"""
        if self.page_size < 512 or self.page_size & (self.page_size - 1):
            raise ValueError("page_size 必须是不小于 512 的二次幂")
        if self.buffer_pool_size < 1:
            raise ValueError("buffer_pool_size 必须为正数")
        if self.replacement_policy.lower() not in {"lru", "fifo"}:
            raise ValueError("replacement_policy 只能是 lru 或 fifo")
        if self.max_varchar_length < 1:
            raise ValueError("max_varchar_length 必须为正数")
        object.__setattr__(self, "payload_codec", validate_payload_codec(self.payload_codec))


@dataclass(frozen=True)
class RuntimeConfig:
    """控制 Web 工作台、查询边界和任务保留策略。"""

    database_path: str = str(DEFAULT_DATABASE_PATH)
    host: str = "127.0.0.1"
    port: int = 8080
    allowed_origins: tuple[str, ...] = ()
    legacy_anonymous: bool = False
    session_ttl_seconds: float = 3600
    request_timeout_seconds: float = 10
    default_query_timeout_seconds: int = 15
    max_query_timeout_seconds: int = 30
    default_result_rows: int = 1000
    max_result_rows: int = 5000
    max_request_body_bytes: int = 262_144
    max_sql_chars: int = 64_000
    max_statements: int = 32
    max_sessions: int = 64
    max_tasks: int = 32
    history_entries: int = 200
    result_retention_bytes: int = 2_000_000
    trace_max_steps: int | None = None
    payload_codec: PayloadCodecName = "json"
    database_config: DatabaseConfig = field(default_factory=DatabaseConfig)

    @classmethod
    def from_environment(cls) -> "RuntimeConfig":
        """读取后端运行环境；空的 YOURSQL_TRACE_MAX_STEPS 表示不启用步数硬上限。"""

        config = cls(
            database_path=_env_text("YOURSQL_DATABASE", str(DEFAULT_DATABASE_PATH)),
            host=_env_text("YOURSQL_HOST", "127.0.0.1"),
            port=_env_int("YOURSQL_PORT", 8080, minimum=1, maximum=65_535),
            allowed_origins=_env_origins(),
            legacy_anonymous=_env_bool("YOURSQL_LEGACY_ANONYMOUS", False),
            session_ttl_seconds=_env_float("YOURSQL_SESSION_TTL_SECONDS", 3600),
            request_timeout_seconds=_env_float("YOURSQL_REQUEST_TIMEOUT_SECONDS", 10),
            default_query_timeout_seconds=_env_int("YOURSQL_QUERY_TIMEOUT_SECONDS", 15),
            max_query_timeout_seconds=_env_int("YOURSQL_MAX_QUERY_TIMEOUT_SECONDS", 30),
            default_result_rows=_env_int("YOURSQL_DEFAULT_RESULT_ROWS", 1000),
            max_result_rows=_env_int("YOURSQL_MAX_RESULT_ROWS", 5000),
            max_request_body_bytes=_env_int("YOURSQL_MAX_REQUEST_BODY_BYTES", 262_144),
            max_sql_chars=_env_int("YOURSQL_MAX_SQL_CHARS", 64_000),
            max_statements=_env_int("YOURSQL_MAX_STATEMENTS", 32),
            max_sessions=_env_int("YOURSQL_MAX_SESSIONS", 64),
            max_tasks=_env_int("YOURSQL_MAX_TASKS", 32),
            history_entries=_env_int("YOURSQL_HISTORY_ENTRIES", 200),
            result_retention_bytes=_env_int(
                "YOURSQL_RESULT_RETENTION_BYTES", 2_000_000
            ),
            trace_max_steps=_env_optional_int("YOURSQL_TRACE_MAX_STEPS"),
            payload_codec=_env_payload_codec(),
            database_config=DatabaseConfig.from_environment(),
        )
        if config.default_query_timeout_seconds > config.max_query_timeout_seconds:
            raise ValueError(
                "YOURSQL_QUERY_TIMEOUT_SECONDS 不能大于 YOURSQL_MAX_QUERY_TIMEOUT_SECONDS"
            )
        if config.default_result_rows > config.max_result_rows:
            raise ValueError(
                "YOURSQL_DEFAULT_RESULT_ROWS 不能大于 YOURSQL_MAX_RESULT_ROWS"
            )
        return config

    def __post_init__(self) -> None:
        """完成数据类初始化后的派生状态设置。"""
        if not self.database_path.strip():
            raise ValueError("database_path 不能为空")
        if not self.host.strip():
            raise ValueError("host 不能为空")
        if not 1 <= self.port <= 65_535:
            raise ValueError("port 必须在 1–65535 范围内")
        if self.default_query_timeout_seconds > self.max_query_timeout_seconds:
            raise ValueError("默认查询超时不能大于最大查询超时")
        if self.default_result_rows > self.max_result_rows:
            raise ValueError("默认结果行数不能大于最大结果行数")
        positive_limits = {
            "session_ttl_seconds": self.session_ttl_seconds,
            "request_timeout_seconds": self.request_timeout_seconds,
            "default_query_timeout_seconds": self.default_query_timeout_seconds,
            "max_query_timeout_seconds": self.max_query_timeout_seconds,
            "default_result_rows": self.default_result_rows,
            "max_result_rows": self.max_result_rows,
            "max_request_body_bytes": self.max_request_body_bytes,
            "max_sql_chars": self.max_sql_chars,
            "max_statements": self.max_statements,
            "max_sessions": self.max_sessions,
            "max_tasks": self.max_tasks,
            "history_entries": self.history_entries,
            "result_retention_bytes": self.result_retention_bytes,
        }
        invalid = next(
            (name for name, value in positive_limits.items() if value <= 0), None
        )
        if invalid is not None:
            raise ValueError(f"{invalid} 必须为正数")
        if self.trace_max_steps is not None and self.trace_max_steps < 1:
            raise ValueError("trace_max_steps 必须为正数或 None")
        object.__setattr__(self, "payload_codec", validate_payload_codec(self.payload_codec))


def configure_logging(
    level: int = logging.WARNING, log_file: str | Path | None = None
) -> logging.Logger:
    """配置一次包级日志器并返回它。"""

    logger = logging.getLogger("yoursql")
    logger.setLevel(level)
    if not logger.handlers:
        handler: logging.Handler = logging.StreamHandler()
        if log_file is not None:
            handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
    return logger
