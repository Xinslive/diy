"""年度作用域：科目与期初挂在**哪一年**的科目表上（2026-10-31 生产实测后补）。

**生产上发生了什么。**

账套「泉州劲松珠宝有限公司」有 2023~2026 **四张**科目表（迁移 0032：
科目表每个会计年度各有一份，同一个编码在不同年度是**两行不同的记录**、
两个 id）。用户在 **2023 年**那张表上给 1123 建下级，接口回一句
「上级科目不存在」—— 父科目明明就摆在屏幕上。

根因有两条，都是**年度没传到后端**：

1. ``POST /books/{id}/subjects`` 从**查询参数**读年度（``?year=``），
   而前端把年度放在**请求体**里。FastAPI 不会把请求体的字段当查询参数读
   → 年度被**静默丢掉** → 后端退回「账套当前在记的那一年」
   （``accounting/years.resolve_year``，这本是 2026，因为 2026 有凭证）
   → 拿 **2026 年**的科目表去认 **2023 年**的父科目 → 「上级科目不存在」。
2. ``POST /books/{id}/openings/parse`` 压根**没带年度** → 同样按 2026 年认科目
   → 解析出来的每一行都指着 **2026 年的科目 id** → 前端往 **2023 年**的
   表格里填，一个都对不上 → **表格全是 0** → 刷新清空 → 保存把那 0 存进去
   （后端 ``set_openings`` 的语义是「整年替换」，于是这一年的期初快照
   **被清空**）→ 死循环，钱一分没进去。

所以这一组用例钉的是**年度作用域**这一条：

* 请求带了哪一年的年度，东西就落在**那一年**（查询参数与请求体两处都认）；
* 年度对不上时**必须报错**，而且要把是哪一年说清楚 ——
  不许静默落到另一张表上，因为落错了在界面上一个字都看不出来。
"""
from __future__ import annotations

import io

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.accounting.periods import ensure_periods
from app.accounting.years import copy_chart, ensure_book_year
from app.models import Subject

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def two_year_book(db, book):
    """给账套装上 2027 年度：年度登记 + 12 个期间 + 一份自己的科目表。

    与年度结转铺新年度科目表同一套做法（``copy_chart`` 按**编码**复制）——
    编码是唯一稳定的对应关系，新行是新 id。
    """
    ensure_book_year(db, book, 2027)
    ensure_periods(db, book, 2027)
    copy_chart(db, book.id, 2026, 2027)
    db.commit()
    db.expire_all()
    return book


def _subjects_of(db, book, year: int) -> dict[str, Subject]:
    return {s.code: s for s in db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.year == year)
        .order_by(Subject.code)).all()}


def _payload(**kw) -> dict:
    base = {"code": "100199", "name": "库存现金-子户", "category": "ASSET",
            "direction": "DEBIT"}
    base.update(kw)
    return base


def _openings_xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["科目编码", "科目名称", "方向", "期初余额"])
    ws.append(["1001", "库存现金", "借", 1000])
    ws.append(["2001", "短期借款", "贷", 1000])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------ 新建科目落哪一年


def test_年度放在请求体里时科目落在那一年(client, auth, db, two_year_book):
    """**请求体这一路以前是死的**（接口只认查询参数）。

    生产实测：前端把年度放在 body 里，接口从查询参数读 → 年度被丢掉 →
    在 2023 年的表上建 1123 的下级，被当成「2026 年的表里没这个父科目」。
    """
    parent = _subjects_of(db, two_year_book, 2027)["1001"]

    r = client.post(f"/api/books/{two_year_book.id}/subjects", headers=auth,
                    json=_payload(parent_id=parent.id, year=2027))
    assert r.status_code == 201, f"请求体里带年度应当能建出来：{r.status_code} {r.text}"

    db.expire_all()
    made = db.scalar(select(Subject).where(
        Subject.book_id == two_year_book.id, Subject.year == 2027,
        Subject.code == "100199"))
    assert made is not None, "科目没落在 2027 年那张表上"
    assert made.parent_id == parent.id


def test_年度放在查询参数里时科目落在那一年(client, auth, db, two_year_book):
    """查询参数是本项目年度作用域接口的**同一套写法**（``?year=``）。"""
    parent = _subjects_of(db, two_year_book, 2027)["1001"]

    r = client.post(f"/api/books/{two_year_book.id}/subjects?year=2027", headers=auth,
                    json=_payload(parent_id=parent.id))
    assert r.status_code == 201, f"{r.status_code} {r.text}"

    db.expire_all()
    made = db.scalar(select(Subject).where(
        Subject.book_id == two_year_book.id, Subject.year == 2027,
        Subject.code == "100199"))
    assert made is not None, "科目没落在 2027 年那张表上"


def test_两处都带年度时以查询参数为准(client, auth, db, two_year_book):
    """两处都收，就得有一处说了算 —— 否则又是一条「看谁先跑」的隐式规则。"""
    parent27 = _subjects_of(db, two_year_book, 2027)["1001"]

    r = client.post(f"/api/books/{two_year_book.id}/subjects?year=2027", headers=auth,
                    json=_payload(parent_id=parent27.id, year=2026))
    assert r.status_code == 201, f"{r.status_code} {r.text}"

    db.expire_all()
    made = db.scalar(select(Subject).where(
        Subject.book_id == two_year_book.id, Subject.code == "100199"))
    assert made is not None
    assert made.year == 2027, "查询参数应当压过请求体"


# -------------------------------------------------- 年度对不上：必须报错


def test_父科目在别的年度时拒掉并说清是哪一年(client, auth, db, two_year_book):
    """**报错必须说出是哪一年** —— 只说「上级科目不存在」的话，
    人只会盯着屏幕上那个明明存在的父科目想不通。"""
    parent = _subjects_of(db, two_year_book, 2027)["1001"]

    # 不带年度 → 后端按「账套当前在记的那一年」= 2026（夹具账套启用年度）
    r = client.post(f"/api/books/{two_year_book.id}/subjects", headers=auth,
                    json=_payload(parent_id=parent.id))
    assert r.status_code == 400, f"跨年度挂父科目不该被放行：{r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert "2027" in detail and "2026" in detail, f"报错里没说是哪一年：{detail}"

    db.expire_all()
    assert db.scalar(select(Subject).where(
        Subject.book_id == two_year_book.id, Subject.code == "100199")) is None, \
        "被拒的请求不该留下科目"


def test_年度对得上时同一句话不误伤(client, auth, db, two_year_book):
    """上一条不能变成「只要父科目不是当年就一律拒」——
    年度说对了就得能建。"""
    parent = _subjects_of(db, two_year_book, 2027)["1001"]

    r = client.post(f"/api/books/{two_year_book.id}/subjects?year=2027", headers=auth,
                    json=_payload(parent_id=parent.id))
    assert r.status_code == 201, f"{r.status_code} {r.text}"


# ------------------------------------------------------------ 期初解析的年度


def test_解析期初不带年度被拒(client, auth, two_year_book):
    """**不带年度就猜**是这一类事故的根源：猜错时不报错，
    只是把数写到另一年的科目 id 上，界面上一个字都看不出来。

    现在直接拒，并把话说清楚（让人知道刷新一下就好）。
    """
    r = client.post(f"/api/books/{two_year_book.id}/openings/parse", headers=auth,
                    files={"file": ("期初.xlsx", _openings_xlsx(), XLSX_MIME)})
    assert r.status_code == 400, f"缺年度应当被拒：{r.status_code} {r.text}"
    assert "年度" in r.json()["detail"], r.json()


def test_解析期初认的是指定那一年的科目(client, auth, db, two_year_book):
    """带年度时：**认的必须是那一年**的科目 id，并把年度回给调用方。

    调用方要能自己核对「拿到的这些 id 是不是我页面上那一批」——
    以前响应里没有年度，它**无从判断**对不对上，只能看着 0 发呆。
    """
    r = client.post(f"/api/books/{two_year_book.id}/openings/parse?year=2027", headers=auth,
                    files={"file": ("期初.xlsx", _openings_xlsx(), XLSX_MIME)})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    res = r.json()
    assert res.get("year") == 2027, f"响应里没有年度：{res.get('year')}"

    ids_2027 = {s.id for s in _subjects_of(db, two_year_book, 2027).values()}
    ids_2026 = {s.id for s in _subjects_of(db, two_year_book, 2026).values()}
    matched = [row for row in res.get("rows", []) if row.get("subject_id")]
    assert matched, "一份标准余额表不该一条都没认上"
    for row in matched:
        assert row["subject_id"] in ids_2027, "认到的是别的年度的科目"
        assert row["subject_id"] not in ids_2026
