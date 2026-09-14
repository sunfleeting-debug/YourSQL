"""身份、权限与审计边界。

安全模块只负责回答“谁在访问”和“是否允许访问”，不依赖 HTTP、SSH
或查询执行细节。这样服务层可以复用同一套 RBAC 与会话对象。
"""

from .audit import AuditLog
from .auth import RBAC, Role, User
from .manager import SecurityManager
from .session import Session

__all__ = ["AuditLog", "RBAC", "Role", "SecurityManager", "Session", "User"]
