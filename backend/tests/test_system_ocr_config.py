"""外部服务凭据接口的权限与保密。

OCR 的 token 从 .env 搬进库之后，它多了一条以前不存在的路：任何能调
``/api/system/ocr`` 的人都能读出或改掉一个能花钱的第三方凭据。所以这里钉的
不是「功能能用」，是**三条不能破的线**：

1. 明文 token 不出现在任何响应里（GET 也好、保存后的返回也好）。
2. 非管理员读不到、改不了 —— 路由上挂了 ``_require_admin``，前端藏按钮不算。
3. 改动留痕，且日志里同样不出现明文。

第 3 条最容易被忽略：审计日志页所有管理员都能翻，明文进日志等于二次泄露 ——
于是「谁把 token 改了」这条记录本身变成了泄露渠道。
"""
from __future__ import annotations

from app.core import system_settings
from app.models import AuditLog


TOKEN = "paddle-secret-token-abcd1234"


def _ocr_get(client, auth):
    return client.get("/api/system/ocr", headers=auth)


def _ocr_put(client, auth, payload):
    return client.put("/api/system/ocr", headers=auth, json=payload)


# ------------------------------------------------------------------ 保密


def test_保存后响应里没有明文token(client, auth):
    r = _ocr_put(client, auth, {"api_token": TOKEN})
    assert r.status_code == 200, r.text
    assert TOKEN not in r.text, "保存接口把 token 原样吐回来了"
    assert r.json()["token_set"] is True
    assert r.json()["token_hint"].endswith("1234"), "只该给末 4 位提示"


def test_读取接口里没有明文token(client, auth):
    _ocr_put(client, auth, {"api_token": TOKEN})
    r = _ocr_get(client, auth)
    assert r.status_code == 200
    assert TOKEN not in r.text, "GET 泄了 token —— 能读出来就会进浏览器历史与网络面板"
    assert "api_token" not in r.json(), "连字段名都不该出现"


def test_能回答配没配但拿不回原值(client, auth, db):
    """管理员要能回答「配没配上」，但不需要能把 token 读出来贴走。"""
    # 用例之间是**提交**过的，前面几条已经把 token 写进库了；不先清掉，
    # 这条断言的「未配置」起点就不成立。
    system_settings.set_value(db, "ocr_api_token", "")
    db.commit()

    assert _ocr_get(client, auth).json()["token_set"] is False
    _ocr_put(client, auth, {"api_token": TOKEN})
    body = _ocr_get(client, auth).json()
    assert body["token_set"] is True
    assert TOKEN not in str(body)


def test_掩码函数不泄露短token():
    assert system_settings.mask_secret(TOKEN).endswith("1234")
    assert TOKEN not in system_settings.mask_secret(TOKEN)
    # 短于 4 位时不该原样返回 —— 那等于没掩码
    assert system_settings.mask_secret("ab") == "…"
    assert system_settings.mask_secret("") == ""


# ------------------------------------------------------------------ 权限


def test_非管理员读不到(client, plain_auth):
    assert _ocr_get(client, plain_auth).status_code == 403


def test_非管理员改不了(client, plain_auth):
    assert _ocr_put(client, plain_auth, {"api_token": TOKEN}).status_code == 403


def test_未登录访问不了(client):
    assert client.get("/api/system/ocr").status_code == 401


# ------------------------------------------------------------------ 语义


def test_空token不会误清已有配置(client, auth):
    """界面上一个没填内容的输入框，一次普通保存不该把别人的 token 抹掉。"""
    _ocr_put(client, auth, {"api_token": TOKEN})
    # 不带 api_token 字段（前端 token 留空时就是这么发的）
    _ocr_put(client, auth, {"api_url": "https://example.com/jobs"})
    assert _ocr_get(client, auth).json()["token_set"] is True, "空值把已有 token 抹了"


def test_显式清除才真的清掉(client, auth):
    _ocr_put(client, auth, {"api_token": TOKEN})
    r = _ocr_put(client, auth, {"clear_token": True})
    assert r.json()["token_set"] is False


def test_库里存的值优先于env(client, auth, db):
    """界面是运维真正会用的入口，让它说了算 —— 否则「改了却没生效」无人能解释。"""
    _ocr_put(client, auth, {"api_url": "https://db.example.com/jobs"})
    assert system_settings.ocr_config(db)["api_url"] == "https://db.example.com/jobs"


def test_没配时回退到env(db):
    from app.core.config import settings

    # 前面的用例往库里写过同一个键，而且用例之间是**提交**过的（不隔离库）。
    # 不先清掉，这一��测的其实是「库优先」，跟这里要验的两件事混在一起。
    system_settings.set_value(db, "ocr_api_url", "")
    db.commit()
    monkey = settings.invoice_ocr_api_url
    settings.invoice_ocr_api_url = "https://env.example.com/jobs"
    try:
        assert system_settings.ocr_config(db)["api_url"] == "https://env.example.com/jobs"
    finally:
        settings.invoice_ocr_api_url = monkey


# ------------------------------------------------------------------ 留痕


def test_改动留下审计且不含明文(client, auth, db):
    _ocr_put(client, auth, {"api_token": TOKEN})
    db.expire_all()
    rows = db.query(AuditLog).filter(
        AuditLog.action == "SYSTEM_OCR_CONFIG_UPDATE").all()
    assert rows, "改了凭据却没留痕"
    assert TOKEN not in str([{"detail": r.detail, "before": r.before, "after": r.after} for r in rows]), \
        "审计里出现明文 token —— 操作日志页所有管理员都能翻"


def test_审计快照记的是改完之后(client, auth, db):
    """before/after 必须真的不同。

    session 是 autoflush=False，写完不 flush 就读，读到的是**改之前**的值 ——
    于是日志里留下 before == after 的假变更。「谁把 token 换成了什么」是这次
    改动唯一可查的线索，记成没变化等于把线索毁掉。
    """
    system_settings.set_value(db, "ocr_api_token", "")
    db.commit()
    _ocr_put(client, auth, {"api_token": TOKEN})
    db.expire_all()
    row = (db.query(AuditLog).filter(AuditLog.action == "SYSTEM_OCR_CONFIG_UPDATE")
           .order_by(AuditLog.id.desc()).first())
    assert row.after["token"] != row.before["token"], \
        f"快照前后一样：before={row.before} after={row.after}"
    assert row.after["token"].endswith("1234")
