"""周期记账插件的真实宿主端到端测试。

不打桩：插件被宿主完整装载（签名校验 → 迁移 → 注册 → 网关鉴权），请求走真实
路由，凭证真的进账。这里守的是几条不变量：

1. **同月不重复**：同一条规则同一月份执行两次，只生成一张凭证。
2. **借贷平衡**：生成的分录借贷相等，宿主校验必须放行。
3. **账套隔离**：规则存在各自账套的表里，A 账套的执行不会动到 B 账套。
4. **金额与日期**：金额取规则固定值，日期落在规则声明的「每月第 N 天」。
5. **只读账套不能记账**：VIEWER 触发保存与执行都要被宿主挡下。
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.accounting import Entry, Voucher
from app.models.book import Period
from app.models.system import UserBook

from tests.plugin_fixtures import plugin_env  # noqa: F401  供插件环境使用

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "com.cwq.periodic"
PID = "com.cwq.periodic"


def _files():
    """插件源目录里的真实文件，签名打包用。"""
    files = {}
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if not path.is_file() or path.name in {".DS_Store", "plugin.json"}:
            continue
        files[path.relative_to(PLUGIN_DIR).as_posix()] = path.read_bytes()
    return files


@pytest.fixture
def periodic(plugin_env, client, auth, admin_user, book_id):
    """把**真实的插件源目录**装进宿主并重启。

    私钥复��自 plugin_env 的同一把：``build`` 会用 plugin_env 生成的可信公钥去
    签，而可信公钥正是那把私钥的公钥 —— 所以必须让 package() 用同一把私钥，
    否则装包阶段会以 UNTRUSTED_SIGNATURE 被拒。
    """
    build, restart = plugin_env
    raw, manifest, _keys, _private = build(
        plugin_id=PID,
        version="1.0.0",
        files=_files(),
        tables=False,
        edit=_manifest_edit,
    )
    installed = client.post("/api/system/plugins/install",
                            files={"file": ("periodic.cwp", raw)}, headers=auth)
    assert installed.status_code == 200, installed.text
    restart()
    granted = client.put(f"/api/system/plugins/{PID}/users", headers=auth,
                         json={"user_ids": [admin_user.id]})
    assert granted.status_code == 200, granted.text
    return f"/api/plugins/{PID}"


def _manifest_edit(manifest):
    """用插件自己的 manifest，只把 permissions 换成测试需要的最小集。

    直接读源 manifest 会把菜单、图标、hook 声明一并带进来 —— 那些正是要验的
    部分，不能为了省事替换掉。
    """
    source = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    manifest.update({
        "name": source["name"],
        "description": source["description"],
        "permissions": source["permissions"],
        "hooks": source["hooks"],
        "tasks": source.get("tasks", []),
        "tables": source["tables"],
        "contributes": source["contributes"],
        "api_version": source["api_version"],
    })


def _rule(client, path, headers, book, **overrides):
    payload = {
        "book_id": book,
        "name": overrides.pop("name", "办公室租金"),
        "day_of_month": overrides.pop("day_of_month", 5),
        "amount": overrides.pop("amount", "8000.00"),
        "entries": overrides.pop("entries", [
            {"code": "5602", "direction": "DEBIT", "summary": "房租"},
            {"code": "1002", "direction": "CREDIT", "summary": "付租"},
        ]),
    }
    payload.update(overrides)
    return client.post(path + "/rules", json=payload, headers=headers)


def _vouchers_of(db, book):
    return list(db.scalars(select(Voucher).where(Voucher.book_id == book)
                           .order_by(Voucher.id)))


def test_rule_roundtrip_and_duplicate_month_is_idempotent(db, periodic, client, auth, book, book_id):
    """同一条规则同一个月跑两次，只应有一张凭证。"""
    headers = auth
    saved = _rule(client, periodic, headers, book.id)
    assert saved.status_code == 200, saved.text
    rule = saved.json()
    assert rule["amount"] == "8000.00"
    assert rule["day_of_month"] == 5

    first = client.post(periodic + "/run", json={"book_id": book.id}, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert len(body["created"]) == 1, body
    assert body["failed"] == []
    voucher_no = body["created"][0]["voucher_no"]
    assert voucher_no

    second = client.post(periodic + "/run", json={"book_id": book.id}, headers=headers)
    assert second.status_code == 200, second.text
    assert second.json()["created"] == []
    assert any(item.get("done") for item in second.json()["skipped"])

    rows = _vouchers_of(db, book.id)
    assert len(rows) == 1, [v.voucher_no for v in rows]
    assert rows[0].voucher_no == voucher_no


def test_generated_voucher_is_balanced_and_dated_by_rule(db, periodic, client, auth, book):
    """生成的分录借贷相等、金额取规则值、日期落在规则声明的那天。"""
    _rule(client, periodic, auth, book.id, amount="1234.56")
    body = client.post(periodic + "/run", json={"book_id": book.id, "year": 2026, "month": 3},
                       headers=auth).json()
    assert len(body["created"]) == 1, body

    voucher = _vouchers_of(db, book.id)[0]
    assert voucher.voucher_date.isoformat() == "2026-03-05"
    entries = list(db.scalars(select(Entry).where(Entry.voucher_id == voucher.id)))
    debit = sum(Decimal(e.amount) for e in entries if e.direction == "DEBIT")
    credit = sum(Decimal(e.amount) for e in entries if e.direction == "CREDIT")
    assert debit == credit == Decimal("1234.56")
    assert len(entries) == 2


def test_day_31_falls_back_to_month_end(db, periodic, client, auth, book):
    """31 号在小月应落到当月最后一天，而不是跳过这个月。"""
    _rule(client, periodic, auth, book.id, day_of_month=31)
    body = client.post(periodic + "/run", json={"book_id": book.id, "year": 2026, "month": 2},
                       headers=auth).json()
    assert len(body["created"]) == 1, body
    assert body["created"][0]["voucher_date"] == "2026-02-28"


def test_rules_are_book_scoped(db, periodic, client, auth, book, admin_user):
    """另一个账套看不到、也记不到本账套的规则。"""
    from app.models.book import Book
    from app.seed.chart_of_accounts import install_chart
    from app.accounting.periods import create_periods
    import uuid

    other = Book(company_name=f"另一公司{uuid.uuid4().hex[:6]}", name=f"另一账套{uuid.uuid4().hex[:6]}",
                 start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    create_periods(db, other)
    install_chart(db, other.id)
    db.add(UserBook(user_id=admin_user.id, book_id=other.id, role="OWNER"))
    db.commit()

    _rule(client, periodic, auth, book.id, name="租金A")
    assert client.post(periodic + "/run", json={"book_id": other.id}, headers=auth).json()["created"] == []
    assert _vouchers_of(db, other.id) == []
    names = [r["name"] for r in client.get(periodic + "/rules", params={"book_id": other.id},
                                           headers=auth).json()["rules"]]
    assert names == []


def test_viewer_cannot_write(plugin_env, client, auth, plain_auth, plain_user, book, db,
                             periodic_admin_book):
    """只读账套不能新增规则，也不能执行记账。"""
    path, book_id_ = periodic_admin_book
    link = UserBook(user_id=plain_user.id, book_id=book_id_, role="VIEWER")
    db.add(link)
    db.commit()
    assert _rule(client, path, plain_auth, book_id_).status_code == 403
    assert client.post(path + "/run", json={"book_id": book_id_}, headers=plain_auth).status_code == 403


@pytest.fixture
def periodic_admin_book(installed, client, auth, book):
    return installed, book.id


def test_close_period_hook_backfills_missing_months(db, periodic, client, auth, book, admin_user):
    """结账后，漏掉的月份由钩子补上。"""
    from app.accounting.periods import close_period

    _rule(client, periodic, auth, book.id)
    assert client.post(periodic + "/run", json={"book_id": book.id, "year": 2026, "month": 1},
                       headers=auth).json()["created"]
    assert client.post(periodic + "/run", json={"book_id": book.id, "year": 2026, "month": 2},
                       headers=auth).json()["created"]

    # 制造一个缺口：把 2 月的台账撤掉（模拟「2 月漏记、3 月已记」）
    rows = client.get(periodic + "/rules", params={"book_id": book.id}, headers=auth).json()["runs"]
    feb = [row for row in rows if row["period_label"] == "2026年2月"]
    assert feb, rows
    client.post(periodic + f"/runs/{feb[0]['id']}/undo", json={"book_id": book.id}, headers=auth)
    assert client.post(periodic + "/run", json={"book_id": book.id, "year": 2026, "month": 3},
                       headers=auth).json()["created"]

    period = db.scalar(select(Period).where(Period.book_id == book.id, Period.year == 2026,
                                            Period.month == 1))
    close_period(db, period, admin_user, auto_post=False)

    labels = [row["period_label"] for row in
              client.get(periodic + "/rules", params={"book_id": book.id}, headers=auth).json()["runs"]]
    assert "2026年2月" in labels, labels


def test_scheduled_task_and_manual_do_not_double_post(db, periodic, client, auth, book):
    """定时任务与手动执行指向同一个月时，只能记一次账。

    这是用户明确提的要求：**限制一个任务每个月的执行成功次数，不能重复记账**。
    先手动、再让定时任务跑一遍（反过来也测），凭证数都必须停在 1。
    """
    from app.scheduler import service as scheduler

    _rule(client, periodic, auth, book.id)
    assert client.post(periodic + "/run", json={"book_id": book.id, "year": 2026, "month": 3},
                       headers=auth).json()["created"]

    runtime = client.app.state.plugin_runtime
    handler = runtime.tasks[PID]["monthly_posting"][1]
    handler(_TaskContext())

    assert len(_vouchers_of(db, book.id)) == 1, [v.voucher_no for v in _vouchers_of(db, book.id)]

    # 反向顺序：撤销台账后先跑定时任务，再手动执行，同样只能记一次。
    rows = client.get(periodic + "/rules", params={"book_id": book.id}, headers=auth).json()["runs"]
    client.post(periodic + f"/runs/{rows[0]['id']}/undo", json={"book_id": book.id}, headers=auth)
    db.expire_all()
    for voucher in _vouchers_of(db, book.id):
        db.delete(voucher)
    db.commit()

    handler(_TaskContext())
    assert len(_vouchers_of(db, book.id)) == 1
    again = client.post(periodic + "/run", json={"book_id": book.id, "year": 2026, "month": 3},
                        headers=auth).json()
    assert again["created"] == [], again
    assert any(item.get("done") for item in again["skipped"])
    assert len(_vouchers_of(db, book.id)) == 1


class _TaskContext:
    """宿主传给定时任务的上下文（handler 只用到 plugin_id / task_name）。"""
    plugin_id = PID
    task_name = "monthly_posting"
    run_id = "test-run"
    scheduled = True


def test_scheduled_task_respects_start_label(db, periodic, client, auth, book):
    """定时任务也必须遵守起止年月，不能凭空补出起始之前的月份。"""
    from app.scheduler import service as scheduler  # noqa: F401

    _rule(client, periodic, auth, book.id, start_label="2999-01")
    runtime = client.app.state.plugin_runtime
    runtime.tasks[PID]["monthly_posting"][1](_TaskContext())
    assert _vouchers_of(db, book.id) == []


def test_periodic_task_is_declared_and_registered(db, periodic, client, auth):
    """manifest 声明的每个定时任务都必须注册处理函数，否则插件装载失败。"""
    runtime = client.app.state.plugin_runtime
    assert PID in runtime.tasks
    assert "monthly_posting" in runtime.tasks[PID]
    schedule = runtime.tasks[PID]["monthly_posting"][0]
    assert schedule["type"] == "cron"
    assert schedule["expression"] == "17 7 * * *"


def test_start_label_limits_backfill(db, periodic, client, auth, book):
    """起始年月之前不生成。"""
    _rule(client, periodic, auth, book.id, start_label="2026-03")
    body = client.post(periodic + "/preview", json={"book_id": book.id, "year": 2026, "month": 1},
                       headers=auth)
    assert body.status_code == 200, body.text
    assert body.json()["plans"] == []
    assert body.json()["problems"], body.json()
