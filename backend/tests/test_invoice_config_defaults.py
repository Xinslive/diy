"""发票配置不预填科目。

原来「自动制证配置」会按科目编码预选 2221 应交税费 / 1001 库存现金，省一次选择。
预填的问题是它替用户做了决定却不说是替他做的：一家公司完全可能把预付款挂在
别处、把销项税额记在 2221 的另一个明细上，而界面上一个已经填好的下拉框不会
提醒任何人去核对 —— 凭证科目错了，要等总账对不上才发现。

所以改成留空。空着是「还没配」，预览时明确报「未配置税额科目 / 未配置结算科目」，
把问题挡在生成凭证之前。
"""
from __future__ import annotations

from app.invoice import service


def test_未配置时科目一律为空(client, auth, book):
    """新账套不该一上来就带着几个已选科目。"""
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        config = service.get_config(db, book.id)
        assert config["taxpayer_id"] == ""
        for key in service.CONFIG_SUBJECT_KEYS:
            assert config[key] is None, f"{key} 又被预填了：{config[key]}"
    finally:
        db.close()


def test_不预填意味着真的不查库(client, auth, book):
    """预填曾经是「按编码查一次科目表」。不预填就不该再有这次查询 ——
    库里没有 2221 的账套与有 2221 的账套，行为应当完全一样。"""
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        # 这个账套里没有 2221 / 1001 也不会有任何差别
        assert service._default_config(db, book.id)["output_tax_subject_id"] is None
    finally:
        db.close()


def test_留空保存就存成空(client, auth, book):
    """传 null 存 null，**不回填**任何默认值。

    回填会让「我把这一项清掉了」变成「怎么又变回 1001 了」—— 用户清空的动作
    被系统悄悄撤销，而界面上看不出任何区别。
    """
    r = client.put(f"/api/books/{book.id}/invoices/config", headers=auth, json={
        "taxpayer_id": "91310000MA1FL0000X",
        "output_tax_subject_id": None,
        "input_credit_subject_id": None,
        "output_debit_subject_id": None,
    })
    assert r.status_code == 200, r.text
    for key in service.CONFIG_SUBJECT_KEYS:
        assert r.json()[key] is None, f"{key} 被偷偷回填了：{r.json()[key]}"
    assert r.json()["taxpayer_id"] == "91310000MA1FL0000X"


def test_选了就是选的(client, auth, book, db):
    from app.models import Subject

    leaf = db.query(Subject).filter(
        Subject.book_id == book.id, Subject.is_leaf.is_(True)).first()
    assert leaf is not None, "账套里得有末级科目可选"

    r = client.put(f"/api/books/{book.id}/invoices/config", headers=auth,
                   json={"output_tax_subject_id": leaf.id})
    assert r.status_code == 200, r.text
    assert r.json()["output_tax_subject_id"] == leaf.id
    # 没传的另外两项仍是空，不被这次保存牵连
    for key in ("input_credit_subject_id", "output_debit_subject_id"):
        assert r.json()[key] is None


def test_配置里没有进项税额科目(client, auth, book):
    """小微企业不做进项抵扣：配置里压根不该有这一项。"""
    r = client.get(f"/api/books/{book.id}/invoices/config", headers=auth)
    assert r.status_code == 200
    assert "input_tax_subject_id" not in r.json()


def test_保存的内容真的落库了(client, auth, book, db):
    """用**另一个会话**回读。

    这是唯一能抓住「只 flush 没 commit」的那条。保存接口当场会用同一个会话把
    值读回来回给你，所以界面上明明显示「已保存」—— 但事务从没提交，刷新一次
    就没了。任何在同一会话里回读的断言都看不出问题，必须换连接。
    """
    from app.models import Subject

    leaf = db.query(Subject).filter(
        Subject.book_id == book.id, Subject.is_leaf.is_(True)).first()
    r = client.put(f"/api/books/{book.id}/invoices/config", headers=auth, json={
        "taxpayer_id": "91310000MA1FL0000X",
        "output_tax_subject_id": leaf.id,
        "input_credit_subject_id": leaf.id,
        "output_debit_subject_id": leaf.id,
    })
    assert r.status_code == 200, r.text

    # 关键：开新会话，绕开任何同一请求内的缓存
    from app.core.db import SessionLocal

    other = SessionLocal()
    try:
        saved = service.get_config(other, book.id)
        assert saved["id"] is not None, "配置行根本没写进库"
        assert saved["taxpayer_id"] == "91310000MA1FL0000X"
        assert saved["output_tax_subject_id"] == leaf.id
    finally:
        other.close()
