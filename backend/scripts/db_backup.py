#!/usr/bin/env python
"""备份与恢复。备份只是「存了一份东西」，恢复才是真正的退路。

    # 备份（原来就有的用法，输出到 stdout，调用方自己决定存哪儿）
    python scripts/db_backup.py > backup.sql

    # 恢复（这次补上的那半条退路）
    python scripts/db_backup.py --restore --file backup.sql
    cat backup.sql | python scripts/db_backup.py --restore

**为什么以前只有 dump 没有 restore。** 审计 P2-25 的原话：备份只 dump，
恢复路径不存在且无测试，备份失败仍然执行迁移。对一个「凭证号可回收、
审计快照是唯一追溯手段」的系统，退路失败不阻断等于没有退路 ——
而且退路本身还得靠人临场手敲一条 psql 命令，那条命令写错了没有任何东西会拦。

**恢复为什么先验后跑。** 恢复要执行的是 dump 文件里的内容。最坏的情况不是
恢复失败，而是**恢复「成功」了**：备份那步其实失败了、只留下一个 0 字节的
空文件，而恢复照单全收 —— 数据库被清空，运维还以为自己刚刚救回了数据。
所以 --restore 先检查输入像个真的 pg_dump 输出，检查不过就拒绝执行。

**恢复为什么带 --single-transaction。** dump 文件执行到一半失败（磁盘满、
连接断），不开事务的话库里会留下「恢复了一半」的状态：那比没恢复更难收拾 ——
既不是迁移前的版本，也不是完整的备份版本。有事务就是全有或全无。

**为什么口令要绕开命令行。** 直接 ``pg_dump postgresql://user:pw@host/db``
会把口令写进进程参数，而 ``ps`` 对同机器上的任何用户可见 —— 于是一次例行备份
就把生产口令泄露给了所有能登进这台机器的人。这里改成把口令放进子进程的
``PGPASSWORD`` 环境变量，argv 里只有主机、端口、库名和用户名。

**为什么依赖 pg_dump/psql 而不是纯 Python 导出。** 一致性：只有 pg_dump 知道
怎么在跨版本、跨扩展的场景下正确导出这个库。自己拼 SQL 迟早漏掉某种类型，
而漏掉的地方恰恰是恢复时才会发现的那种。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402

# pg_dump 不认 SQLAlchemy 的 ``postgresql+psycopg://`` 驱动段，先剥掉
_DRV_PREFIX = "postgresql+psycopg://"

# 客户端二进制的位置。默认在 PATH 里；容器 / 定制安装可能不在，
# 用这两个环境变量指路 —— 找不到就报错，不静默退化成「不备份」。
PG_DUMP_BIN = os.environ.get("CWQ_PG_DUMP", "pg_dump")
PSQL_BIN = os.environ.get("CWQ_PSQL", "psql")

# 认得出是 pg_dump 输出的标志。pg_dump 的 plain 格式开头必然是
# ``--`` 注释块 + 一段 SET（client_encoding / standard_conforming_strings）。
# 只查一个特征不保险，取多个，降低误判。
_DUMP_MARKERS = (
    b"PostgreSQL database dump",
    b"standard_conforming_strings",
    b"SELECT pg_catalog.set_config",
    b"SET statement_timeout",
)


class RestoreRefused(Exception):
    """拒绝执行恢复。消息必须自带「为什么不恢复」和「该怎么办」。"""


def _parse(url: str):
    """剥掉驱动段并解析连接串。解析不出主机名就是配置错误，直接拒绝。"""
    u = urlparse(url.replace(_DRV_PREFIX, "postgresql://"))
    if not u.hostname:
        raise ValueError(f"连接串解析不出主机名：{url!r}")
    return u


def _db_args(u) -> list[str]:
    return ["-h", u.hostname, "-p", str(u.port or 5432),
            "-U", u.username or "", "-d", (u.path or "/").lstrip("/")]


def _env(u) -> dict:
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    return env


def dump_command(url: str) -> list[str]:
    u = _parse(url)
    return [PG_DUMP_BIN, "--no-owner", "--no-acl", "--format=plain", *_db_args(u)]


def restore_command(url: str) -> list[str]:
    """恢复用的 psql 命令。

    ``--single-transaction``：整份 dump 要么全进要么全不进（见模块说明）。
    ``ON_ERROR_STOP``：dump 里任何一条语句失败就立刻非零退出，而不是继续跑
    剩下的部分 —— 后者在有事务时也会整体回滚，但没有它的话首个错误之后的行为
    依赖 psql 的默认宽容度，不该把这种事留给默认值。
    """
    u = _parse(url)
    return [PSQL_BIN, "--single-transaction", "--set", "ON_ERROR_STOP=1",
            "--dbname", (u.path or "/").lstrip("/"),
            "-h", u.hostname, "-p", str(u.port or 5432),
            "-U", u.username or ""]


def assert_restorable(sql: bytes) -> None:
    """检查一份 dump 值不值得恢复。不值得就抛 :class:`RestoreRefused`。

    这道闸挡的是**最坏的那种情况**：备份失败留下一个空文件（或被截断的半份），
    而恢复把它当成真数据执行下去。数据库被清空，而日志里写着「恢复完成」。
    """
    if not sql.strip():
        raise RestoreRefused(
            "恢复输入是空的。这多半是**备份那一步就失败了**，只留下一个 0 字节文件 —— "
            "此时执行恢复不会救回任何东西，只会把当前库清空。请先确认备份文件非空。"
        )
    if not any(m in sql for m in _DUMP_MARKERS):
        head = sql[:200].decode("utf-8", "replace").strip().splitlines()[:3]
        raise RestoreRefused(
            "恢复输入不像 pg_dump 的输出（找不到 pg_dump 的特征行）。"
            "拒绝执行 —— 一份坏文件跑进库里，比不恢复糟得多。\n"
            f"  开头几行：{head}"
        )


def _run(cmd: list[str], env: dict, stdin_bytes: bytes | None = None) -> int:
    try:
        if stdin_bytes is None:
            return subprocess.call(cmd, env=env)
        # 喂 stdin 必须走 run()：call() 没有 input= 参数
        # （它只接受 stdin=<文件对象>，传 input 会直接 TypeError）
        return subprocess.run(cmd, env=env, input=stdin_bytes).returncode
    except FileNotFoundError:
        name = cmd[0]
        print(f"找不到 {name}。装 PostgreSQL 客户端，或用 "
              f"{'CWQ_PG_DUMP' if name == PG_DUMP_BIN else 'CWQ_PSQL'} 指向它的位置。",
              file=sys.stderr)
        return 127


def dump(url: str) -> int:
    """导出到 stdout。返回进程退出码。"""
    u = _parse(url)
    return _run(dump_command(url), _env(u))


def restore(url: str, sql: bytes) -> int:
    u = _parse(url)
    assert_restorable(sql)
    return _run(restore_command(url), _env(u), stdin_bytes=sql)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="数据库备份/恢复")
    ap.add_argument("--restore", action="store_true",
                    help="从 stdin（或 --file）读 dump 并恢复，而不是导出")
    ap.add_argument("--file", help="恢复用的 dump 文件；不给则读 stdin")
    ap.add_argument("--url", default=settings.database_url,
                    help="目标连接串，默认取配置里的 DATABASE_URL（恢复演练时指向测试库）")
    args = ap.parse_args(argv)

    try:
        if not args.restore:
            return dump(args.url)

        if args.file:
            p = Path(args.file)
            if not p.is_file():
                print(f"恢复中止：{p} 不存在", file=sys.stderr)
                return 2
            sql = p.read_bytes()
        else:
            sql = sys.stdin.buffer.read()
    except ValueError as e:
        print(f"{e}，操作中止", file=sys.stderr)
        return 2

    try:
        return restore(args.url, sql)
    except RestoreRefused as e:
        print(f"❌ 拒绝恢复：{e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())