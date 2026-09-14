"""本地教学环境的用户、角色和对象权限。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from ...common.errors import AuthorizationError


def _hash_password(password: str, salt: bytes | None = None) -> str:
    chosen = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), chosen, 120_000)
    return f"{chosen.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        salt_text, digest_text = encoded.split("$", 1)
        actual = _hash_password(password, bytes.fromhex(salt_text)).split("$", 1)[1]
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, digest_text)


@dataclass
class Role:
    name: str
    privileges: set[str] = field(default_factory=set)


@dataclass
class User:
    name: str
    password_hash: str
    roles: set[str] = field(default_factory=set)
    direct_privileges: set[str] = field(default_factory=set)


class RBAC:
    """默认 admin/admin 只用于本地教学与自动化测试。"""

    SERIALIZATION_VERSION = 1

    def __init__(self) -> None:
        self.roles: dict[str, Role] = {}
        self.users: dict[str, User] = {}
        self.create_role("admin")
        self.create_user("admin", "admin", roles=("admin",))
        self.roles["admin"].privileges.add("*")

    @staticmethod
    def _key(name: str) -> str:
        return name.strip().lower()

    def create_role(self, name: str) -> Role:
        key = self._key(name)
        if not key or key in self.roles:
            raise AuthorizationError(f"角色 {name!r} 已存在或为空")
        role = Role(name)
        self.roles[key] = role
        return role

    def create_user(
        self, name: str, password: str, *, roles: Iterable[str] = ()
    ) -> User:
        key = self._key(name)
        if not key or key in self.users:
            raise AuthorizationError(f"用户 {name!r} 已存在或为空")
        role_keys = {self._key(role) for role in roles}
        missing = role_keys.difference(self.roles)
        if missing:
            raise AuthorizationError(f"角色不存在: {', '.join(sorted(missing))}")
        user = User(name, _hash_password(password), role_keys)
        self.users[key] = user
        return user

    def authenticate(self, name: str, password: str) -> User:
        user = self.users.get(self._key(name))
        if user is None or not _verify_password(password, user.password_hash):
            raise AuthorizationError("用户名或密码错误")
        return user

    def grant(
        self, privilege: str, *, user: str | None = None, role: str | None = None
    ) -> None:
        if (user is None) == (role is None):
            raise AuthorizationError("GRANT 必须指定一个用户或角色")
        target = (
            self.users.get(self._key(user))
            if user is not None
            else self.roles.get(self._key(role or ""))
        )
        if target is None:
            raise AuthorizationError("授权目标不存在")
        if user is not None:
            target.direct_privileges.add(privilege.upper())
        else:
            target.privileges.add(privilege.upper())

    def revoke(
        self, privilege: str, *, user: str | None = None, role: str | None = None
    ) -> None:
        if (user is None) == (role is None):
            raise AuthorizationError("REVOKE 必须指定一个用户或角色")
        target = (
            self.users.get(self._key(user))
            if user is not None
            else self.roles.get(self._key(role or ""))
        )
        if target is None:
            raise AuthorizationError("撤权目标不存在")
        privilege_key = privilege.upper()
        if role is not None and self._key(role) == "admin" and privilege_key == "*":
            raise AuthorizationError("不能撤销 admin 角色的全部权限")
        if user is not None:
            target.direct_privileges.discard(privilege_key)
        else:
            target.privileges.discard(privilege_key)

    def get_user(self, name: str) -> User:
        """按名称读取用户，供 SQL 管理命令展示有效权限。"""

        user = self.users.get(self._key(name))
        if user is None:
            raise AuthorizationError("用户不存在")
        return user

    def get_role(self, name: str) -> Role:
        """按名称读取角色，供 SQL 管理命令展示权限。"""

        role = self.roles.get(self._key(name))
        if role is None:
            raise AuthorizationError("角色不存在")
        return role

    def privileges_for(
        self, *, user: str | None = None, role: str | None = None
    ) -> tuple[str, ...]:
        """返回用户或角色的有效权限集合。"""

        if (user is None) == (role is None):
            raise AuthorizationError("查看权限必须指定一个用户或角色")
        if role is not None:
            return tuple(sorted(self.get_role(role).privileges))

        target = self.get_user(user or "")
        privileges = set(target.direct_privileges)
        for role_name in target.roles:
            assigned = self.roles.get(role_name)
            if assigned is not None:
                privileges.update(assigned.privileges)
        return tuple(sorted(privileges))

    def to_dict(self) -> dict[str, object]:
        """把用户、角色和权限转换成可持久化的 JSON 中间表示。"""

        return {
            "version": self.SERIALIZATION_VERSION,
            "roles": [
                {"name": role.name, "privileges": sorted(role.privileges)}
                for role in sorted(
                    self.roles.values(), key=lambda item: item.name.lower()
                )
            ],
            "users": [
                {
                    "name": user.name,
                    "password_hash": user.password_hash,
                    "roles": sorted(user.roles),
                    "direct_privileges": sorted(user.direct_privileges),
                }
                for user in sorted(
                    self.users.values(), key=lambda item: item.name.lower()
                )
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object] | None) -> "RBAC":
        """从 JSON 中间表示恢复 RBAC；空值时使用默认管理员。"""

        if not value:
            return cls()
        try:
            version = int(value.get("version", 1))
        except (TypeError, ValueError) as exc:
            raise AuthorizationError("权限目录版本无效") from exc
        if version > cls.SERIALIZATION_VERSION:
            raise AuthorizationError(f"不支持的权限目录版本 {version}")

        raw_roles = value.get("roles", [])
        raw_users = value.get("users", [])
        if not isinstance(raw_roles, list) or not isinstance(raw_users, list):
            raise AuthorizationError("权限目录中的 roles/users 不是数组")

        rbac = cls.__new__(cls)
        rbac.roles = {}
        rbac.users = {}
        for raw_role in raw_roles:
            if not isinstance(raw_role, Mapping):
                raise AuthorizationError("权限目录中的角色项不是对象")
            name = raw_role.get("name")
            if not isinstance(name, str):
                raise AuthorizationError("权限目录中的角色名无效")
            role = rbac.create_role(name)
            role.privileges = cls._read_strings(
                raw_role.get("privileges", []), "角色权限"
            )

        for raw_user in raw_users:
            if not isinstance(raw_user, Mapping):
                raise AuthorizationError("权限目录中的用户项不是对象")
            name = raw_user.get("name")
            password_hash = raw_user.get("password_hash")
            if (
                not isinstance(name, str)
                or not isinstance(password_hash, str)
                or not password_hash
            ):
                raise AuthorizationError("权限目录中的用户凭据无效")
            key = rbac._key(name)
            if not key or key in rbac.users:
                raise AuthorizationError(f"权限目录中的用户 {name!r} 重复或为空")
            roles = rbac._read_strings(raw_user.get("roles", []), "用户角色")
            missing = roles.difference(rbac.roles)
            if missing:
                raise AuthorizationError(
                    f"权限目录中的角色不存在: {', '.join(sorted(missing))}"
                )
            rbac.users[key] = User(
                name=name,
                password_hash=password_hash,
                roles=roles,
                direct_privileges=cls._read_strings(
                    raw_user.get("direct_privileges", []), "用户权限"
                ),
            )

        admin_role = rbac.roles.get("admin")
        if (
            rbac.users.get("admin") is None
            or admin_role is None
            or "*" not in admin_role.privileges
        ):
            raise AuthorizationError("权限目录缺少有效的 admin 引导账号")
        return rbac

    @staticmethod
    def _read_strings(value: object, context: str) -> set[str]:
        if not isinstance(value, list):
            raise AuthorizationError(f"权限目录中的{context}不是数组")
        result: set[str] = set()
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise AuthorizationError(f"权限目录中的{context}包含无效项")
            result.add(item.lower() if context == "用户角色" else item.upper())
        return result

    def check(self, user: User, action: str, object_name: str | None = None) -> None:
        action_key = action.upper()
        candidates = {action_key, "*"}
        if object_name:
            candidates.update(
                {f"{action_key} {object_name.upper()}", f"* {object_name.upper()}"}
            )
        privileges = set(user.direct_privileges)
        for role_name in user.roles:
            role = self.roles.get(role_name)
            if role is not None:
                privileges.update(role.privileges)
        if not candidates.intersection(privileges):
            target = f" on {object_name}" if object_name else ""
            raise AuthorizationError(
                f"用户 {user.name!r} 缺少 {action_key}{target} 权限"
            )
