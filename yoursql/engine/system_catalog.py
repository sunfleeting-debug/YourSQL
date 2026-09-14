"""基于现有 Heap 的内部权限表及其 admin-only 只读系统视图。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..common.errors import CatalogError
from ..common.types import Column, DataType, PageId, Schema
from .security.auth import RBAC
from .catalog import TableMetadata

if TYPE_CHECKING:
    from .runtime.database import Database


@dataclass(frozen=True)
class SystemTableSpec:
    """一个内部表的固定名称和模式。"""

    name: str
    schema: Schema


@dataclass(frozen=True)
class SystemViewSpec:
    """一个安全系统视图的固定名称、模式和查询定义。"""

    name: str
    schema: Schema
    definition_sql: str


def _schema(*columns: Column) -> Schema:
    """构造系统表使用的 Schema。"""
    return Schema.from_iterable(columns)


SYSTEM_TABLE_SPECS: tuple[SystemTableSpec, ...] = (
    SystemTableSpec(
        "_sys_users",
        _schema(
            Column("user_name", DataType.VARCHAR, nullable=False, unique=True),
            Column("password_hash", DataType.VARCHAR, nullable=False),
        ),
    ),
    SystemTableSpec(
        "_sys_roles",
        _schema(Column("role_name", DataType.VARCHAR, nullable=False, unique=True)),
    ),
    SystemTableSpec(
        "_sys_role_members",
        _schema(
            Column("member_type", DataType.VARCHAR, nullable=False),
            Column("member_name", DataType.VARCHAR, nullable=False),
            Column("role_name", DataType.VARCHAR, nullable=False),
            Column("admin_option", DataType.BOOLEAN, nullable=False),
            Column("inherit_option", DataType.BOOLEAN, nullable=False),
            Column("set_option", DataType.BOOLEAN, nullable=False),
        ),
    ),
    SystemTableSpec(
        "_sys_privileges",
        _schema(
            Column("grantee_type", DataType.VARCHAR, nullable=False),
            Column("grantee_name", DataType.VARCHAR, nullable=False),
            Column("privilege", DataType.VARCHAR, nullable=False),
            Column("object_name", DataType.VARCHAR),
            Column("grantor", DataType.VARCHAR, nullable=False),
            Column("grant_option", DataType.BOOLEAN, nullable=False),
        ),
    ),
)


SYSTEM_VIEW_SPECS: tuple[SystemViewSpec, ...] = (
    # WHY：用户表仍然不能读取 password_hash；管理界面可通过存储检查查看受控的原始页。
    SystemViewSpec(
        "sys_users",
        _schema(Column("user_name", DataType.VARCHAR, nullable=False, unique=True)),
        "SELECT user_name FROM _sys_users",
    ),
    SystemViewSpec(
        "sys_roles",
        _schema(Column("role_name", DataType.VARCHAR, nullable=False, unique=True)),
        "SELECT role_name FROM _sys_roles",
    ),
    SystemViewSpec(
        "sys_role_members",
        _schema(
            Column("member_type", DataType.VARCHAR, nullable=False),
            Column("member_name", DataType.VARCHAR, nullable=False),
            Column("role_name", DataType.VARCHAR, nullable=False),
            Column("admin_option", DataType.BOOLEAN, nullable=False),
            Column("inherit_option", DataType.BOOLEAN, nullable=False),
            Column("set_option", DataType.BOOLEAN, nullable=False),
        ),
        "SELECT member_type, member_name, role_name, admin_option, inherit_option, set_option FROM _sys_role_members",
    ),
    SystemViewSpec(
        "sys_privileges",
        _schema(
            Column("grantee_type", DataType.VARCHAR, nullable=False),
            Column("grantee_name", DataType.VARCHAR, nullable=False),
            Column("privilege", DataType.VARCHAR, nullable=False),
            Column("object_name", DataType.VARCHAR),
            Column("grantor", DataType.VARCHAR, nullable=False),
            Column("grant_option", DataType.BOOLEAN, nullable=False),
        ),
        "SELECT grantee_type, grantee_name, privilege, object_name, grantor, grant_option FROM _sys_privileges",
    ),
)


@dataclass
class _RoleRecord:
    name: str
    privileges: set[str]


@dataclass
class _UserRecord:
    name: str
    password_hash: str
    roles: set[str]
    direct_privileges: set[str]


def _text(value: object, context: str) -> str:
    """从系统表行中读取文本字段。"""
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"内部权限表中的{context}无效")
    return value


def _bool(value: object, context: str) -> bool:
    """从系统表行中读取布尔字段。"""
    if not isinstance(value, bool):
        raise CatalogError(f"内部权限表中的{context}必须是 BOOLEAN")
    return value


def _privilege_parts(privilege: str) -> tuple[str, str | None]:
    """拆分权限键中的动作和对象部分。"""
    normalized = privilege.upper().strip()
    action, separator, object_name = normalized.partition(" ")
    return action, object_name.strip() if separator and object_name.strip() else None


def _join_privilege(privilege: str, object_name: str | None) -> str:
    """将权限动作和对象重新组合为权限键。"""
    action = privilege.upper().strip()
    return action if object_name is None else f"{action} {object_name.upper().strip()}"


class SystemCatalog:
    """把 RBAC 快照映射到固定的内部堆表，并建立安全的系统视图层。"""

    def __init__(self, database: Database) -> None:
        """初始化实例所需的状态和依赖。"""
        self.database = database

    @staticmethod
    def specs() -> tuple[SystemTableSpec, ...]:
        """返回系统对象的规格定义。"""
        return SYSTEM_TABLE_SPECS

    @staticmethod
    def view_specs() -> tuple[SystemViewSpec, ...]:
        """返回系统视图的规格定义。"""
        return SYSTEM_VIEW_SPECS

    def is_admin(self) -> bool:
        """系统视图只向内置 admin 用户或 admin 角色开放。"""

        user = self.database.session.user
        return user.name.lower() == "admin" or "admin" in user.roles

    def ensure_tables(self) -> bool:
        """创建缺失的内部表，并拒绝同名的普通用户表。"""

        changed = False
        for spec in SYSTEM_TABLE_SPECS:
            table = self.database.catalog.find_table(spec.name, include_system=True)
            if table is None:
                self.database.catalog.create_table(spec.name, spec.schema, system=True)
                changed = True
                continue
            if not table.system:
                raise CatalogError(f"{spec.name} 已被普通表占用，无法初始化内部权限表")
            if len(table.schema) == 0:
                # HOW：内置表的列定义在 Catalog 页中采用紧凑格式，启动时由固定规范补回。
                table.schema = spec.schema
                changed = True
            if table.schema != spec.schema:
                raise CatalogError(f"内部表 {spec.name} 的模式不匹配")
        return changed

    def ensure_views(self) -> bool:
        """初始化内部表对应的只读视图；视图定义不分配物理数据页。"""

        changed = False
        for spec in SYSTEM_VIEW_SPECS:
            view = self.database.catalog.find_view(spec.name)
            if view is None:
                if (
                    self.database.catalog.find_table(spec.name, include_system=True)
                    is not None
                ):
                    raise CatalogError(f"{spec.name} 已被表占用，无法初始化系统视图")
                self.database.catalog.create_view(
                    spec.name, spec.schema, spec.definition_sql, system=True
                )
                changed = True
                continue
            if not view.system:
                raise CatalogError(f"{spec.name} 已被普通视图占用，无法初始化系统视图")
            if (
                view.schema != spec.schema
                or view.definition_sql.strip() != spec.definition_sql
            ):
                raise CatalogError(f"系统视图 {spec.name} 的定义不匹配")
        return changed

    def _table(self, name: str) -> TableMetadata:
        """解析或获取表元数据。"""
        return self.database.catalog.get_table(name, include_system=True)

    def _rows(self, name: str, width: int) -> tuple[tuple[object, ...], ...]:
        """读取指定系统表的当前行。"""
        rows = tuple(
            row for _row_id, row in self.database._heap(self._table(name)).scan()
        )
        for row in rows:
            if len(row) != width:
                raise CatalogError(f"内部表 {name} 的记录列数错误")
        return rows

    def load_rbac(self) -> RBAC | None:
        """从内部表恢复 RBAC；四张表全空时返回 None 供新库初始化。"""

        expected = {spec.name for spec in SYSTEM_TABLE_SPECS}
        existing = {
            table.name.lower() for table in self.database.catalog.system_tables()
        }
        if not expected.issubset(existing):
            raise CatalogError("内部权限表未初始化")

        user_rows = self._rows("_sys_users", 2)
        role_rows = self._rows("_sys_roles", 1)
        member_rows = self._rows("_sys_role_members", 6)
        privilege_rows = self._rows("_sys_privileges", 6)
        if not user_rows and not role_rows and not member_rows and not privilege_rows:
            return None

        roles: dict[str, _RoleRecord] = {}
        for row in role_rows:
            name = _text(row[0], "角色名")
            key = name.lower()
            if key in roles:
                raise CatalogError(f"内部权限表中的角色 {name!r} 重复")
            roles[key] = _RoleRecord(name, set())

        users: dict[str, _UserRecord] = {}
        for row in user_rows:
            name = _text(row[0], "用户名")
            password_hash = _text(row[1], "密码哈希")
            key = name.lower()
            if key in users:
                raise CatalogError(f"内部权限表中的用户 {name!r} 重复")
            users[key] = _UserRecord(name, password_hash, set(), set())

        for row in member_rows:
            member_type = _text(row[0], "成员类型").upper()
            member_name = _text(row[1], "成员名")
            role_name = _text(row[2], "角色名")
            role_key = role_name.lower()
            if role_key not in roles:
                raise CatalogError(f"内部权限表引用了不存在的角色 {role_name!r}")
            if member_type == "USER":
                user = users.get(member_name.lower())
                if user is None:
                    raise CatalogError(f"内部权限表引用了不存在的用户 {member_name!r}")
                user.roles.add(role_key)
            elif member_type == "ROLE":
                raise CatalogError("当前内部 RBAC 尚不支持角色嵌套")
            else:
                raise CatalogError(f"内部权限表中的成员类型 {member_type!r} 不支持")
            _bool(row[3], "admin_option")
            _bool(row[4], "inherit_option")
            _bool(row[5], "set_option")

        for row in privilege_rows:
            grantee_type = _text(row[0], "授权主体类型").upper()
            grantee_name = _text(row[1], "授权主体名")
            privilege = _text(row[2], "权限名")
            object_name = None if row[3] is None else _text(row[3], "权限对象")
            effective = _join_privilege(privilege, object_name)
            if grantee_type == "ROLE":
                target = roles.get(grantee_name.lower())
                if target is None:
                    raise CatalogError(f"内部权限表引用了不存在的角色 {grantee_name!r}")
                target.privileges.add(effective)
            elif grantee_type == "USER":
                target = users.get(grantee_name.lower())
                if target is None:
                    raise CatalogError(f"内部权限表引用了不存在的用户 {grantee_name!r}")
                target.direct_privileges.add(effective)
            else:
                raise CatalogError(
                    f"内部权限表中的授权主体类型 {grantee_type!r} 不支持"
                )
            _text(row[4], "授权者")
            _bool(row[5], "grant_option")

        return RBAC.from_dict(
            {
                "version": 1,
                "roles": [
                    {"name": role.name, "privileges": sorted(role.privileges)}
                    for role in roles.values()
                ],
                "users": [
                    {
                        "name": user.name,
                        "password_hash": user.password_hash,
                        "roles": sorted(user.roles),
                        "direct_privileges": sorted(user.direct_privileges),
                    }
                    for user in users.values()
                ],
            }
        )

    @staticmethod
    def _rbac_rows(rbac: RBAC) -> dict[str, list[tuple[object, ...]]]:
        """生成用户、角色和权限的系统表行。"""
        users = sorted(rbac.users.values(), key=lambda item: item.name.lower())
        roles = sorted(rbac.roles.values(), key=lambda item: item.name.lower())
        user_rows = [(user.name, user.password_hash) for user in users]
        role_rows = [(role.name,) for role in roles]
        member_rows: list[tuple[object, ...]] = []
        privilege_rows: list[tuple[object, ...]] = []

        for user in users:
            for role_key in sorted(user.roles):
                role = rbac.roles.get(role_key)
                member_rows.append(
                    (
                        "USER",
                        user.name,
                        role.name if role else role_key,
                        True,
                        True,
                        True,
                    )
                )
            for privilege in sorted(user.direct_privileges):
                action, object_name = _privilege_parts(privilege)
                privilege_rows.append(
                    ("USER", user.name, action, object_name, "admin", False)
                )
        for role in roles:
            for privilege in sorted(role.privileges):
                action, object_name = _privilege_parts(privilege)
                privilege_rows.append(
                    ("ROLE", role.name, action, object_name, "admin", False)
                )
        return {
            "_sys_users": user_rows,
            "_sys_roles": role_rows,
            "_sys_role_members": member_rows,
            "_sys_privileges": privilege_rows,
        }

    def persist_rbac(self, rbac: RBAC) -> None:
        """用最新 RBAC 快照重写内部表。"""

        self.ensure_tables()
        rows_by_table = self._rbac_rows(rbac)
        for spec in SYSTEM_TABLE_SPECS:
            table = self._table(spec.name)
            heap = self.database._heap(table)
            for row_id, _row in tuple(heap.scan()):
                heap.delete(row_id)
            for row in rows_by_table[spec.name]:
                heap.insert(row)
            table.page_ids = [PageId(page_id) for page_id in heap.page_ids]
            table.first_page_id = table.page_ids[0] if table.page_ids else None
            table.row_count = len(rows_by_table[spec.name])

        self.database.buffer_pool.flush_all()
        self.database._persist_catalog()
