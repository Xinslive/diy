#!/usr/bin/env python
"""启动前的 schema / 数据库预检：**在进程起来之前**回答「能不能起」。

**为什么要有这个脚本（它是 2026-10-17 那次「端口在监听但完全不响应」的解药）。**

那天后端「又」没了响应。查下来的链路是：

  1. 开发环境用 ``uvicorn --reload`` 起的。uvicorn 的 **reloader 父进程**自己
     bind 住 :8000 的 LISTEN 套接字，再把它 pickle 给 worker 子进程。
  2. worker 子进程的 ``startup()`` 里调 :func:`assert_schema_ready`。数据库停在
     旧 revision（迁移 head 前进过、``alembic upgrade`` 没跟着跑），于是子进程
     抛 ``SchemaVersionError`` 然后退出。
  3. **父进程还活着，还握着那个 LISTEN 套接字。** 它此刻阻塞在
     ``os_waitpid`` 等那个已经死掉的子进程，所以永远不会有人调 ``accept()``。

结果就是 TCP 三次握手成功（客户端看到 ESTABLISHED）、事件循环健康空闲、
但**一个请求都得不到应答** —— 从外面看跟「服务器彻底卡死」一模一样。
更糟的是 ``SIGTERM`` 也杀不掉它：reloader 的 signal handler 只
``self.should_exit.set()``，而那个 Event 只在 ``pause()`` / ``should_restart()``
里被观察；父进程正卡在 waitpid，事件永远轮不到被看一眼。实测连发两次
SIGTERM 进程纹丝不动，``kill -9`` 之后端口才释放。

**所以闸门必须长在「进程起来之前」。** 这不是洁癖：闸门长在 uvicorn 的
startup 里，意味着每次闸门拦人，生产出来的都是「一个端口在监听、没人应答、
杀不掉」的状态，而不是一条看得懂的报错。

用法（``dev.sh`` 与 systemd 的 ``ExecStartPre=`` 调的是同一个入口）::

    .venv/bin/python scripts/preflight.py

**退出码**（systemd 只关心非零，但分开是为了让人一眼看出是哪一类）::

    0  schema 与数据库都没问题，可以起
    3  schema 版本对不上 —— 要跑 alembic upgrade head（或 stamp，见报错文案）
    4  数据库连不上 —— 查数据库进程/网络/连接串，不是代码的问题
    1  其它（预检本身坏了，比如 migrations 目录没跟着代码走）

**它不执行任何 DDL。** 理由与 ``schema_version.py`` 的模块注释一致：建表改表
只由 Alembic 负责，启动路径只做版本检查。预检自己偷偷 upgrade，等于把
「迁移要评审」这条纪律又漏掉一次。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 退出码。写死在这里而不是散在 return 语句旁边：systemd 与 dev.sh 都要读它们。
EXIT_OK = 0
EXIT_SCHEMA = 3
EXIT_DB = 4
EXIT_OTHER = 1


def _safe_url(raw: str) -> str:
    """连接串脱敏后打印。

    报错里带连接串是排障最有用的一行，但连接串里带口令。``hide_password=True``
    只挡口令，host/port/库名还得留着 —— 「连不上」和「连错库」是两回事。
    """
    try:
        from sqlalchemy.engine import make_url

        return make_url(raw).render_as_string(hide_password=True)
    except Exception:
        return "（连接串无法解析）"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = "--quiet" in argv

    from app.core.config import settings
    from app.core.schema_version import SchemaVersionError, assert_schema_ready

    try:
        revision = assert_schema_ready()
    except SchemaVersionError as exc:
        print("✗ schema 版本对不上，服务**不能起**（这一步在起进程之前拦的）：",
              file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return EXIT_SCHEMA
    except Exception as exc:
        # 走到这里说明连「版本对不对」这一步都没做完 —— 绝大多数是数据库连不上。
        # 必须和 SchemaVersionError 分开报：前者的动作是「跑 alembic」，
        # 后者的动作是「先看数据库活着没」，混成一句话就会把人引到错误的路上。
        name = type(exc).__name__
        if name.startswith(("OperationalError", "InterfaceError", "DBAPIError")) or \
                "OperationalError" in name:
            print("✗ 连不上数据库，服务**不能起**：", file=sys.stderr)
            print(f"  {name}: {exc}", file=sys.stderr)
            print(f"  连接串（已隐去口令）：{_safe_url(settings.database_url)}",
                  file=sys.stderr)
            print("  先确认：数据库容器/进程活着吗？端口通吗？.env 里的连接串对吗？",
                  file=sys.stderr)
            return EXIT_DB
        print(f"✗ schema 预检本身失败了（不是版本对不上）：{name}: {exc}",
              file=sys.stderr)
        print("  这通常意味着部署不完整 —— migrations/ 目录没跟着代码走。",
              file=sys.stderr)
        return EXIT_OTHER

    if not quiet:
        print(f"  ✓ schema 就绪（数据库 revision {revision}）")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
