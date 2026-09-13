"""YourSQL 公共入口。"""

__all__ = ["Database"]


def __getattr__(name: str) -> object:
    if name == "Database":
        from .engine.database import Database

        return Database
    raise AttributeError(name)
