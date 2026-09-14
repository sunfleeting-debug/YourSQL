"""数据库运行时核心。

`Database` 的实现位于本包，运行时边界与服务、安全、元数据模块在目录上分开。
"""

from .database import Database

__all__ = ["Database"]
