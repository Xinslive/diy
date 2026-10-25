#!/usr/bin/env python
"""上线前的存量数据体检（**只读**）。

**这个脚本替的是 §7 第 1 步第 11 项里「怎么做」，不是「谁来做」。**

审计报告的路线图写着：

    11. 先做一次存量数据体检：查 entry 是否已存在同一 voucher_id 下 line_no
        重复的行（§3.15 场景 B 已经能落库）。有重复则先人工清理 ——
        这一步是第 12 项的前置。

「查」这一步代码替得了（在真实库上跑一条只读查询）；「清理」替不了 ——
哪一条重复该留、金额该怎么修正，只有懂业务的人能决定。
所以这个脚本只负责**把问题查清楚、并说清楚**，不替人改一个字节。

**用法**

    cd backend
    .venv/bin/python scripts/data_health_check.py                # 体检当前 .env 指向的库
    .venv/bin/python scripts/data_health_check.py --book-id 3    # 只查某个账套
    .venv/bin/python scripts/data_health_check.py --json         # 机器可读
    .venv/bin/python scripts/data_health_check.py --strict       # 提示项也按阻断处理

**读的是哪个库。** ``app.core.config.settings.database_url`` —— 也就是
``.env`` 里那一个。所以体检生产库的办法是：**在生产机上、用生产机的 .env**
跑这个脚本。拿开发库跑一遍不说明生产库的任何事。

脚本第一行就会把「连的是哪个库」（隐去口令）打出来 —— 事后要能证明
体检的是哪一库。

**它一条写操作都没有。** 体检跑完之后库里的一切应当**逐字节不变**。
这个性质有用：它证明体检本身没有污染被体检的对象。

**退出码**

    0  体检通过（没有阻断项，也没有提示项）
    2  有**阻断项**（重复分录行号）—— **不能上线**
    3  只有提示项（整理就绪不干净），没有阻断项
    1  体检本身没跑成（连不上库、库结构不认识）—— 这**不是**「库有问题」
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 退出码。写死在这里而不是散在 return 语句旁边：调用方（发布流程、
# 运维手册）只认这几个数。
EXIT_OK = 0
EXIT_BROKEN = 1        # 体检自己没跑成 —— 与「库有问题」严格分开
EXIT_BLOCKING = 2      # 有阻断项：不能上线
EXIT_WARNING = 3       # 只有提示项


def _safe_url(raw: str) -> str:
    """连接串脱敏后打印。

    报错里带连接串是排障最有用的一行，但连接串里带口令。
    ``hide_password=True`` 只挡口令，host/port/库名还得留着 ——
    「连不上」和「连错库」是两回事。
    """
    try:
        from sqlalchemy.engine import make_url

        return make_url(raw).render_as_string(hide_password=True)
    except Exception:
        return "（连接串无法解析）"


def _check_duplicates(db, book_id: int | None) -> list[tuple[int, list[int], int]]:
    """检查一：同一张凭证里行号重复（**阻断级**）。

    判据在 ``app.accounting.data_health`` —— 与迁移 0038 的前置检查
    共用同一份实现（审计报告 §4.5：同一个量只允许一个权威实现）。
    """
    from app.accounting.data_health import duplicate_line_no

    return duplicate_line_no(db, book_id=book_id)


def _check_periods(db, book_id: int | None) -> list[dict]:
    """检查二：「整理就绪」体检不干净的期间（**提示级**）。

    判据在 ``app.accounting.organize.check_period`` —— 与「整理凭证」页面
    上那个体检**是同一个**。这里只负责把体检铺开到所有期间。

    为什么要查它（审计报告 §7 第 0 步第 10 项）：新建凭证改用
    **目标期间的凭证字**发号了 —— 一个已经混用了多个凭证字的期间，
    改完之后新发的号可能和存量号不是一个字，那个期间从此「整理就绪」不过，
    记账、结账全被拦。所以**改发号行为之前要先把存量数据体检一遍**。
    """
    from sqlalchemy import select

    from app.accounting.organize import check_period, rows_by_period
    from app.models import Period

    stmt = select(Period)
    if book_id is not None:
        stmt = stmt.where(Period.book_id == book_id)
    periods = list(db.scalars(
        stmt.order_by(Period.book_id, Period.year, Period.month)).all())
    if not periods:
        return []

    rows = rows_by_period(db, periods)
    dirty = []
    for p in periods:
        report = check_period(db, p, rows=rows.get(p.id, []))
        if not report["clean"]:
            dirty.append(report)
    return dirty


def _open_session(session_factory):
    """返回 ``(session, 该不该由我关掉)``。

    ``session_factory`` 给了就用给的 —— 测试要靠它把体检指到自己的会话上
    （测试进程里的默认会话绑的是 worker 专用库，而用例里的数据可能还没提交，
    换一条连接就看不见了）。没给就用生产用的 ``SessionLocal``。
    """
    if session_factory is not None:
        return session_factory(), False
    from app.core.db import SessionLocal

    return SessionLocal(), True


def _hint_for(exc: Exception) -> None:
    """按异常类型补一句「下一步该干什么」。

    **为什么不能只把异常原样打出来。** 原样打出来的话，人看到的是一句
    ``relation "entry" does not exist`` —— 他得自己想到「那大概是连错库了」。
    而这一步是在**生产库**上跑的，操作的人此刻最需要的不是异常的原文，
    是「你连的是不是那个库」。所以这里把两种最常见的情况直接说清楚。
    """
    msg = str(exc)
    if "does not exist" in msg and "relation" in msg:
        print("  疑因：这个库里连表都没有 —— 要么**连错库了**，要么这个库"
              "还没跑过迁移。`entry` 是 0001 基线表，正常库里一定有它。",
              file=sys.stderr)
    elif ("connection failed" in msg or "could not connect" in msg
          or "Connection refused" in msg):
        print("  疑因：数据库**连不上** —— 先确认库进程活着、端口通、"
              "连接串里的库名对。", file=sys.stderr)


def main(argv: list[str] | None = None, *, session_factory=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="上线前的存量数据体检（只读）")
    parser.add_argument("--book-id", type=int, default=None,
                        help="只查某个账套（不给就是全库）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true",
                        help="提示项（整理就绪不干净）也按阻断处理")
    args = parser.parse_args(argv)

    try:
        from app.core.config import settings
    except Exception as exc:
        # 配置都加载不了（.env 缺变量、生产校验没过）—— 这也是「体检没跑成」，
        # 不是「库有问题」。直接抛栈的话，操作的人看到的是一段 Python
        # traceback，还得自己分辨是环境问题还是数据问题。
        print(f"✗ 体检没跑完（应用配置都没加载成功）："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        print("  确认环境变量齐了（DATABASE_URL / JWT_SECRET / APP_ENV）再跑。",
              file=sys.stderr)
        return EXIT_BROKEN

    if not args.json:
        print("存量数据体检（只读）")
        print(f"库：{_safe_url(settings.database_url)}")
        # **必须 flush**：stdout 被重定向到文件时是块缓冲的，stderr 是无缓冲的
        # —— 不 flush 的话，「体检没跑完」那句会跑到「连的是哪个库」前面去。
        # 而那行库名恰恰是事后唯一能证明「体检的是哪一库」的东西，不能被埋掉。
        sys.stdout.flush()

    try:
        db, should_close = _open_session(session_factory)
    except Exception as exc:
        print(f"✗ 体检没跑完（连不上应用配置好的数据库会话）："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_BROKEN

    try:
        try:
            dups = _check_duplicates(db, args.book_id)
            dirty = _check_periods(db, args.book_id)
        except Exception as exc:
            # 走到这里说明体检**没跑完** —— 这与「体检出结果了」是两回事，
            # 退出码必须分开（1 = 没跑成，2/3 = 跑成了且有问题）。
            # 混起来的后果是：发布流程把「体检脚本自己坏了」当成
            # 「库里有问题」，或者反过来 —— 两个都危险，但错的方向不一样。
            print(f"✗ 体检没跑完（不是「库有问题」，是体检自己跑不动）："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            print(f"  连的是：{_safe_url(settings.database_url)}", file=sys.stderr)
            _hint_for(exc)
            return EXIT_BROKEN

        result = {
            "database": _safe_url(settings.database_url),
            "book_id": args.book_id,
            "duplicate_line_no": [
                {"voucher_id": vid, "lines": lines, "count": n}
                for vid, lines, n in dups
            ],
            "unclean_periods": dirty,
            "blocking": bool(dups),
            "warning": bool(dirty),
        }

        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            _print_text(result)

        if dups:
            return EXIT_BLOCKING
        if dirty and args.strict:
            return EXIT_BLOCKING
        if dirty:
            return EXIT_WARNING
        return EXIT_OK
    finally:
        if should_close:
            db.close()


def _print_text(result: dict) -> None:
    """人看的输出。"""
    from app.accounting.data_health import (
        DUPLICATE_HOW_IT_HAPPENED,
        DUPLICATE_HOW_TO_FIX,
        describe_duplicate,
    )

    dups = result["duplicate_line_no"]
    print()
    print("[1/2] 重复分录行号（同一 voucher_id 下 line_no 重复）")
    if not dups:
        print("  ✓ 没有重复行号")
    else:
        print(f"  ✗ 查到 {len(dups)} 组重复：")
        for d in dups:
            print(f"      {describe_duplicate((d['voucher_id'], d['lines'], d['count']))}")
        print(f"    {DUPLICATE_HOW_IT_HAPPENED}")
        print(f"    {DUPLICATE_HOW_TO_FIX}")
        print("    （迁移 0038 撞上重复行会拒绝加唯一约束 —— "
              "先清理，再上线。）")
        print("  → 阻断上线")

    dirty = result["unclean_periods"]
    print()
    print("[2/2] 整理就绪体检（断号 / 重号 / 混用凭证字 / 时序）")
    if not dirty:
        print("  ✓ 所有期间都干净")
    else:
        print(f"  ✗ {len(dirty)} 个期间不干净：")
        for p in dirty:
            print(f"      {p['period']}：" + "；".join(p["blocking"]))
        print("    这些期间不能记账、不能结账（「整理就绪」体检拦着）。")
        print("  → 需人工确认（默认不阻断上线；--strict 会按阻断处理）")

    print()
    if result["blocking"]:
        print("结论：✗ 体检未通过（阻断 1 项、提示 %d 项）—— **不能上线**"
              % len(dirty))
    elif dirty:
        print("结论：! 体检有提示项（%d 个期间不干净）—— 上线前请人工确认"
              % len(dirty))
    else:
        print("结论：✓ 体检通过")


if __name__ == "__main__":
    raise SystemExit(main())
