#!/usr/bin/env python
"""把各账套的数据压缩后备份到阿里云 OSS，并按天/周/月分级保留。

    # 跑一轮（systemd timer 每天调的就是它）
    python scripts/backup_to_oss.py

    # 只备某几个账套；强制按月度留档（补一份永久档）
    python scripts/backup_to_oss.py --book 1 --book 3 --tier monthly

    # 试跑：只打印会传什么、会删什么，一个字节都不写
    python scripts/backup_to_oss.py --dry-run

    # 从 OSS 下载一份归档后恢复（**退路本身**）
    python scripts/backup_to_oss.py --restore /tmp/book-1-20261001.json.gz

**为什么要有 ``--restore``，而不是只管备份。** 只 dump 不 restore 的备份不是
备份 —— 那句结论本仓库已经付过一次学费（见 ``scripts/db_backup.py`` 开头的
长注释：审计 P2-25 之后才补上恢复路径与它的往返测试）。OSS 备份的产物躺在
一台**别的机器**上，取回来的方式、能不能灌回去，都必须有人真的跑过一次，
否则出事那天会发现「有备份，但不知道怎么用」。

**退出码。** ``0`` 至少一本成功；``2`` 配置不全 / 没有任何东西可备；``4`` 一本都没
成功。systemd 只关心非零，而「全部失败」必须是非零 —— 一轮全灭还返回 0，
表现就是「定时任务每天都是绿的」，正是这套系统反复在防的那个形态。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.backup import config as cfg_mod           # noqa: E402
from app.backup import export as export_mod        # noqa: E402
from app.backup import service                     # noqa: E402
from app.core.db import SessionLocal                # noqa: E402


def _print_run(run) -> str:
    mark = "✅" if run.status == "success" else "❌"
    head = f"{mark} {run.book_label} [{run.tier}]"
    if run.status == "success":
        return (f"{head}  {run.row_count} 行 / {run.size_bytes / 1024:.1f} KiB"
                f"  → {run.oss_key}")
    return f"{head}  失败：{run.message}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="账套数据备份到阿里云 OSS")
    ap.add_argument("--book", type=int, action="append", dest="books",
                    help="只备这一个账套（可重复）；不给就是全部账套")
    ap.add_argument("--tier", choices=("daily", "weekly", "monthly"),
                    help="强制档位。不给则自动分级（见 app/backup/tier.py）")
    ap.add_argument("--no-prune", action="store_true", help="这轮不清理过期产物")
    ap.add_argument("--ignore-enabled", action="store_true",
                    help="即使总开关是关的也照跑（手动补备份用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出这轮会备什么、传什么，一个字节都不写")
    ap.add_argument("--restore", metavar="FILE",
                    help="从这份归档恢复（先验后跑，整批一个事务）")
    ap.add_argument("--restore-dry-run", action="store_true",
                    help="配合 --restore：只校验归档并报行数，不写库")
    args = ap.parse_args(argv)

    session = SessionLocal()
    try:
        if args.restore:
            return _restore(session, args)

        if not args.ignore_enabled and not cfg_mod.is_enabled(session):
            print("定时备份开关是关的（系统管理 → 数据安全）。"
                  "要强制跑一轮请加 --ignore-enabled。")
            return 0

        try:
            cfg = cfg_mod.require_ready(session)
        except cfg_mod.ConfigIncomplete as e:
            print(f"❌ {e}", file=sys.stderr)
            return 2

        if args.dry_run:
            from app.backup import tier as tier_mod
            from app.models import SCOPE_BOOK, SCOPE_SYSTEM, Book
            from sqlalchemy import select as _sel
            keep = cfg_mod.retention_days(session)
            print(f"Bucket {cfg['bucket']}  前缀 {cfg['prefix']}/")
            print(f"保留天数：日 {keep['daily']} / 周 {keep['weekly']} / 月 永久")
            books = list(session.scalars(_sel(Book).order_by(Book.id)).all())
            ids = set(args.books) if args.books else {b.id for b in books}
            for b in books:
                if b.id not in ids:
                    continue
                t = tier_mod.classify(session, SCOPE_BOOK, b.id)
                print(f"  账套 {b.id} {b.company_name}/{b.name}  → {t}")
            if cfg_mod.include_system(session):
                t = tier_mod.classify(session, SCOPE_SYSTEM, None)
                print(f"  系统数据（用户/授权/参数）  → {t}")
            print("\n试跑结束，没有上传任何东西。")
            return 0

        result = service.run_backup(
            session, book_ids=args.books, force_tier=args.tier,
            prune=not args.no_prune)
        for run in result.runs:
            print(_print_run(run))
        for d in result.deleted:
            print(f"🧹 清理 {d.tier}: {d.key}（存了 {d.age_days:.1f} 天）")
        ok = len(result.succeeded)
        print(f"\n{ok}/{len(result.runs)} 个备份对象成功，"
              f"清理 {len(result.deleted)} 份过期产物。")
        if result.prune_error:
            # **走 stderr，不改退出码。** 退出码 4 的含义是「这一轮没备份出去」，
            # 而数据确实全传上去了 —— 让清理的失败占用那个码，等于把一次成功
            # 的备份报成失败，正是这次要修的那个毛病。退路窗口在悄悄缩短这件事
            # 要有人看见，而 systemd 早就把这个进程的 stderr 收进日志文件了。
            print(f"⚠️  清理过期产物失败（不影响已上传的备份）：{result.prune_error}",
                  file=sys.stderr)
        return 0 if ok else 4
    finally:
        session.close()


def _restore(session, args) -> int:
    path = Path(args.restore)
    if not path.is_file():
        print(f"❌ 归档不存在：{path}", file=sys.stderr)
        return 2
    try:
        counts = export_mod.restore(session, path, dry_run=args.restore_dry_run)
    except export_mod.ExportRefused as e:
        print(f"❌ 拒绝恢复：{e}", file=sys.stderr)
        return 3
    if args.restore_dry_run:
        print("校验通过（未写库）。各表行数：")
        for name, n in counts.items():
            print(f"  {name:20} {n}")
        return 0
    session.commit()
    print("恢复完成：")
    for name, n in counts.items():
        print(f"  {name:20} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
