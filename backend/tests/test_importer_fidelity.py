"""Excel 导入的保真（审计 P1-07）。

三处叠加造成静默丢数据：

1. ``_as_decimal`` 解析失败 ``return Decimal("0")`` —— 非法金额被吞成 0
2. ``if subject is None or (dr <= 0 and cr <= 0): continue`` —— 红字（负数）
   分录在这一行被丢弃
3. ``direction = "DEBIT" if dr > 0 else "CREDIT"`` —— 方向由正负强推

审计实测：含 5 条分录的导入文件（正常行 + 非法金额行 + 负数行），
实际只解析出 2 条，批次 ``ok`` 标志却是 **true**，错误列表**是空的**。

导入成功率虚高：用户以为凭证齐了，实际少记了金额，而且不产生任何告警。
这类缺陷在财务软件里最容易造成实际损失 —— 因为它悄无声息。

这里的断言口径是「被丢弃的行必须在错误列表里可见」，
而不是「导入别报错」—— 后者正是老实现能通过的写法。
"""
from __future__ import annotations

import io
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accounting.importer import commit, parse_workbook, stage
from app.models import Entry, ImportBatch, Voucher


def _xlsx(header, rows) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEAD = ["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"]


def test_非法金额不再被吞成0(db, book):
    """金额列写「金额待定」这种值，必须出现在错误列表里。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "x", "1002", "金额待定", 0],
        [None, None, "x", "5001", 0, 1000],
    ])
    r = parse_workbook(data, book.id, db)

    v = r["vouchers"][0]
    assert v["ok"] is False, "非法金额的凭证被判定为通过了"
    assert any("无法解析为金额" in e for e in v["errors"]), v["errors"]


def test_非法金额时批次ok标志与实际入库一致(db, book):
    """审计的核心指标：批次不能一边丢数据一边报 ok=true。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "x", "1002", "金额待定", 0],
        [None, None, "x", "5001", 0, 1000],
    ])
    b = stage(db, book.id, _admin(db), "脏数据.xlsx", data)

    assert b.ok_count == 0, f"批次仍报 {b.ok_count} 张通过"
    assert b.error_count >= 1
    # 错误列表里看得见那行
    errs = b.payload["vouchers"][0]["errors"]
    assert any("无法解析为金额" in e for e in errs), errs


def test_百分比金额被报告(db, book):
    """整列从别处粘串了，99% 的单元格都带百分号 —— 原来全部静默变 0。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "x", "1002", "50%", 0],
        [None, None, "x", "5001", 0, 500],
    ])
    r = parse_workbook(data, book.id, db)
    v = r["vouchers"][0]
    assert v["ok"] is False
    assert any("百分号" in e for e in v["errors"]), v["errors"]


def test_红字分录不再被丢弃(db, book):
    """冲销一张记错的凭证是记账的日常 —— 恰恰是最该保真的场景。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "冲上月的错账", "1002", -500, 0],
        [None, None, "冲上月的错账", "5001", 0, -500],
    ])
    r = parse_workbook(data, book.id, db)

    v = r["vouchers"][0]
    assert len(v["entries"]) == 2, f"红字分录被丢弃了：{v}"
    assert v["ok"] is True, v["errors"]
    assert all(Decimal(e["amount"]) == Decimal("-500") for e in v["entries"]), \
        f"红字的符号没保住：{[e['amount'] for e in v['entries']]}"
    assert [e["direction"] for e in v["entries"]] == ["DEBIT", "CREDIT"]


def test_红字凭证方向靠列位置不靠正负(db, book):
    """负数在贷方列就该是贷方，不能因为「不是正数」就当成借方。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "红字", "1002", 0, -300],
        [None, None, "红字", "560206", -300, 0],
    ])
    r = parse_workbook(data, book.id, db)
    entries = r["vouchers"][0]["entries"]
    by_code = {e["subject_code"]: e for e in entries}

    assert by_code["1002"]["direction"] == "CREDIT"
    assert Decimal(by_code["1002"]["amount"]) == Decimal("-300")
    assert by_code["560206"]["direction"] == "DEBIT"
    assert Decimal(by_code["560206"]["amount"]) == Decimal("-300")


def test_同一行借贷都填了金额必须报错(db, book):
    """一行只能有一条分录。原来的代码只按借方算，贷方那栏凭空消失。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "x", "1002", 500, 500],
    ])
    r = parse_workbook(data, book.id, db)
    v = r["vouchers"][0]
    assert v["ok"] is False
    assert any("借方和贷方都填了金额" in e for e in v["errors"]), v["errors"]


def test_审计场景_5条分录只入2条且报成功(db, book):
    """复刻审计报告里的那张表：5 条来源分录 → 只解析出 2 条 → 却报成功。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "正常行", "1002", 1000, 0],      # 1 正常
        [None, None, "正常行", "5001", 0, 1000],                 # 2 正常
        [None, None, "金额错", "1002", "错误金额", 0],            # 3 非法金额
        [None, None, "红字行", "1002", -300, 0],                 # 4 红字
        [None, None, "红字行", "5001", 0, -300],                 # 5 红字
    ])
    r = parse_workbook(data, book.id, db)
    v = r["vouchers"][0]

    assert v["ok"] is False, "丢了 3 条分录却仍然报成功"
    # 被丢弃的每一行都有说法
    errs = " | ".join(v["errors"])
    assert "无法解析为金额" in errs, errs
    # 红字那两条进来了
    assert len([e for e in v["entries"] if e["amount"].startswith("-")]) == 2


def test_原始金额被保留下来(db, book):
    """出错时要能对回源文件 —— 光看解析后的数字认不出是哪一格。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "x", "1002", "1,234.50", 0],
        [None, None, "x", "5001", 0, 1234.50],
    ])
    r = parse_workbook(data, book.id, db)
    e = r["vouchers"][0]["entries"][0]
    assert Decimal(e["amount"]) == Decimal("1234.50")
    assert e["amount_raw"] == "1,234.50", "原始值没留住"


def test_同侧相抵的凭证被拒(db, book):
    """一条都没记的凭证不该放行 —— 这里说的是**没有分录**。

    原用例写的是 ``借 500`` / ``借 -500`` 两笔（同一个科目、同一个方向），
    名字叫「借贷相抵为 0 的凭证」，docstring 却写「**全零**凭证什么也没记」。
    两句话说的不是一回事：那份数据不是全零，它是一笔正的加一笔负的，
    净发生额为 0 而已 —— 用友的结息、团体险分摊、工会经费返还全是这个形状
    （实测那份商管序时账 556 张里有 6 张）。拿合计为 0 去拦它们，报出来的
    还是「什么也没记」，恰恰说反了。

    所以判据回到本用例自己声明的那件事：**没有分录**。
    金额 0 的行根本不生成分录，所以「一行都没填」必然落在这里。
    """
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "x", "1002", 0, 0],
        [None, None, "x", "1002", 0, 0],
    ])
    r = parse_workbook(data, book.id, db)
    assert r["vouchers"][0]["ok"] is False
    assert "没有任何分录" in " | ".join(r["vouchers"][0]["errors"])


def test_同侧相抵但记了东西的凭证放行(db, book):
    """与上一条正对着：500 与 -500 抵平，但它**确实记了两笔**，该放行。

    用来友的序时账时，这是绕不开的一类凭证。
    """
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "x", "1002", 500, 0],
        [None, None, "x", "1002", -500, 0],
    ])
    r = parse_workbook(data, book.id, db)
    v = r["vouchers"][0]
    assert len(v["entries"]) == 2
    assert v["ok"] is True, v["errors"]


def test_说明文字行不会被当成错误(db, book):
    """导出模板底部有说明文字，收紧不能把这些行也报错。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "x", "1002", 1000, 0],
        [None, None, "x", "5001", 0, 1000],
        [],
        ["说明：", None, "一张凭证占多行，第二行起日期与凭证号留空", None, None, None],
    ])
    r = parse_workbook(data, book.id, db)
    v = r["vouchers"][0]
    assert v["ok"] is True, v["errors"]
    assert len(v["entries"]) == 2


def _admin(db):
    from app.models import SysUser

    u = db.query(SysUser).filter_by(is_admin=True).first()
    return u


def test_红字凭证能真正入库并保留符号(db, book):
    """不只是解析对，写进库里也得是红字。"""
    data = _xlsx(HEAD, [
        ["2026-01-05", "记-1", "红字冲销", "1002", -500, 0],
        [None, None, "红字冲销", "5001", 0, -500],
    ])
    b = stage(db, book.id, _admin(db), "红字.xlsx", data)
    r = commit(db, b, _admin(db))

    assert r["ok"] is True, r
    v = db.scalars(select(Voucher).where(Voucher.book_id == book.id)).first()
    amounts = sorted(Decimal(e.amount) for e in v.entries)
    assert amounts == [Decimal("-500"), Decimal("-500")], f"入库的金额被改写了：{amounts}"
