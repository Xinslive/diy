"""用友序时账（.xls）导入。

用友导出的序时账和这套系统原来的凭证导入模板是**两种排版**：

    模板          一张凭证的头一行填日期与凭证号，后续分录行留空
    用友序时账    每一条分录都平铺成一行，**每行都重复**日期与凭证号数

所以同一套解析规则读不对它 —— 照「日期或凭证号非空 = 新的一张」去读，
实测那份 ``商管序时账.xls``（5579 行分录 = 556 张凭证，其中一张有 170 条分录）
会被拆成 5577 张每条都借贷不平衡的废凭证：**导入「成功」，一张没进**。
这是最坏的一类失败 —— 不报错，只是把人骗过去。

这里钉住的四件事：

    1. 同样的日期 + 凭证号 = 同一张凭证的多条分录；
    2. 表尾的「合计」行必须挡掉（实测合计 14,538,288.54，挂到上一张真实
       凭证头上足以把它撑爆）；
    3. 用友每个月都从 0001 重新编号，跨月重号是正常的，不能被当成重复；
    4. 红字（负数）分录保真 —— 冲销是记账的日常。

外加两条**不许被改坏**的回归：普通 xlsx 的断行规则，以及「每行填日期、
凭证号留空交给系统编号」的那种表（它和序时账在行内容上完全一样，
判据只能落在表头上，改错了会把那种表的两张小凭证并成一张）。

用例里的 .xls 是**真的**用 xlwt 写出来的 OLE 复合文档，不是改后缀的 xlsx ——
分流走的是魔数，改后缀的假文件测不出它。
"""
from __future__ import annotations

import io
from decimal import Decimal

import pytest

from app.accounting.importer import parse_workbook

# 用友序时账的真实表头（取自 商管序时账.xls）
JOURNAL_HEADER = ["日期", "凭证号数", "上级科目", "科目编码", "科目名称",
                  "摘要", "方向", "数量", "外币", "金额"]

TEMPLATE_HEADER = ["日期", "凭证号", "摘要", "科目编码", "借方", "贷方"]


def _xls(header: list[str], rows: list[list]) -> bytes:
    """写一个真的 .xls（OLE2），不是改后缀的 xlsx。"""
    import xlwt

    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet("UFPrn")
    for col, name in enumerate(header):
        sheet.write(0, col, name)
    for r, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            if value is None:
                continue
            sheet.write(r, col, value)
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


def _xlsx(header: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _line(day: str, no: str, code: str, name: str, summary: str,
          side: str, amount) -> list:
    """一条序时账分录行：日期与凭证号数逐行重复。"""
    return [day, no, f"上级科目/{code}", code, name, summary, side, 0, 0, amount]


# 一张凭证 = 两条分录，用本账套里真实存在的两个明细科目
_DR = ("1002", "银行存款-工商银行")
_CR = ("5001", "主营业务收入-租金")


def _voucher_rows(day: str, no: str, amount: float, *, summary: str = "收租") -> list[list]:
    return [
        _line(day, no, _DR[0], _DR[1], summary, "借", amount),
        _line(day, no, _CR[0], _CR[1], summary, "贷", amount),
    ]


# 合计行：用友序时账表尾固定带借贷两行，金额是全表发生额合计。
# 这里给一个和任何一条分录都不一样的数，测试才能断言「它没被当成一条分录」。
_TOTAL = 987654.32
_TOTAL_ROWS = [
    ["", "", "", "", "", "合计", "借", 0, 0, _TOTAL],
    ["", "", "", "", "", "", "贷", 0, 0, _TOTAL],
]


# --------------------------------------------------------------------- 排版识别


def test_序时账被认出来(db, book):
    data = _xls(JOURNAL_HEADER, _voucher_rows("2026.01.15", "记-0001", 1000))
    r = parse_workbook(data, book.id, db)
    assert r["journal"] is True


def test_序时账的表头全部识别(db, book):
    """上级科目 / 数量 / 外币 是正常业务列，不能报成「未识别的表头」。

    报出来等于告诉用户「你的文件导出错了」，而错的不是他。
    """
    data = _xls(JOURNAL_HEADER, _voucher_rows("2026.01.15", "记-0001", 1000))
    r = parse_workbook(data, book.id, db)
    assert r["unknown_headers"] == []


def test_普通模板不被当成序时账(db, book):
    data = _xlsx(TEMPLATE_HEADER, [
        ["2026-01-15", "记-1", "收租", "1002", 1000, None],
        [None, None, "收租", "5001", None, 1000],
    ])
    assert parse_workbook(data, book.id, db)["journal"] is False


# --------------------------------------------------------------------- 断行


def test_相同日期与凭证号并成一张(db, book):
    """序时账的核心：每行都重复日期与凭证号数，不能被拆成一张张的单边凭证。"""
    rows = []
    for amount in (1000, 2000, 3000):
        rows += _voucher_rows("2026.01.15", "记-0001", amount)
    data = _xls(JOURNAL_HEADER, rows)
    r = parse_workbook(data, book.id, db)

    assert len(r["vouchers"]) == 1
    v = r["vouchers"][0]
    assert len(v["entries"]) == 6
    assert Decimal(v["total_debit"]) == Decimal(v["total_credit"]) == 6000
    assert v["ok"] is True, v["errors"]


def test_凭证号原样保留(db, book):
    data = _xls(JOURNAL_HEADER, _voucher_rows("2026.01.15", "记-0001", 1000))
    r = parse_workbook(data, book.id, db)
    # 用友是「记-0001」这种，系统按 (期间, 凭证号) 唯一，这串要原样进去
    assert r["vouchers"][0]["voucher_no"] == "记-0001"


def test_换凭证号就换一张(db, book):
    rows = (_voucher_rows("2026.01.15", "记-0001", 1000)
            + _voucher_rows("2026.01.16", "记-0002", 2000))
    r = parse_workbook(_xls(JOURNAL_HEADER, rows), book.id, db)
    assert [v["voucher_no"] for v in r["vouchers"]] == ["记-0001", "记-0002"]


def test_跨月重号是两张凭证(db, book):
    """用友每个月都从 0001 重新编号 —— 跨月重号是正常的，不是重复凭证。

    系统那条唯一约束是 (期间, 凭证号)，跨月的两张落在不同期间，本来就不冲突。
    """
    rows = (_voucher_rows("2026.01.15", "记-0001", 1000)
            + _voucher_rows("2026.02.03", "记-0001", 2000))
    r = parse_workbook(_xls(JOURNAL_HEADER, rows), book.id, db)
    assert len(r["vouchers"]) == 2
    assert [v["voucher_no"] for v in r["vouchers"]] == ["记-0001", "记-0001"]
    assert [v["voucher_date"] for v in r["vouchers"]] == ["2026-01-15", "2026-02-03"]


# --------------------------------------------------------------------- 合计行


def test_合计行被挡掉(db, book):
    """合计行没有日期、没有凭证号、也没有科目。

    它本来会命中「接上一张」的规则被挂到上一张**真实**凭证头上 ——
    实测那份文件的合计是 14,538,288.54。
    """
    rows = _voucher_rows("2026.01.15", "记-0001", 1000) + _TOTAL_ROWS
    r = parse_workbook(_xls(JOURNAL_HEADER, rows), book.id, db)

    assert len(r["vouchers"]) == 1
    assert r["vouchers"][0]["ok"] is True, r["vouchers"][0]["errors"]
    assert len(r["vouchers"][0]["entries"]) == 2
    assert r["footer_rows"] == 2


def test_合计行不会让最后一张凭证多出分录(db, book):
    """这是上一条的实质：合计的 2000 不能混进那张真实凭证的 1000 里。"""
    rows = _voucher_rows("2026.01.15", "记-0001", 1000) + _TOTAL_ROWS
    v = parse_workbook(_xls(JOURNAL_HEADER, rows), book.id, db)["vouchers"][0]
    assert Decimal(v["total_debit"]) == Decimal("1000.00")


# --------------------------------------------------------------------- 红字


def test_红字分录保真(db, book):
    """冲销一张记错的凭证是记账的日常，方向由「方向」列说了算，不是靠正负猜。"""
    rows = [
        _line("2026.01.15", "记-0001", _DR[0], _DR[1], "退租", "借", -2205),
        _line("2026.01.15", "记-0001", _CR[0], _CR[1], "退租", "贷", -2205),
    ]
    v = parse_workbook(_xls(JOURNAL_HEADER, rows), book.id, db)["vouchers"][0]
    assert [e["direction"] for e in v["entries"]] == ["DEBIT", "CREDIT"]
    assert [Decimal(e["amount"]) for e in v["entries"]] == [Decimal("-2205"), Decimal("-2205")]
    assert v["ok"] is True, v["errors"]


def test_同侧相抵的凭证照样该存(db, book):
    """结息 / 费用分摊这类凭证：几笔正数 + 一笔等额负数，**同侧**自己抵平。

    借贷两边各自合计都是 0，但它确实记了东西。原来的判据
    ``dr == 0 and cr == 0`` 拿合计代替「有没有记东西」，会把它误杀，
    还报一句恰恰说反了的「这张凭证什么也没记」。
    实测那份用友序时账 556 张里有 6 张是这种。

    （科目用的是本账套真实存在的三个银行存款明细 —— 这里要验的是
    「同侧相抵放行」这个结构行为，与科目经济含义无关。）
    """
    rows = [
        _line("2026.03.21", "记-0029", "1002", "银行存款-工商银行", "结息", "借", 1000),
        _line("2026.03.21", "记-0029", "1012", "银行存款-建设银行", "结息", "借", 500),
        _line("2026.03.21", "记-0029", "1001", "银行存款-微信支付", "结息", "借", -1500),
    ]
    v = parse_workbook(_xls(JOURNAL_HEADER, rows), book.id, db)["vouchers"][0]
    assert len(v["entries"]) == 3
    assert Decimal(v["total_debit"]) == 0
    assert v["ok"] is True, v["errors"]


def test_每条都是零仍然被挡(db, book):
    """金额 0 的行根本不生成分录，所以「什么都没填」由「没有任何分录」兜住。

    这里同时钉住一件事：**不要**再拿合计为 0 去拦 —— 那会把同侧相抵的
    凭证（上一条）一起误杀。
    """
    rows = [
        _line("2026.01.15", "记-0001", _DR[0], _DR[1], "x", "借", 0),
        _line("2026.01.15", "记-0001", _CR[0], _CR[1], "x", "贷", 0),
    ]
    v = parse_workbook(_xls(JOURNAL_HEADER, rows), book.id, db)["vouchers"][0]
    assert v["ok"] is False
    assert any("没有任何分录" in e for e in v["errors"]), v["errors"]


# --------------------------------------------------------------------- 科目


def test_科目对不上时报出上级科目路径(db, book):
    """序时账的「上级科目」是明细科目的完整路径，对不上时它是最有用的线索。"""
    rows = [
        _line("2026.01.15", "记-0001", "999999", "不存在", "x", "借", 1000),
        _line("2026.01.15", "记-0001", _CR[0], _CR[1], "x", "贷", 1000),
    ]
    v = parse_workbook(_xls(JOURNAL_HEADER, rows), book.id, db)["vouchers"][0]
    assert v["ok"] is False
    assert any("999999" in e and "上级科目路径" in e for e in v["errors"]), v["errors"]


# --------------------------------------------------------------------- 回归


def test_普通模板的断行规则没被改(db, book):
    """一张凭证几行、后续行留空 —— 这条规则本来就对，不许被序时账带歪。"""
    data = _xlsx(TEMPLATE_HEADER, [
        ["2026-01-15", "记-1", "收租", "1002", 1000, None],
        [None, None, "收租", "5001", None, 1000],
        ["2026-01-16", "记-2", "收租", "1002", 2000, None],
        [None, None, "收租", "5001", None, 2000],
    ])
    r = parse_workbook(data, book.id, db)
    assert len(r["vouchers"]) == 2
    assert len(r["vouchers"][0]["entries"]) == 2


def test_没有凭证号时不会把同日期的行并起来(db, book):
    """「日期相同且凭证号相同 = 接上一张」这条规则不许溢出到非序时账的表。

    没有凭证号的表里，凭证号是空的，判据就该落空 —— 每行各自成张，
    与改动前完全一致。要是这条规则只看日期不看凭证号，下面这四行
    （本该是 2026-01-15 的两张凭证，凭证号留空交给系统编号）
    会被并成一张 3000 块的大凭证。
    """
    data = _xlsx(TEMPLATE_HEADER, [
        ["2026-01-15", None, "收租", "1002", 1000, None],
        ["2026-01-15", None, "收租", "5001", None, 1000],
        ["2026-01-15", None, "收租", "1002", 2000, None],
        ["2026-01-15", None, "收租", "5001", None, 2000],
    ])
    r = parse_workbook(data, book.id, db)
    # 4 行各自成张（每张只有一条分录、借贷不平衡）—— 这就是改动前的行为，
    # 重点是没有被并成一张。
    assert len(r["vouchers"]) == 4
    assert all(len(v["entries"]) == 1 for v in r["vouchers"])


# --------------------------------------------------------------------- 格式分流


def test_改后缀的xls照样读得开(db, book):
    """分流按**文件内容**，文件名只作兜底 —— 改了后缀不能就拒收。"""
    data = _xls(JOURNAL_HEADER, _voucher_rows("2026.01.15", "记-0001", 1000))
    r = parse_workbook(data, book.id, db, filename="其实是xlsx.xlsx")
    assert len(r["vouchers"]) == 1 and r["vouchers"][0]["ok"] is True


def test_xlsx改名成xls不会喂给xlrd(db, book):
    """反向也一样：zip 就按 zip 读，不能让 xlrd 接着而报一句看不懂的错。"""
    data = _xlsx(TEMPLATE_HEADER, [
        ["2026-01-15", "记-1", "收租", "1002", 1000, None],
        [None, None, "收租", "5001", None, 1000],
    ])
    r = parse_workbook(data, book.id, db, filename="其实是xls.xls")
    assert len(r["vouchers"]) == 1 and r["vouchers"][0]["ok"] is True


def _safe_parse(data: bytes, book, db) -> str:
    try:
        parse_workbook(data, book.id, db)
    except ValueError as exc:
        return str(exc)
    return ""


def test_非表格文件报错口径没变(db, book):
    with pytest.raises(ValueError, match="无法读取 Excel 文件"):
        parse_workbook(b"not-an-excel-file", book.id, db)


def test_非表格文件的报错不叠罗汉(db, book):
    """分流器的两个 reader 各自报「无法读取 .xls/.xlsx 文件：…」，
    对外要合成一句，不能出现「无法读取 Excel 文件：无法读取 .xlsx 文件：…」。"""
    msg = _safe_parse(b"not-an-excel-file", book, db)
    assert msg.count("无法读取") == 1, msg
    assert msg.startswith("无法读取 Excel 文件："), msg


def test_非表格文件的报错不叠罗汉(db, book):
    """分流器的两个 reader 各自报「无法读取 .xls/.xlsx 文件：…」，
    对外要合成一句，不能出现「无法读取 Excel 文件：无法读取 .xlsx 文件：…」。"""
    msg = _safe_parse(b"not-an-excel-file", book, db)
    assert msg.count("无法读取") == 1, msg


# --------------------------------------------------------------------- 接口层


def test_接口收xls(client, auth, book):
    rows = _voucher_rows("2026.01.15", "记-0001", 1000) + _TOTAL_ROWS
    r = client.post(f"/api/books/{book.id}/vouchers/import", headers=auth,
                    files={"file": ("商管序时账.xls", _xls(JOURNAL_HEADER, rows),
                                    "application/vnd.ms-excel")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["error_count"] == 0
    assert body["journal"] is True
    assert body["footer_rows"] == 2


def test_接口仍然拒非表格后缀(client, auth, book):
    r = client.post(f"/api/books/{book.id}/vouchers/import", headers=auth,
                    files={"file": ("凭证.csv", b"a,b,c", "text/csv")})
    assert r.status_code == 400
    assert ".xls" in r.json()["detail"]


def test_xls走完暂存到入库(db, book, admin_user):
    """解析 → 确认入库整条路，凭证号原样落库，合计行没被带进去。"""
    from sqlalchemy import select

    from app.accounting.importer import commit, stage
    from app.models import Entry, Voucher

    rows = (_voucher_rows("2026.01.15", "记-0001", 1000)
            + _voucher_rows("2026.02.03", "记-0001", 2000)   # 跨月重号
            + _TOTAL_ROWS)
    batch = stage(db, book.id, admin_user, "商管序时账.xls", _xls(JOURNAL_HEADER, rows))
    assert batch.total == 2 and batch.error_count == 0

    r = commit(db, batch, admin_user)
    assert r["ok"] is True, r
    assert r["created"] == 2

    vs = db.scalars(select(Voucher).where(Voucher.book_id == book.id)).all()
    # 表里写的是「记-0001」，落库是「记0001」—— 导入**做格式归一**（用户
    # 2026-10-19 定的形态）。不归一的后果不是"看着不齐"：发号正则要求凭证字后
    # **紧跟**数字，带横杠的那几张它一条都看不见，next_voucher_no 于是把这个
    # 已记到 0004 的期间当成空期间，从 1 重新发号。2026-10-19 生产事故就是这么
    # 来的（那次还叠加了一张号被改成 "wochakan" 的脏数据，四种排序全部 500）。
    assert sorted(v.voucher_no for v in vs) == ["记0001", "记0001"]
    assert sorted(v.period_id for v in vs)[0] != sorted(v.period_id for v in vs)[1]
    assert all(v.source == "IMPORT" for v in vs)
    # 两张各两条分录；合计行的 987654.32 没有变成第 5、6 条分录
    assert db.scalar(select(Entry).where(Entry.book_id == book.id,
                                          Entry.amount == _TOTAL)) is None
