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
import re
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


def _通知备份失败(message: str) -> None:
    """备份失败时发一封告警邮件。**发不出去也不影响脚本的退出码。**

    **为什么脚本要自己发**：这条备份是 **systemd timer**
    （``cwq-backup.timer``，``deploy.sh`` 默认会装上并启用）直接调的本脚本，
    **不经过应用内的调度器**。只在调度器里挂告警，这条路径失败时就是
    一声不吭。

    同步发送：进程马上就要退出，异步线程会随进程一起死，信就永远送不到。

    **只报备份失败，不报恢复失败**：恢复被拒（RestoreRefused）说明校验
    起作用了，那是**好事**，不该发告警。

    **通知模块是懒加载的**：这个脚本是**恢复路径**，而恢复恰恰发生在
    应用已经坏了的时候（配置写错、依赖装不上、库连不上）。模块级
    import 通知子系统会把它一起拖下水 —— 于是「最后一道退路」在最需要
    它的时刻起不来。import 放进函数里，失败只少一封信，脚本照常工作。
    """
    try:
        from app.notify import events as notify_events

        notify_events.local_backup_failed_blocking(
            "本机数据库备份（每日 pg_dump）", message)
    except Exception as exc:  # noqa: BLE001
        # 告警本身绝不能把备份脚本搞崩：退出码是 systemd 判断成败的依据。
        print(f"⚠️  告警邮件发送失败（已忽略）：{exc}", file=sys.stderr)


def dump(url: str) -> int:
    """导出到 stdout。返回进程退出码。"""
    u = _parse(url)
    rc = _run(dump_command(url), _env(u))
    if rc != 0:
        _通知备份失败(f"pg_dump 退出码 {rc}（导出到 stdout）")
    return rc


# ---------------------------------------------------------------------------
# 服务端不认识 dump 里的 SET 行时的自愈
# ---------------------------------------------------------------------------
#
# **问题。** pg_dump 会把「导出时用的会话参数」写进 dump 开头，其中有些是
# **版本特有**的：``transaction_timeout`` 是 PostgreSQL 17 才有的参数，
# pg_dump 17+ 会在 dump 里写一行 ``SET transaction_timeout = 0;``。
# 而 PG 16 的服务端不认识它 —— 恢复时直接报
# ``ERROR: unrecognized configuration parameter "transaction_timeout"``。
#
# **为什么这是「恢复彻底失败」而不是「一行报错」。**
# 恢复命令带 ``--single-transaction`` 与 ``ON_ERROR_STOP=1``（见
# :func:`restore_command`）：前者让整份 dump 要么全进要么全不进，后者让
# 首个错误立刻非零退出。两者叠加的结果是 —— **那一条 SET 让整个事务回滚，
# 一张表都不恢复**，而退出码只有 3。
#
# 实测（postgres:16 服务端 + pg_dump 18.6 客户端）：
#   ERROR:  unrecognized configuration parameter "transaction_timeout"
#   退出码 3；之后查 \dt → Did not find any relations.
#
# 这个仓库把恢复称作「系统唯一的退路」（见本模块开头），所以「恢复不了」
# 不是能接受的状态 —— 而触发它的条件（客户端比服务端新）非常容易发生：
# 发行版升级 apt 源里的 postgresql-client 就会换掉客户端版本。
#
# **修法。** 恢复前先问服务端它认识哪些配置参数，把 dump 里服务端**不认识**
# 的 SET 行剔掉再恢复。
#
# 为什么可以剔：这些 SET 只是 pg_dump 记下的会话参数（超时、编码之类），
# 不是数据，也不是结构。剔掉它们不改变恢复出来的库的内容。而被剔的那几个
# 参数，恰恰是「新版客户端为了保护旧服务端而加的保险」（比如把事务超时设成
# 0 = 不超时）—— 旧服务端本来就没有这个概念，缺了它反而回到该版本的本意。
#
# 问不到服务端（客户端缺失、连不上）时**不剔任何东西**：那时候本来也
# 恢复不了，不该在这里做猜测。

_SET_LINE = re.compile(rb"^SET ([A-Za-z_][A-Za-z0-9_]*) = ")


def server_setting_names(url: str) -> set[str] | None:
    """问服务端它认识哪些配置参数。

    返回参数名集合；问不到（客户端缺失、连不上、权限不足）时返回 ``None``
    —— 那个 ``None`` 的含义是「不知道」，不是「一个都没有」，调用方据此
    **不做任何剔除**。
    """
    u = _parse(url)
    cmd = [PSQL_BIN, "--no-psqlrc", "--tuples-only", "--no-align",
           "--dbname", (u.path or "/").lstrip("/"),
           "-h", u.hostname, "-p", str(u.port or 5432),
           "-U", u.username or "",
           "--command", "SELECT name FROM pg_settings;"]
    try:
        result = subprocess.run(cmd, env=_env(u), capture_output=True, timeout=60)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    names = set()
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line:
            names.add(line)
    return names or None


def strip_unsupported_settings(sql: bytes, known: set[str]) -> tuple[bytes, list[str]]:
    """把 dump 里服务端不认识的 ``SET xxx = ...`` 行剔掉。

    :param sql: 原始 dump。
    :param known: 服务端认识的参数名（:func:`server_setting_names` 的结果）。
    :return: ``(处理后的 dump, 被剔掉的参数名列表)``。

    只处理 ``SET <名字> = <值>;`` 这种简单形式。像
    ``SET SESSION CHARACTERISTICS AS TRANSACTION ...`` 这种复合形式
    **不动** —— 它不是单个参数，误判的风险大于收益，而它也不带版本特有的
    新参数名。
    """
    removed: list[str] = []
    kept: list[bytes] = []
    for line in sql.splitlines(keepends=True):
        match = _SET_LINE.match(line)
        if match:
            name = match.group(1).decode("ascii", "replace")
            if name not in known:
                removed.append(name)
                continue  # 整行丢掉：留着半行会让 psql 语法出错
        kept.append(line)
    return b"".join(kept), removed


def restore(url: str, sql: bytes) -> int:
    u = _parse(url)
    assert_restorable(sql)

    # 服务端不认识 dump 里的某个 SET 时，那一条会让 --single-transaction
    # 整份回滚 —— 一张表都不恢复。剔掉它再恢复。
    known = server_setting_names(url)
    if known is not None:
        cleaned, removed = strip_unsupported_settings(sql, known)
        if removed:
            print(
                f"恢复前剔除了服务端不认识的会话参数：{', '.join(sorted(set(removed)))}。"
                "这些是 pg_dump 记下的版本特有参数（新版客户端写进 dump 的保险），"
                "留着会让整份 dump 回滚、一张表都不恢复。",
                file=sys.stderr,
            )
            sql = cleaned

    return _run(restore_command(url), _env(u), stdin_bytes=sql)


def dump_to_file(out: Path, keep: int | None = None) -> int:
    """导出到文件，**并且**保证落盘的不是空文件。

    为什么不能 `> file` 一把梭：shell 重定向会**先把文件建出来**，
    pg_dump 随后才跑。pg_dump 失败（客户端没装、口令错、磁盘满）时，
    那个文件**已经躺在那里了** —— 0 字节或半截，且文件名看着完全正常。
    没有保留策略时它会一路留到被轮转掉，而某一天有人拿它恢复，
    结果恢复「成功」了但库里是空的。
    （这与 --restore 那侧的 RestoreRefused 是同一个担心，两头都得防。）

    所以这里先写临时文件，**确认非空后再改名**。改名是原子的，
    于是目录里出现的永远要么是上一份完整备份，要么就是这一份完整备份。

    保留策略：只保留最近 ``keep`` 份，按修改时间倒序。
    没有保留策略的定时备份会慢慢把磁盘写满 —— 而磁盘写满之后，
    下一个失败的就是**备份本身**，一个把自己搞挂的备份策略。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")

    try:
        with open(tmp, "wb") as fh:
            # 备份里是全量账务数据（凭证、科目、金额）。靠 umask 兜底是不够的
            # —— 默认 022 会把它落成 644，而这台机器上任何本地账号都读得到。
            # 显式收成 600，不猜调用方的 umask。
            os.fchmod(fh.fileno(), 0o600)
            rc = dump_to(fh)
    except BaseException:
        # **抛异常也要清。** dump_to 不只可能「返回非零」：
        # 磁盘满时 fh.write 会抛 OSError，Popen 也可能抛别的错。
        # 只在 rc != 0 那条路上清理的话，异常一抛，.part 就留在目录里了 ——
        # 而它长得像备份，保留策略还只管 *.sql，于是它能一直躺着。
        tmp.unlink(missing_ok=True)
        raise
    if rc != 0:
        tmp.unlink(missing_ok=True)      # 别把半截文件留在目录里冒充备份
        print(f"❌ 备份失败（退出码 {rc}），未生成 {out.name}", file=sys.stderr)
        _通知备份失败(f"pg_dump 退出码 {rc}，未生成 {out.name}")
        return rc
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        print(f"❌ 备份产出是 0 字节，已丢弃：{out}", file=sys.stderr)
        _通知备份失败(f"备份产出是 0 字节，已丢弃：{out}")
        return 4
    tmp.replace(out)                      # 原子改名：目录里不会出现半截备份

    if keep:
        prune(out.parent, keep)
    return 0


def dump_to(fh) -> int:
    """把 dump 写进已打开的二进制流。"""
    u = _parse(settings.database_url)
    cmd = dump_command(settings.database_url)
    try:
        p = subprocess.Popen(cmd, env=_env(u), stdout=fh)
    except FileNotFoundError:
        print(f"找不到 {cmd[0]}。装 PostgreSQL 客户端，或用 "
              f"{'CWQ_PG_DUMP' if cmd[0] == PG_DUMP_BIN else 'CWQ_PSQL'} 指向它的位置。",
              file=sys.stderr)
        return 127
    return p.wait()


def prune(directory: Path, keep: int) -> None:
    """只留最近 keep 份 .sql（按 mtime 倒序）。"""
    backups = sorted(directory.glob("*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        old.unlink(missing_ok=True)
        print(f"  已清理旧备份 {old.name}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="数据库备份/恢复")
    ap.add_argument("--restore", action="store_true",
                    help="从 stdin（或 --file）读 dump 并恢复，而不是导出")
    ap.add_argument("--file", help="恢复用的 dump 文件；不给则读 stdin")
    ap.add_argument("--url", default=settings.database_url,
                    help="目标连接串，默认取配置里的 DATABASE_URL（恢复演练时指向测试库）")
    ap.add_argument("--out", help="导出到这个文件（不给则写 stdout，与原行为一致）。"
                                   "定时备份用它，省掉 shell 重定向的半截文件问题")
    ap.add_argument("--keep", type=int, metavar="N",
                    help="配合 --out：只保留最近 N 份备份，删掉更旧的")
    args = ap.parse_args(argv)

    try:
        if not args.restore:
            if args.out:
                return dump_to_file(Path(args.out), args.keep)
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