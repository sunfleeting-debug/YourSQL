"""会话身份和权限入口。"""

from __future__ import annotations

from dataclasses import dataclass

from .auth import RBAC, User


@dataclass
class Session:
    user: User
    rbac: RBAC

    def authorize(self, action: str, object_name: str | None = None) -> None:
        self.rbac.check(self.user, action, object_name)
