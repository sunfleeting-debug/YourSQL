"""无需 Python SSH 依赖的 stdio 协议适配，可挂到 OpenSSH 强制命令。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
from typing import TextIO

from ...common import JsonObject
from ...common.errors import YourSQLError
from ..runtime.database import Database


class SSHAdapterError(RuntimeError):
    """SSH 客户端或 stdio 协议输入错误。"""


class SSHStdioServer:
    """每行读取 SQL，每行返回一份 JSON ExecutionResult。"""

    def __init__(
        self,
        database: Database,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.database = database
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout

    def run(self, lines: Iterable[str] | None = None) -> int:
        """运行当前任务并返回状态码或结果。"""
        source = self.stdin if lines is None else lines
        for line in source:
            sql = line.strip()
            if not sql:
                continue
            try:
                payload = self.database.execute(sql).as_dict()
            except YourSQLError as exc:
                payload = exc.as_dict()
            except Exception as exc:
                payload = {"error": "INTERNAL_ERROR", "message": str(exc)}
            self.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.stdout.flush()
        return 0


class SSHCommandClient:
    """调用系统 ssh 程序连接远端 stdio 服务。"""

    def __init__(self, executable: str = "ssh") -> None:
        """初始化实例所需的状态和依赖。"""
        self.executable = executable

    def execute(
        self, destination: str, sql: str, *, remote_command: str = "yoursql --stdio"
    ) -> JsonObject:
        """执行操作并返回执行结果。"""
        if shutil.which(self.executable) is None:
            raise SSHAdapterError(f"未找到外部 SSH 程序: {self.executable}")
        completed = subprocess.run(
            [self.executable, destination, remote_command],
            input=sql + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise SSHAdapterError(
                completed.stderr.strip() or f"ssh exit code {completed.returncode}"
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SSHAdapterError("SSH 服务返回的不是 JSON") from exc
        if not isinstance(value, dict):
            raise SSHAdapterError("SSH 服务返回的 JSON 不是对象")
        return value


def serve_ssh_stdio(database: Database) -> int:
    """通过标准输入输出启动 SSH 兼容服务。"""
    return SSHStdioServer(database).run()


__all__ = ["SSHAdapterError", "SSHCommandClient", "SSHStdioServer", "serve_ssh_stdio"]
