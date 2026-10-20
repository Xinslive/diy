#!/usr/bin/env python3
"""管理员二次验证的运维入口。

审查报告 P0-1 的整改配套。**这一份脚本存在的唯一理由是「防锁死」**：
二次验证一旦在生产上生效，邮箱通道就成了管理员登进系统的必经之路，
而通道会坏（SMTP 挂了、域名被 SPF 拒了、邮件进了垃圾箱）。

没有应急出口时，那种故障的表现是**所有管理员一起登不进去**，
而唯一的解法是「登进服务器改配置」—— 可「登进服务器」这一步本身
也要口令。所以这里给三条不依赖 Web 服务的路：

    # 看当前状态与最近的挑战
    backend/.venv/bin/python backend/scripts/mfa_admin.py status

    # 临时关掉二次验证（通道坏了时的救急）
    backend/.venv/bin/python backend/scripts/mfa_admin.py --disable

    # 重新打开
    backend/.venv/bin/python backend/scripts/mfa_admin.py --enable

    # 生成一次性应急码（打印到终端）
    backend/.venv/bin/python backend/scripts/mfa_admin.py --emergency-code

**应急码怎么用。** 本脚本生成的那串**只打印到终端**，不落库、不落日志、
不写进任何响应。它要填进**服务进程的环境变量**：

    ADMIN_MFA_EMERGENCY_CODE=<生成出来的那串>

然后重启后端。管理员在登录页把应急码填进二次验证的输入框即可完成一次
验证 —— **用一次即废**（见 ``app/notify/mfa.py::consume_emergency_code``）。
用完之后记得把环境变量撤掉。

**为什么不在库里存应急码。** 库里存的东西能被备份带走、能被读审计日志的
人看到；而应急码一旦泄露就等于「一次口令就能登进系统」。它只该活在
运维的终端与进程环境里。

**读配置的方式沿用既有脚本**（``sys.path`` + ``app.core.config.settings`` /
``app.core.db.SessionLocal``）。只多了一件事：**切到 backend/ 再导入** ——
``app.core.config`` 的 ``env_file`` 是相对路径（``.env``），从仓库根目录
跑脚本时它找不到 ``backend/.env``，于是连的是**另一个库**。「连错库」在
运维脚本里是最糟的一类事故，所以这里显式对齐工作目录。
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
# 见上面那段注释：env_file 是相对路径，不对齐工作目录就会连错库。
os.chdir(_ROOT)

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import AdminMfaChallenge, SysUser  # noqa: E402
from app.notify import mfa as mfa_service  # noqa: E402


def _fmt_time(value) -> str:
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def cmd_status(args) -> int:
    """看开关状态、通道就绪与否，以及最近的挑战。"""
    with SessionLocal() as db:
        enabled = mfa_service.mfa_enabled(db)
        ready = mfa_service.smtp_ready(db)
        error = mfa_service.channel_error(db)
        recipients = []
        try:
            from app.notify.config import recipients as _recipients
            recipients = _recipients(db)
        except Exception:  # noqa: BLE001
            recipients = []

        print("== 管理员二次验证 ==")
        print(f"开关：{'开' if enabled else '关'}"
              f"（sys_setting.{mfa_service.MFA_ENABLED_KEY} 优先，env 兜底）")
        print(f"通道：{'就绪' if ready else '**不可用**'}")
        print(f"收件人：{len(recipients)} 个"
              + (f"（{mfa_service.mask_email(recipients[0])} 等）" if recipients else ""))
        if error:
            print(f"⚠ {error}")

        rows = db.scalars(
            select(AdminMfaChallenge)
            .order_by(AdminMfaChallenge.id.desc())
            .limit(args.limit)
        ).all()
        print(f"\n最近 {len(rows)} 条挑战：")
        if not rows:
            print("  （还没有过任何挑战）")
            return 0
        for row in rows:
            user = db.get(SysUser, row.user_id)
            name = user.username if user is not None else f"#{row.user_id}"
            print(f"  #{row.id:<6} {name:<24} {row.status:<9} {row.reason:<7}"
                  f" 发出 { _fmt_time(row.last_sent_at) }"
                  f"  过期 { _fmt_time(row.expires_at) }")
    return 0


def cmd_enable(args) -> int:
    """打开二次验证。"""
    with SessionLocal() as db:
        mfa_service.set_mfa_enabled(db, True, updated_by="mfa_admin.py")
        db.commit()
    print("已打开管理员二次验证。")
    if not mfa_service.smtp_ready(db):
        print("⚠ 但 SMTP 通道还没配好 —— 现在管理员登录会拿到一条明确的"
              "「通道未配置」错误，而不是被放行。请先把收件人与 SMTP 配好。")
    return 0


def cmd_disable(args) -> int:
    """关掉二次验证。**这是防锁死的救急开关。**"""
    with SessionLocal() as db:
        mfa_service.set_mfa_enabled(db, False, updated_by="mfa_admin.py")
        db.commit()
    print("已关闭管理员二次验证。管理员登录恢复为「只要口令」。")
    print("（问题处理完之后请记得 --enable 开回去。）")
    return 0


def cmd_emergency_code(args) -> int:
    """生成一次性应急码。**只打印到终端**，不落库、不落日志。"""
    code = secrets.token_urlsafe(24)
    print("一次性应急码（请立刻抄下来；本脚本不落库、不写日志）：")
    print()
    print(f"    {code}")
    print()
    print("用法：")
    print(f"  1. 把它填进后端进程的环境变量：ADMIN_MFA_EMERGENCY_CODE={code}")
    print("  2. 重启后端")
    print("  3. 管理员在登录页的二次验证输入框里填这串码，完成一次验证")
    print("  4. 用完把环境变量撤掉并重启（这串码**用一次即废**）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="管理员二次验证的运维入口（状态 / 开关 / 应急码）")
    parser.add_argument("command", nargs="?", default=None,
                        choices=["status", "enable", "disable", "emergency-code"],
                        help="要执行的命令（默认 status；也可以用下面的开关写法）")
    parser.add_argument("--enable", action="store_true",
                        help="打开二次验证")
    parser.add_argument("--disable", action="store_true",
                        help="关闭二次验证（防锁死的救急开关）")
    parser.add_argument("--emergency-code", action="store_true",
                        help="生成一次性应急码并打印到终端")
    parser.add_argument("--limit", type=int, default=10,
                        help="status 时显示最近多少条挑战（默认 10）")
    args = parser.parse_args()

    if args.enable and args.disable:
        parser.error("--enable 与 --disable 不能同时用")

    command = args.command
    if args.enable:
        command = "enable"
    elif args.disable:
        command = "disable"
    elif args.emergency_code:
        command = "emergency-code"
    command = command or "status"

    if command == "enable":
        return cmd_enable(args)
    if command == "disable":
        return cmd_disable(args)
    if command == "emergency-code":
        return cmd_emergency_code(args)
    return cmd_status(args)


if __name__ == '__main__':
    raise SystemExit(main())
