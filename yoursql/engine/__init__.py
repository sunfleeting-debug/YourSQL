"""执行引擎公共 API；服务与运行时入口按需加载，避免层间循环导入。"""

from yoursql.engine.catalog import Catalog, IndexMetadata, TableMetadata, ViewMetadata
from yoursql.engine.security import AuditLog, RBAC, Role, SecurityManager, Session, User

__all__ = [
    "AuditLog",
    "Catalog",
    "Database",
    "DatabaseHTTPServer",
    "HTTPService",
    "IndexMetadata",
    "RBAC",
    "Role",
    "SecurityManager",
    "Session",
    "User",
    "SSHAdapterError",
    "SSHCommandClient",
    "SSHStdioServer",
    "TableMetadata",
    "ViewMetadata",
    "serve_http",
    "serve_ssh_stdio",
]


def __getattr__(name: str) -> object:
    """按需解析运行时和协议服务，保持公共导出但不提前拉起依赖链。"""

    if name == "Database":
        from yoursql.engine.runtime.database import Database

        return Database
    if name in {
        "DatabaseHTTPServer",
        "ERROR_STATUS",
        "HTTPService",
        "MAX_BODY",
        "MAX_DATABASE_IMPORT",
        "STORAGE_PAGE_LIMIT",
        "serve_http",
        "SSHAdapterError",
        "SSHCommandClient",
        "SSHStdioServer",
        "serve_ssh_stdio",
    }:
        from yoursql.engine import services

        return getattr(services, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
