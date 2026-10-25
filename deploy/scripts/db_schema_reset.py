#!/usr/bin/env python3
"""清空 public schema —— 「回退库」流程的第一步。

为什么需要这一步
----------------
本仓库的 dump 是 ``pg_dump --format=plain`` 且**不带 --clean**
（backend/scripts/db_backup.py 的 dump_command），dump 里全是 ``CREATE TABLE``。
往一个**已经有数据**的库里灌会撞 ``relation "xxx" already exists``；
而恢复命令带 ``--single-transaction`` 与 ``ON_ERROR_STOP=1``
→ 首个错误就把**整份**恢复回滚，**一张表都灌不进去**，退出码非零。

仓库自己的测试注释写的就是这件事
（backend/tests/test_db_backup_restore.py：「真实恢复就是往空库里灌。
往一个已经有数据的库里灌会撞 CREATE TABLE already exists」）。

所以「回退库」必须是**两步**：先清空 schema，再灌 dump。

**这一步是不可逆的**：备份之后的全部写入都会丢失。
所以发布脚本**不会自动执行它** —— 脚本只把完整的流程打印出来，
由人在失败现场决定要不要做。

用法
----
    cd <应用根>/backend
    .venv/bin/python ../deploy/scripts/db_schema_reset.py

为什么用应用自己的配置拿连接串
------------------------------
连接串在 .env 里（``DATABASE_URL``），而 pydantic 按 **cwd** 找 .env
（``SettingsConfigDict(env_file=".env")``，相对路径）。
本脚本必须**在 backend 目录下**运行 —— 上面那行 ``cd`` 就是为了这个。

而口令**不靠 argv**：直接写进连接串的话，``ps`` 对同机器上任何用户都看得见
（backend/scripts/db_backup.py 开头的说明说的就是它）。走 SQLAlchemy 的连接
参数，口令留在进程自己的连接里，不进命令行，也不打印。

为什么「先删后建」
------------------
pg_dump 的 plain 输出**不会**建 public schema（它写的是
「*not* creating schema, since initdb creates it」）。
所以删掉之后必须自己建回来，否则第 2 步灌 dump 时表无处安放。
新建的 schema 归当前连接角色所有 —— 原来 schema 上的授权会丢，
灌完之后要重新确认账号权限。
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    # 本脚本在 <应用根>/deploy/scripts/ 下，而 app 包在 <应用根>/backend/ 下。
    # cwd 必须是 backend（.env 是相对 cwd 找的），所以把 cwd 加进 sys.path。
    here = os.getcwd()
    if here not in sys.path:
        sys.path.insert(0, here)

    try:
        from sqlalchemy import create_engine, text

        from app.core.config import settings
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 导入失败：{exc}", file=sys.stderr)
        print("   这个脚本必须在 <应用根>/backend 目录下运行"
              "（.env 是相对 cwd 找的）。", file=sys.stderr)
        return 2

    url = settings.database_url
    if not url:
        print("❌ 读不到 DATABASE_URL —— 拒绝清空 schema。", file=sys.stderr)
        return 2

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 清空 public schema 失败：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print("✅ public schema 已清空并重建 —— 现在可以把 dump 灌回去了。")
    print("   ⚠️ 这一步已经**不可逆**：备份之后的全部写入都没了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
