"""解析失败的发票能不能自己救回来。

用户提的是两件事，根子是同一件：

1. **一张分页票传上去被告知「重复发票」**（另一条线的改动，见
   ``test_invoice_multipage_merge.py``）；
2. **解析失败的发票没有「重新解析」按钮** —— 解析是在**上传那一刻**跑的，
   解析器认不出某种票面写法时（PDF 把汉字拆成独立一格，明细表头认不出来），
   整张票就报「没有可入账的发票明细」。解析器后来修好了，可这批票已经躺在
   库里 —— 让用户把几百张票重传一遍，等于让他白干一遍。

这里钉住：

* **表头被拆开时也要认得出明细**（``项 目 名称`` 那种写法）；
* **重新解析能把一张失败的发票救回来**，而且不需要重传整个批次；
* **判重没有被放宽**：重新解析出来的号码撞了已有的，照样报错。
"""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select

from app.invoice import service
from app.core.config import settings
from app.models import InvoiceRecord

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "invoices"
BUYER_TAX_ID = "91420100MA1234567X"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """发票原件落到临时目录 —— 绝不写进仓库的 data/invoices。"""
    monkeypatch.setattr(settings, "invoice_storage_dir", str(tmp_path / "invoices"))
    return tmp_path / "invoices"


@pytest.fixture
def configured(db, book, admin_user, subjects):
    """配好税号与科目规则：预览里不该因为「没配科目」而全红。

    规则也要配 —— 否则重新解析出来的明细**命中不了任何科目**，于是报的是
    「没有命中科目规则」而不是「没有可入账的发票明细」：那测的就不是
    「解析有没有救回来」，而是「规则配没配」。
    """
    service.save_config(db, book.id, {
        "taxpayer_id": BUYER_TAX_ID,
        "output_tax_subject_id": subjects["22210106"].id,
        "input_credit_subject_id": subjects["1002"].id,
        "output_debit_subject_id": subjects["1002"].id,
    })
    for keyword, code in (("餐饮", "560202"), ("保洁", "560207")):
        service.create_rule(db, book.id, admin_user, {
            "match_type": "PROJECT", "invoice_type": "BOTH", "keyword": keyword,
            "subject_id": subjects[code].id, "priority": 0, "active": True,
        })
    return book


# ------------------------------------------------------------------ 解析层


def test_表头被PDF拆开也要认得出明细():
    """「项 目 名称 规格型号 …」—— 字与字之间有空格时也要认得出表头。

    原来那条正则只认连续的「项目名称」，于是这种票的明细区段整个找不着 →
    一行明细都提不出来 → 整张票报「没有可入账的发票明细」。
    而这张票的号码、日期、买卖方、价税合计**全都认得出来**，只有明细是空的。
    """
    from app.invoice.text_parser import RE_TABLE_HEADER, extract_lines

    text = (
        "项 目 名称 规格型号 单 位 数 量 单 价 金 额 税率/征收率 税 额\n"
        "合 计\n"
        "*家具*厨柜 组 2 450.49504950495 900.99 1% 9.01\n"
    )
    assert RE_TABLE_HEADER.search(text), "拆开的表头没认出来"
    lines = extract_lines(text)
    assert len(lines) == 1, f"明细提出来了 {len(lines)} 行"
    assert lines[0].amount == __import__("decimal").Decimal("900.99")
    assert lines[0].tax == __import__("decimal").Decimal("9.01")


def test_正常的表头照样认得():
    from app.invoice.text_parser import RE_TABLE_HEADER

    assert RE_TABLE_HEADER.search("项目名称 规格型号 单位 数量 单价 金额 税率 税额")


# ------------------------------------------------------------------ 重新解析


def _flaky_parser(real):
    """模拟「上传那会儿解析器还认不出这一页」。

    真实场景就是「项 目 名称」那种表头：解析器修好之前提不出明细。
    这里不重造一张票，而是让解析结果在**上传时**没有明细、在**重新解析时**有 ——
    前后是同一个解析器，区别只在它当时是不是好的。
    """
    state = {"broken": True, "number": ""}

    def parse_page(index, text):
        invoice = real(index, text)
        if state["broken"]:
            invoice.lines = []
            invoice.parse_warning = "没有可入账的发票明细"
        if state["number"]:
            invoice.meta.invoice_no = state["number"]
        return invoice

    parse_page.state = state
    return parse_page


def test_重新解析把失败的发票救回来(db, store, configured, admin_user, monkeypatch):
    import app.invoice.parser as parser_mod
    import app.invoice.service as service_mod

    real = parser_mod.parse_page
    flaky = _flaky_parser(real)
    monkeypatch.setattr(parser_mod, "parse_page", flaky)
    monkeypatch.setattr(service_mod, "parse_page", flaky)

    batch = service.create_batch(db, configured.id, admin_user,
                                 [("十月发票.pdf", (FIXTURES / "inv_a.pdf").read_bytes())])
    record = db.scalar(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id))
    assert record is not None
    assert not (record.payload or {}).get("lines"), "前置：这张应当是解析不出来的"

    # 解析器修好了，用户点「重新解析」
    flaky.state["broken"] = False
    result = service.reparse_record(db, batch, record, {})

    assert result["error_count"] == 0, f"重新解析后还有 {result['error_count']} 张有问题"
    assert result["ready_count"] == 1, "重新解析后这张应当可生成"
    lines = result["invoices"][0]["lines"]
    assert lines, "重新解析后仍然没有明细"


def test_重新解析不会动别的发票(db, store, configured, admin_user, monkeypatch):
    """只重跑那一页 —— 别的记录一根汗毛都不动。"""
    import app.invoice.parser as parser_mod
    import app.invoice.service as service_mod

    real = parser_mod.parse_page
    flaky = _flaky_parser(real)
    monkeypatch.setattr(parser_mod, "parse_page", flaky)
    monkeypatch.setattr(service_mod, "parse_page", flaky)

    batch = service.create_batch(db, configured.id, admin_user,
                                 [("十月发票.pdf", (FIXTURES / "inv_a.pdf").read_bytes())])
    record = db.scalar(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id))
    flaky.state["broken"] = False
    before = db.scalar(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id))

    service.reparse_record(db, batch, record, {})
    after = db.scalar(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id))
    assert after is not None
    assert after.id != before.id, "重新解析应当换掉原来那条记录"
    assert after.invoice_no == before.invoice_no, "号码不该变"


def test_重新解析撞上已有的发票照样报错(db, store, configured, admin_user, monkeypatch):
    """判重没有被放宽：号码撞了账上已有的，就该报错，不悄悄写进去。

    撞的是**另一个批次**里的票 —— 库层对 (账套, 发票号码) 有唯一约束，
    同一个批次里压根塞不进两条同号的记录，所以这条只能那样造。
    """
    import app.invoice.parser as parser_mod
    import app.invoice.service as service_mod

    real = parser_mod.parse_page
    flaky = _flaky_parser(real)
    monkeypatch.setattr(parser_mod, "parse_page", flaky)
    monkeypatch.setattr(service_mod, "parse_page", flaky)

    # 先传一张正常的（号码 24417000000022222222），它占着这个号
    other_batch = service.create_batch(db, configured.id, admin_user,
                                       [("另一张.pdf", (FIXTURES / "inv_b.pdf").read_bytes())])

    batch = service.create_batch(db, configured.id, admin_user,
                                 [("十月发票.pdf", (FIXTURES / "inv_a.pdf").read_bytes())])
    record = db.scalar(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id))
    flaky.state["broken"] = False
    # 让重新解析认出另一个批次已经有的号码
    flaky.state["number"] = "24417000000022222222"

    with pytest.raises(ValueError, match="重复"):
        service.reparse_record(db, batch, record, {})

    # 报错之后那张票仍然是原来的样子，没有被改掉
    db.expire_all()
    again = db.scalar(select(InvoiceRecord).where(InvoiceRecord.id == record.id))
    assert again is not None, "失败的重新解析不该把原来的记录弄丢"
    assert again.invoice_no == "24417000000011111111"


# ------------------------------------------------------------------ 接口层


def test_接口_重新解析走的是那个路由(client, auth, configured, store, admin_user):
    """路由本身也要通 —— 服务层对了但路由没接上，按钮点了是 404。"""
    created = client.post(
        f"/api/books/{configured.id}/invoices/batches",
        headers=auth,
        files={"files": ("十月发票.pdf", (FIXTURES / "inv_a.pdf").read_bytes(), "application/pdf")},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    record_id = body["invoices"][0]["id"]

    response = client.post(
        f"/api/books/{configured.id}/invoices/batches/{body['id']}/records/{record_id}/reparse",
        headers=auth, json={})
    assert response.status_code == 200, response.text
    assert response.json()["ready_count"] == 1


def test_接口_不属于这个批次的记录报404(client, auth, configured, store, admin_user):
    created = client.post(
        f"/api/books/{configured.id}/invoices/batches",
        headers=auth,
        files={"files": ("十月发票.pdf", (FIXTURES / "inv_a.pdf").read_bytes(), "application/pdf")},
    )
    body = created.json()
    response = client.post(
        f"/api/books/{configured.id}/invoices/batches/{body['id']}/records/999999/reparse",
        headers=auth, json={})
    assert response.status_code == 404, response.text


def test_重新解析把人工改过的东西搬到新记录上(db, store, configured, admin_user, monkeypatch):
    """记录换了新的 id，用户改过的那张发票还是那张 —— 修改不能跟着旧记录消失。"""
    import app.invoice.parser as parser_mod
    import app.invoice.service as service_mod

    real = parser_mod.parse_page
    flaky = _flaky_parser(real)
    monkeypatch.setattr(parser_mod, "parse_page", flaky)
    monkeypatch.setattr(service_mod, "parse_page", flaky)

    batch = service.create_batch(db, configured.id, admin_user,
                                 [("十月发票.pdf", (FIXTURES / "inv_a.pdf").read_bytes())])
    record = db.scalar(select(InvoiceRecord).where(InvoiceRecord.batch_id == batch.id))
    flaky.state["broken"] = False

    # 用户先前在这张发票上把类型改成了「销项」
    result = service.reparse_record(db, batch, record, {str(record.id): {"invoice_type": "OUTPUT"}})

    invoices = result["invoices"]
    assert len(invoices) == 1
    assert invoices[0]["id"] != record.id, "重新解析应当换掉原来那条记录"
    assert invoices[0]["invoice_type"] == "OUTPUT", "人工改过的类型丢了"
