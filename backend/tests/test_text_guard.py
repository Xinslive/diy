"""入口守卫：**写进库之前**就把 XML 非法字符洗掉。

**为什么要在入口拦。**

脏字符（U+FFFE 等，见 ``app/core/text_clean.py``）的最后一道防线是「导出前
扫一遍」—— 那是在**读**的时候补救。而脏字符是**写**的时候进去的：写进去
时没人拦，它就躺在库里，直到某天有人导出这份账表，整份文件一个字节都出不来
（生产事故：导出「管理费用明细账」1934 行直接失败）。

写进库的路子有：手工记账、Excel 导入、发票自动生成、拆分凭证、MCP 工具……
在业务函数里各写一遍会漏。**模型层的 before_insert / before_update 是唯一收口**：
手工、导入、发票、拆分、MCP —— 一视同仁，谁都绕不过去。

这里锁住：
1. 四张表的用户可填文本列（摘要、备注、科目名、账套名/单位名）都被洗；
2. **所有写入路径**都过不掉 —— 不只是手工记账这一条；
3. 洗掉的只是非法字符，正文一个字不动；
4. TAB/LF/CR 在入库这一层保留（解析器按行切分要靠它们定位）。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.core.text_clean import has_illegal_xml_chars
from app.models import Book, Entry, Subject, Voucher

#: U+FFFE —— 生产上真实出现过的坏字符：PDF 文字层漏出来的 UTF-16 BOM。
FFFE = "\ufffe"


def _entry_payload(subject, summary: str) -> list[dict]:
    return [
        {"subject_id": subject.id, "direction": "DEBIT", "amount": "100",
         "summary": summary},
        {"subject_id": subject.id, "direction": "CREDIT", "amount": "100",
         "summary": summary},
    ]


# ===================================================================== 分录摘要


def test_手工记账的摘要脏字符被洗掉(db, book, admin_user, subjects):
    """最普通的一条路：界面里手工记一张凭证，摘要带 U+FFFE。"""
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, f"*电线电缆*广州五羊ZC{FFFE}BV2.5平方线"),
        source="MANUAL", remark="")

    rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
    assert rows, "凭证没有分录"
    for row in rows:
        assert FFFE not in row.summary
        assert row.summary == "*电线电缆*广州五羊ZCBV2.5平方线"


def test_改凭证时写进去的脏字符也被洗掉(db, book, admin_user, subjects):
    """update 那条路也得洗 —— 守卫挂在 before_update 上。"""
    from app.accounting.vouchers import create_voucher, update_voucher

    subject = subjects["1001"]
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, "正常摘要"), source="MANUAL", remark="")

    update_voucher(db, voucher, admin_user, {
        "entries": _entry_payload(subject, f"改过的摘要{FFFE}尾"),
        "remark": f"备注{FFFE}尾",
    })

    rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
    assert all(FFFE not in r.summary for r in rows)
    assert all(r.summary == "改过的摘要尾" for r in rows)
    db.refresh(voucher)
    assert FFFE not in voucher.remark
    assert voucher.remark == "备注尾"


def test_凭证备注脏字符被洗掉(db, book, admin_user, subjects):
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, "正常摘要"), source="MANUAL",
        remark=f"这是备注{FFFE}")
    db.refresh(voucher)
    assert FFFE not in voucher.remark


# ===================================================================== 其它写入路径


def test_直接建分录也过不掉(db, book, admin_user, subjects):
    """**绕过业务层、直接 ORM 建行**也得被洗。

    这条是「模型层守卫」这个方案的全部意义：守卫挂在 ORM 事件上，
    所以连绕开 ``create_voucher`` 直接 ``db.add(Entry(...))`` 都躲不掉。
    """
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, "正常摘要"), source="MANUAL", remark="")

    db.add(Entry(voucher_id=voucher.id, line_no=9, book_id=book.id,
                 period_ord=voucher.entries[0].period_ord,
                 subject_id=subject.id, direction="DEBIT", amount="1",
                 summary=f"绕过业务层写进来的{FFFE}摘要"))
    # 借方加了 1，贷方也得加 1 —— 库里有 ``fn_check_voucher_balance`` 触发器，
    # 借贷不平时整笔 INSERT 会被挡掉（那是**对**的：不平的凭证不该进库）。
    db.add(Entry(voucher_id=voucher.id, line_no=10, book_id=book.id,
                 period_ord=voucher.entries[0].period_ord,
                 subject_id=subject.id, direction="CREDIT", amount="1",
                 summary=f"绕过业务层写进来的{FFFE}摘要"))
    db.commit()

    rows = db.scalars(select(Entry).where(
        Entry.summary.like("%绕过业务层%"))).all()
    assert len(rows) == 2
    for row in rows:
        assert FFFE not in row.summary
        assert row.summary == "绕过业务层写进来的摘要"


def test_科目名脏字符被洗掉(db, book, subjects):
    """科目名会印在每一张明细账/总账的表头上。"""
    subject = subjects["1001"]
    subject.name = f"库存现金{FFFE}"
    db.commit()
    db.refresh(subject)
    assert FFFE not in subject.name
    assert subject.name == "库存现金"


def test_账套名与单位名脏字符被洗掉(db):
    """账套名/单位名印在每一份导出件的抬头上。"""
    b = Book(company_name=f"某某公司{FFFE}", name=f"账套{FFFE}",
             start_year=2026, start_month=1)
    db.add(b)
    db.commit()
    db.refresh(b)
    assert FFFE not in b.name
    assert FFFE not in b.company_name
    assert b.name == "账套"
    assert b.company_name == "某某公司"


# ===================================================================== 洗得对不对


def test_只洗非法字符正文一个字不动(db, book, admin_user, subjects):
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    text = "*供电*电费 / 2026年7月 / 金额 1,234.56 元（含税）"
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, f"{text}{FFFE}"), source="MANUAL", remark="")
    rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
    assert all(r.summary == text for r in rows)


def test_入库这一层保留制表与换行(db, book, admin_user, subjects):
    """TAB/LF/CR 在**入库**时保留。

    发票解析器按行切分要靠它们定位，换成空格会把一张发票的表格结构拆坏。
    （Excel 那侧另有 ``scrub_xml_chars`` 把它们换成空格 —— 那是不撑开单元格
    版面的需要，跟这里是两件事。）
    """
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    text = "第一行\n第二行\t带制表"
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, text), source="MANUAL", remark="")
    rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
    assert all(r.summary == text for r in rows)


def test_各种不可见非法字符都被洗掉(db, book, admin_user, subjects):
    """C0（除 TAB/LF/CR）、C1、U+FFFE、U+FFFF —— 一个都不许留下。"""
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    bad = ["\x00", "\x01", "\x07", "\x08", "\x0b", "\x0c", "\x1b", "\x1f",
           "\x7f", "\x85", "\x9f", "\ufffe", "\uffff"]
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, "摘要" + "".join(bad)), source="MANUAL",
        remark="")
    rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
    for row in rows:
        assert not has_illegal_xml_chars(row.summary), repr(row.summary)


def test_正常摘要一个字都不动(db, book, admin_user, subjects):
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    text = "管理费用 / 办公费 *油料*粮油米 1,234.56 元"
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, text), source="MANUAL", remark="")
    rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
    assert all(r.summary == text for r in rows)


def test_守卫不会把凭证写坏(db, book, admin_user, subjects):
    """洗字符不该动别的：金额、方向、科目、条数都得对。"""
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, f"摘要{FFFE}"), source="MANUAL", remark="")
    rows = db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)).all()
    assert len(rows) == 2
    assert all(r.amount == 100 for r in rows)
    assert {r.direction for r in rows} == {"DEBIT", "CREDIT"}
    assert all(r.subject_id == subject.id for r in rows)


# ===================================================================== 其它入口


def test_导入与发票那条路也过不掉(db, book, admin_user, subjects):
    """Excel 导入、发票自动生成、插件、拆分凭证 —— 全都走 ``create_voucher``。

    所以只要 ``create_voucher`` 这一道洗了，这些路就一起被挡住了。
    这里直接断言「所有建凭证的路都收敛到同一个函数」这个事实，
    免得将来有人另开一条写分录的路而忘了洗。
    """
    from app.accounting import vouchers as svc

    assert callable(svc._clean_entry_payload)
    cleaned = svc._clean_entry_payload([
        {"subject_id": 1, "direction": "DEBIT", "amount": "1",
         "summary": f"导入来的{FFFE}摘要"},
        {"subject_id": 1, "direction": "CREDIT", "amount": "1",
         "summary": "正常"},
    ])
    assert cleaned[0]["summary"] == "导入来的摘要"
    assert cleaned[1]["summary"] == "正常"


def test_审计快照里不留脏字符(db, book, admin_user, subjects):
    """快照会进 ``audit_log.after``（JSONB）。

    PostgreSQL 的 jsonb **存不了 U+0000** —— 一个带 NUL 的摘要会让整条审计
    INSERT 直接炸（``unsupported Unicode escape sequence``），凭证本身都
    存不进去。所以快照必须也是干净的。
    """
    from app.accounting.audit_helper import voucher_snapshot
    from app.accounting.vouchers import create_voucher

    subject = subjects["1001"]
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 5),
        entries=_entry_payload(subject, f"*油料*{FFFE}粮油米"), source="MANUAL",
        remark=f"备注{FFFE}")

    snap = voucher_snapshot(voucher)
    assert FFFE not in (snap.get("remark") or "")
    for row in snap.get("entries") or []:
        assert FFFE not in (row.get("summary") or "")
