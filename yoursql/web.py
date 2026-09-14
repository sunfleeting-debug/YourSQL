"""启动带认证的本地 YourSQL 工作台与兼容 REST 服务。"""

import argparse
from dataclasses import replace

from .common.config import RuntimeConfig, load_dotenv
from .engine.runtime.database import Database
from .engine.services.http import DatabaseHTTPServer


def main() -> None:
    # HOW：本地 .env 只提供默认值；宿主环境变量由 load_dotenv 保留，命令行参数最后覆盖二者。
    """解析命令行参数并启动对应的工作模式。"""
    load_dotenv()
    settings = RuntimeConfig.from_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=settings.database_path)
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        help="覆盖 .env 中的开发前端 Origin，可重复",
    )
    parser.add_argument(
        "--legacy-anonymous",
        action=argparse.BooleanOptionalAction,
        default=settings.legacy_anonymous,
        help="兼容旧教学客户端：允许匿名 /sql 使用默认会话",
    )
    args = parser.parse_args()
    origins = (
        settings.allowed_origins
        if args.allow_origin is None
        else tuple(args.allow_origin)
    )
    database_config = settings.database_config
    detected_page_size = Database.detect_page_size(args.database)
    if detected_page_size is not None:
        # WHY：环境变量控制新库默认页大小，但不能让显式配置破坏已有数据库格式。
        database_config = replace(database_config, page_size=detected_page_size)
    with Database(args.database, config=database_config) as database:
        server = DatabaseHTTPServer(
            (args.host, args.port),
            database,
            legacy_anonymous=args.legacy_anonymous,
            allowed_origins=origins,
            settings=settings,
        )
        print(
            f"YourSQL 工作台 http://{args.host}:{server.server_port} · {database.path.name}",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
