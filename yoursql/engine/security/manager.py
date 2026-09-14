"""兼容名称：安全管理器复用 RBAC 和审计日志。"""

from .audit import AuditLog
from .auth import RBAC, Role, User


class SecurityManager:
    """集中暴露权限对象，方便服务层注入。"""

    def __init__(self, audit: AuditLog | None = None) -> None:
        """初始化实例所需的状态和依赖。"""
        self.rbac = RBAC()
        self.audit = audit or AuditLog()


__all__ = ["AuditLog", "RBAC", "Role", "SecurityManager", "User"]
