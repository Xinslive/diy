"""审查修复：插件与宿主之间的三道边界（H-04 / H-05 / L-25）。

**H-04（中）：插件 hook 抛 ``BaseException`` 会穿透结账事务的提交后回调。**

``SystemExit`` / ``KeyboardInterrupt`` / ``GeneratorExit`` 派生自
``BaseException``，**不被 ``except Exception`` 捕获** —— 它们会一路穿出
``Session.commit()``。后果不是「插件出错」而是：结账事务**已经提交**
（原子性没破），调用方却拿到 500 —— 用户看到「结账失败」，实际账已经锁了，
重试还会撞「已结账」。

**H-05（中）：插件导入批次的「用户确认」不是安全边界。**

``ctx.imports`` 以前一次性给出全套能力（含 ``confirm``），插件可以在**一次
请求内**完成「预览 → 确认 → 提交」，用户那次独立的点击从来没有发生过。
现在：**插件面不含 ``confirm``**，确认必须走宿主路由那一次独立的 HTTP 请求；
提交时还要核对「确认」与「提交」来自两个不同的请求。

**L-25（低）：worker 退出后旧行要让位，重启不必等租约过期。**
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select, text

from app.core.db import SessionLocal
from app.models import AuditLog
from app.models.plugin import PluginWorkerRuntime
from app.plugins import hooks as hooks_mod
from app.plugins.public_api import context_for
from tests.plugin_fixtures import plugin_env


# --------------------------------------------------------------------- H-04


def test_插件抛SystemExit时结账接口不返回5xx(plugin_env, client, auth, admin_user, book):
    """hook 抛 ``SystemExit`` ⇒ 结账**成功**（事务已提交），异常进审计。

    改前：``except Exception`` 接不住 ``SystemExit`` ⇒ 它穿出
    ``Session.commit()`` ⇒ 调用方拿到 500。
    """
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    from app.plugins.installer import install

    install(raw, admin_user)
    runtime = restart()
    pid = manifest["id"]
    client.post(f"/api/system/plugins/{pid}/users/{admin_user.id}", headers=auth)

    def aborting(event):
        raise SystemExit("插件试图终止进程")

    runtime.hooks[pid] = {"on_period_close": aborting}

    with SessionLocal.begin() as db:
        hooks_mod.period_closed(db, user_id=admin_user.id, book_id=book.id, period_id=1)

    # 事务**没有**被异常带崩：提交后的回调把异常吞掉了。
    # （会话要在 with 块内用完 —— 在块外再查一次会把事务留在「idle in
    #   transaction」，那个事务一直占着锁，后面清库时会一直等。）
    with SessionLocal() as db:
        assert db.scalar(text("SELECT 1")) == 1
        rows = db.scalars(select(AuditLog).where(
            AuditLog.action == "plugin.hook", AuditLog.entity_id == pid)).all()
        assert rows, "插件 hook 的失败必须留下审计"
        assert rows[-1].after.get("result") == "failed", rows[-1].after


def test_结账接口在插件抛SystemExit时仍然返回成功(plugin_env, client, auth, plain_auth, admin_user, plain_user, book):
    """**端到端**：结账接口不返回 5xx。

    hook 是在 SQLAlchemy 的 ``after_commit`` 事件里跑的 —— 事务已经提交，
    所以这一层的异常只该被记下来，不该变成调用方的 500。
    """
    build, restart = plugin_env
    raw, manifest, _, _ = build()
    from app.plugins.installer import install
    from app.models.system import UserBook

    install(raw, admin_user)
    runtime = restart()
    pid = manifest["id"]
    client.post(f"/api/system/plugins/{pid}/users/{admin_user.id}", headers=auth)
    client.put(f"/api/system/plugins/{pid}/users", headers=auth,
               json={"user_ids": [plain_user.id]})
    with SessionLocal.begin() as db:
        db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="OWNER"))

    runtime.hooks[pid] = {"on_period_close": lambda event: (_ for _ in ()).throw(SystemExit("boom"))}

    with SessionLocal() as db:
        period = db.scalar(text(
            "SELECT id FROM period WHERE book_id = :book ORDER BY ord LIMIT 1"),
            {"book": book.id})
    response = client.post(f"/api/books/{book.id}/periods/{period}/close", headers=plain_auth)
    assert response.status_code != 500, response.text
    assert response.status_code < 500, response.text


# --------------------------------------------------------------------- H-05


def test_插件拿到的ctx上没有确认能力():
    """``ctx.imports`` 上**没有** ``confirm``。

    确认必须走一次独立的 HTTP 请求（宿主路由），不能由插件在同一次请求里
    自己完成。
    """
    manifest = {"id": "com.test.importer", "permissions": ["import:rw", "voucher:write"],
                "tables": []}
    plugin_ctx = context_for(manifest, plugin=True)
    assert not hasattr(plugin_ctx.imports, "confirm"), \
        "插件面上不能有确认能力"
    # 别的能力还在 —— 别把插件可用性一起削掉。
    for name in ("create", "read", "preview", "edit", "submit", "cancel", "limits"):
        assert hasattr(plugin_ctx.imports, name), name
    # 宿主自己那条路由拿到的上下文仍然有 confirm。
    assert hasattr(context_for(manifest).imports, "confirm")


#: 插件的解析路由：**预览只能由插件的解析产生**（宿主那条 PUT /preview 是
#: 「改草稿」，没有预览时它会拒绝）。
PARSE_CODE = b'''def register(registrar, ctx):
    def parse(ident: str, payload: dict):
        return ctx.imports.preview(ident, payload["vouchers"], revision=payload["revision"])
    registrar.post("/parse/{ident}", parse)
'''


def _install_importer(plugin_env, client, auth, admin_user, plain_user, book, db,
                      content: bytes):
    """装一个带导入能力的插件，并把它授权给 plain_user。"""
    from app.plugins.installer import install
    from app.models.system import UserBook

    build, restart = plugin_env
    raw, manifest, _, _ = build(
        files={"backend/main.py": PARSE_CODE,
               "frontend/index.js": b"export default host => ({render: () => host.h('div')});"},
        edit=lambda m: m.update(permissions=["book:read", "import:rw", "voucher:write"]))
    install(raw, admin_user)
    restart()
    pid = manifest["id"]
    client.put(f"/api/system/plugins/{pid}/users", headers=auth,
               json={"user_ids": [plain_user.id]})
    with SessionLocal.begin() as session:
        session.add(UserBook(user_id=plain_user.id, book_id=book.id, role="OWNER"))
    return pid


def _draft(db, book_id: int) -> dict:
    leaves = db.scalars(text(
        "SELECT id FROM subject WHERE book_id = :book AND year = 2026 AND active "
        "AND is_leaf ORDER BY code LIMIT 3"), {"book": book_id}).all()
    return {"voucher_date": "2026-03-02", "remark": "文件转凭证", "entries": [
        {"subject_id": leaves[0], "direction": "DEBIT", "amount": "12.50", "summary": "第2行"},
        {"subject_id": leaves[1], "direction": "CREDIT", "amount": "12.50", "summary": "第2行"}]}


def test_没有经过独立确认请求就提交会被拒(plugin_env, client, auth, plain_auth,
                                        admin_user, plain_user, book, db):
    """绕过确认直接提交 ⇒ 拒绝。

    宿主级校验（权限、批次归属、状态机）本来就拦得住「没确认就提交」；
    这里钉的是**更靠前**的那一层：确认必须是一次**独立的请求**。
    """
    pid = _install_importer(plugin_env, client, auth, admin_user, plain_user, book, db,
                             b"account,amount\n1001,12.50")
    base = f"/api/plugins/{pid}/_host/imports"
    uploaded = client.post(base, params={"book_id": book.id}, headers=plain_auth,
                           files=[("files", ("交易.csv", b"account,amount\n1001,12.50", "text/csv"))])
    assert uploaded.status_code == 200, uploaded.text
    batch = uploaded.json()

    parsed = client.post(f"/api/plugins/{pid}/parse/{batch['id']}", headers=plain_auth,
                         json={"revision": batch["revision"], "vouchers": [_draft(db, book.id)]})
    assert parsed.status_code == 200, parsed.text
    ready = parsed.json()
    assert ready["status"] == "PREVIEW_READY", ready

    # **没有确认**就提交 ⇒ 409（不是 500，也不是「成功」）。
    submit = client.post(base + f"/{ready['id']}/submit", headers=plain_auth,
                          json={"revision": ready["revision"]})
    assert submit.status_code == 409, submit.text
    assert "确认" in submit.text, submit.text


def test_确认与提交来自同一个请求会被拒(plugin_env, client, auth, plain_auth,
                                        admin_user, plain_user, book, db):
    """「确认」与「提交」必须是**两个不同的请求**。

    宿主路由把当次的请求号记在批次上；提交时若请求号相同，说明这一次请求
    里既确认又提交了 —— 那不是「用户点了一次确认」。
    """
    pid = _install_importer(plugin_env, client, auth, admin_user, plain_user, book, db,
                             b"account,amount\n1002,34.00")
    base = f"/api/plugins/{pid}/_host/imports"
    uploaded = client.post(base, params={"book_id": book.id}, headers=plain_auth,
                           files=[("files", ("交易.csv", b"account,amount\n1002,34.00", "text/csv"))])
    assert uploaded.status_code == 200, uploaded.text
    batch = uploaded.json()

    parsed = client.post(f"/api/plugins/{pid}/parse/{batch['id']}", headers=plain_auth,
                         json={"revision": batch["revision"], "vouchers": [_draft(db, book.id)]})
    assert parsed.status_code == 200, parsed.text
    ready = parsed.json()

    # **同一个请求号**（X-Request-ID）下先确认再提交。
    same = {"X-Request-ID": "same-request-0001"}
    confirmed = client.post(base + f"/{ready['id']}/confirm", headers={**plain_auth, **same},
                            json={"revision": ready["revision"]})
    assert confirmed.status_code == 200, confirmed.text
    submitted = client.post(base + f"/{ready['id']}/submit", headers={**plain_auth, **same},
                            json={"revision": confirmed.json()["revision"]})
    assert submitted.status_code == 409, submitted.text
    assert "独立" in submitted.text or "两次" in submitted.text, submitted.text

    # 换一次请求（新的请求号）就能提交 —— 确认与提交确实是两次独立请求。
    ok = client.post(base + f"/{ready['id']}/submit", headers=plain_auth,
                     json={"revision": confirmed.json()["revision"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "SUBMITTED"


# --------------------------------------------------------------------- L-25


def test_worker退出后旧行被删除_重启不必等租约过期(monkeypatch):
    """worker 退出 ⇒ **它自己的行被删掉**。

    以前是把租约改成过去（行还在表里）。僵尸行会让「本代次活着的 worker
    数」对不上 ⇒ 屏障关闭 ⇒ 所有插件接口 503。
    """
    import os
    import uuid

    from app.plugins.workers import WorkerRuntime

    monkeypatch.setenv("CWQ_PLUGIN_GENERATION", uuid.uuid4().hex)
    monkeypatch.setenv("CWQ_PLUGIN_STARTED_AT", str(int(time.time() * 1e9)))
    monkeypatch.setenv("CWQ_PLUGIN_EXPECTED_WORKERS", "1")

    runtime = WorkerRuntime()
    runtime.join()
    with SessionLocal() as db:
        before = db.scalars(select(PluginWorkerRuntime).where(
            PluginWorkerRuntime.worker_id == runtime.worker_id)).all()
        assert before, "worker 应当已经登记了自己的行"

    runtime.close()

    with SessionLocal() as db:
        after = db.scalars(select(PluginWorkerRuntime).where(
            PluginWorkerRuntime.worker_id == runtime.worker_id)).all()
        assert after == [], f"worker 退出后自己的行应当被删掉：{after}"
