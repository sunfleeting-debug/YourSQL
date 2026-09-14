"""YourSQL 公共入口。"""

__all__ = ["Database"]


def __getattr__(name: str) -> object:
    """按名称延迟获取公共导出，兼容可选依赖未安装的场景。"""
    if name == "Database":
        from yoursql.engine.runtime.database import Database

        return Database
    raise AttributeError(name)
