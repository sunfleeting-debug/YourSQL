"""对外服务适配层。

这里放置 HTTP、SSH、工作台和存储检查等协议边界。HTTP、Workbench 和
storage inspection 属于【前端特供】适配；SSH 是独立的命令协议。它们可以
调用引擎，但不应把协议细节反向带入 SQL、存储或安全核心。
"""

from yoursql.engine.services.http import (
    DatabaseHTTPServer,
    ERROR_STATUS,
    HTTPService,
    MAX_BODY,
    MAX_DATABASE_IMPORT,
    STORAGE_PAGE_LIMIT,
    serve_http,
)
from yoursql.engine.services.inspection import (
    authorize_storage,
    inspect_index,
    inspect_page,
    storage_cache_snapshot,
    storage_index_snapshot,
    storage_page_changes,
    storage_snapshot,
)
from yoursql.engine.services.ssh import SSHAdapterError, SSHCommandClient, SSHStdioServer, serve_ssh_stdio
from yoursql.engine.services.workbench import QueryTask, WebSession, Workbench
from yoursql.engine.services.workbench_sql import MAX_SQL, SQLSlice, redact_sql, split_sql

__all__ = [
    "DatabaseHTTPServer",
    "ERROR_STATUS",
    "HTTPService",
    "MAX_BODY",
    "MAX_DATABASE_IMPORT",
    "MAX_SQL",
    "QueryTask",
    "SQLSlice",
    "SSHAdapterError",
    "SSHCommandClient",
    "SSHStdioServer",
    "WebSession",
    "Workbench",
    "authorize_storage",
    "inspect_index",
    "inspect_page",
    "redact_sql",
    "serve_http",
    "serve_ssh_stdio",
    "split_sql",
    "storage_cache_snapshot",
    "storage_index_snapshot",
    "storage_page_changes",
    "storage_snapshot",
    "STORAGE_PAGE_LIMIT",
]
