"""会话身份和权限入口。"""

from __future__ import annotations

from dataclasses import dataclass

from yoursql.engine.security.auth import RBAC, User


@dataclass
class Session:
    user: User
    rbac: RBAC

    def authorize(self, action: str, object_name: str | None = None) -> None:
        """检查当前主体是否拥有所需权限。"""
        self.rbac.check(self.user, action, object_name)
