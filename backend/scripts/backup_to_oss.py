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
from app.notify import events as notify_events      # noqa: E402


def _通知备份失败(message: str) -> None:
    """备份失败时发一封告警邮件。**发不出去也不影响脚本的退出码。**

    **为什么脚本要自己发**：这条路径由 systemd timer 直接调（仓库里那个
    ``cwq-oss-backup.timer``），**不经过应用内的调度器**。只在调度器里挂
    告警的话，这条路径失败时就是一声不吭。

    生产上 OSS 备份走的是应用内调度器（外部每分钟调 tick），那边已经有
    告警了；这个脚本是**另一条**路径（旧的 timer 与手动补跑），所以它
    自己也要能说话 —— 否则谁装了那个 unit，谁的备份就是静默的。

    同步发送：进程马上就要退出，异步线程会随进程一起死，信就永远送不到。
    """
    try:
        notify_events.backup_failed_blocking("账套备份（每日 OSS）", message)
    except Exception as exc:  # noqa: BLE001
        # 告警本身绝不能把备份脚本搞崩：退出码是 systemd 判断成败的依据，
        # 这里抛出去会让「备份失败」变成「脚本异常」，两件事混在一起。
        print(f"⚠️  告警邮件发送失败（已忽略）：{exc}", file=sys.stderr)


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
            # 配置不全 = 备份**根本没跑**。这不是"少备了几本"，是退路一条
            # 都没留下 —— 最该被知道的情况。
            _通知备份失败(f"配置不全，备份未执行：{e}")
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
            if not cfg_mod.encryption_ready():
                print("⚠️  但**备份加密密钥没配**（BACKUP_ENCRYPTION_KEY）—— "
                      "试跑不拦你，真要上传时会被拒绝（制品必须是密文）。")
            return 0

        try:
            result = service.run_backup(
                session, book_ids=args.books, force_tier=args.tier,
                prune=not args.no_prune)
        except (cfg_mod.ConfigIncomplete, export_mod.ExportRefused) as e:
            # **运行期那道闸**（配了 OSS 就必须配备份加密密钥）就拦在这里，
            # 按**生效配置**判 —— 界面上配的 OSS 也在内，见
            # ``app/backup/config.py::require_encrypted_ready``。
            #
            # **必须变成退出码 2，而不是裸 traceback。** systemd 只看退出码：
            # 一次裸异常落成 exit 1（「脚本崩了」），而真相是「配置没配全」——
            # 这在排障时是两条完全不同的路。退出码 2 的含义本来就是
            # 「配置不全 / 没有任何东西可备」。
            print(f"❌ {e}", file=sys.stderr)
            _通知备份失败(f"配置不全，备份未执行：{e}")
            return 2
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
        失败摘要 = _失败摘要(result)
        if 失败摘要:
            # **部分失败也要告警。** 退出码仍然是 0（这轮确实有东西备份出去了），
            # 但"有 1 本账套没备上"在定时任务里是**看不见的** —— systemd 只看
            # 退出码，而没备上的那本账套是没人会主动去翻的。
            _通知备份失败(失败摘要)
        return 0 if ok else 4
    finally:
        session.close()


def _失败摘要(result) -> str:
    """把这一轮的失败整理成一句话。**空串表示没有失败。**"""
    失败 = list(getattr(result, "failed", []) or [])
    片段 = [f"{run.book_label}：{run.message or '未知原因'}" for run in 失败]
    if result.prune_error:
        片段.append(f"清理过期产物失败：{result.prune_error}")
    if not 片段:
        return ""
    头 = f"{len(失败)} 个备份对象失败" if 失败 else "备份对象都上传成功了"
    return f"{头}；" + "；".join(片段)


def _待重填的凭据(session) -> list[str]:
    """这些凭据现在是空的 —— 也就是运维接下来要补的。

    **为什么要专门列出来。** 备份制品**有意**不带走凭据（审计 P0-1：
    带着 AccessKey 的备份等于把凭据抄送给每一个拿到备份的人）。于是
    恢复完之后那几个凭据是空的 —— 而恢复脚本刚打印过「恢复完成」。
    不说清楚的话，运维看到的是"好了"，下一次备份却因为没有 AccessKey
    而失败，那次失败又发生在没人看的凌晨。
    """
    from app.core.system_settings import get_value

    return [k for k in sorted(export_mod.REDACTED_SETTING_KEYS)
            if not (get_value(session, k, "") or "").strip()]


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
        print("提示：备份制品**不含凭据**（有意为之），恢复后需要在页面上重新配置。")
        return 0
    session.commit()
    print("恢复完成：")
    for name, n in counts.items():
        print(f"  {name:20} {n}")
    待填 = _待重填的凭据(session)
    if 待填:
        print()
        print("⚠️  这份归档**不含凭据**（有意为之，审计 P0-1：带着凭据的备份等于")
        print("    把凭据抄送给每个拿到备份的人）。下面这几项需要重新配置：")
        for key in 待填:
            print(f"     - {key}")
        print("    在「系统管理 → 数据安全」与「邮件通知」页重新填写即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
