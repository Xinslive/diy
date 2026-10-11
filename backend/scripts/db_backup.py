#!/usr/bin/env python
"""迁移前把数据库导出成 SQL。

    python scripts/db_backup.py > backup.sql

**为什么密码要绕开命令行。** 直接 ``pg_dump postgresql://user:pw@host/db``
会把口令写进进程参数，而 ``ps`` 对同机器上的任何用户可见 —— 于是一次例行备份
就把生产口令泄露给了所有能登进这台机器的人。这里改成把口令放进子进程的
``PGPASSWORD`` 环境变量，argv 里只有主机、端口、库名和用户名。

**为什么迁移前必须备份。** 引入 Alembic 之后，表结构的变更第一次有了
「破坏性」这个属性（改列名、拆表、加 NOT NULL 到已有表）。P3-01 修掉的
正是「应用启动时静默不生效」这类问题，但 revision 本身也可能写错。
没有备份，唯一的退路是「这个库只能从昨晚的备份恢复」。

stdin/stdout 走管道，调用方自己决定存哪儿（deploy.sh 存进 .releases）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402


def main() -> int:
    # pg_dump 不认 SQLAlchemy 的 ``postgresql+psycopg://`` 驱动段，先剥掉
    u = urlparse(settings.database_url.replace("postgresql+psycopg://", "postgresql://"))
    if not u.hostname:
        print("DATABASE_URL 解析不出主机名，备份中止", file=sys.stderr)
        return 2

    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)

    cmd = [
        "pg_dump", "--no-owner", "--no-acl", "--format=plain",
        "-h", u.hostname,
        "-p", str(u.port or 5432),
        "-U", u.username or "",
        "-d", (u.path or "/").lstrip("/"),
    ]
    try:
        return subprocess.call(cmd, env=env)
    except FileNotFoundError:
        print("没装 pg_dump，无法备份", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
