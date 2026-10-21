"""账务内核的护栏：结转科目语义、空月份不许结账、断号不许走进死路。

这一组对应审查报告「账务内核」域的三条 P1/P2：

* **P1-1** ``carry_forward`` 没有任何语义校验 —— 资产科目被结转损益冲进本年利润
* **P1-2** 本年利润一旦被标成结转科目，该期间永远结不了账
  （两处「结转科目集合」口径不一致）
* **P2-1** ``close_period`` 的「空期间不许结账」只管被结的那一期 ——
  级联把更早的空月份静默封进历史
* **P1-3** 「已记账凭证 + 断号」是 UI 死路

**为什么这些要在服务端钉死。** 科目表导入按外部表的「类型」列自动置
``carry_forward``，那张表的「类型」列写错一个词，资产科目就被标成结转科目；
随后任何一次月末结转都会把它的余额整体搬进「本年利润」—— 钱凭空消失，
而**借贷仍然平衡**，试算平衡与系统自检全都发现不了。所以：

* 写侧（接口）拒绝把非损益类科目置为结转科目；
* 读侧（结转与结平校验）**只认真正的损益类科目** —— 库里已经存在的脏标记
  也不会被结走。

两处都要有：写侧拦新数据，读侧兜住修复之前就已经躺在库里的行。
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.accounting.periods import (carry_forward_profit, close_period,
                                    carry_forward_subjects)
from app.accounting.vouchers import post_vouchers
from app.ledger.engine import LedgerEngine
from app.models import Book, Period, Subject, UserBook, Voucher


# --------------------------------------------------------------------- 夹具


@pytest.fixture
def other_book(db, admin_user):
    """另一个账套 —— 跨账套那一侧。"""
    from app.accounting.periods import create_periods
    from app.seed.chart_of_accounts import install_chart

    b = Book(company_name="别家公司", name=f"别的账套{uuid.uuid4().hex[:6]}",
             start_year=2026, start_month=1)
    db.add(b)
    db.flush()
    create_periods(db, b)
    install_chart(db, b.id)
    db.add(UserBook(user_id=admin_user.id, book_id=b.id, role="OWNER"))
    db.commit()
    db.refresh(b)
    return b


def _closing(db, book, subject_id, period) -> Decimal:
    eng = LedgerEngine(db, book.id, include_unposted=True)
    return eng.figures(subject_id, period.ord).closing


# --------------------------------------------------------------------- P1-1 写侧


def test_资产科目不能被标成结转科目(client, auth, book):
    """写侧第一道闸：非损益类科目不许置为「结转科目」。

    标错了的后果不是「多一个开关」：月末结转会把它的余额整体搬进本年利润。
    """
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": "1905", "name": "库存材料", "category": "ASSET",
        "direction": "DEBIT", "carry_forward": True,
    })
    assert r.status_code == 422, f"资产科目被标成结转科目：HTTP {r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert "损益类" in detail, detail
    # 人话里要说清楚「只有损益类可以」
    assert "1905" in detail, detail


def test_改科目时不能把非损益类科目标成结转科目(client, auth, book, subjects):
    """PATCH 这条路同样要挡 —— 接口能被直接调，界面不给选不算防线。"""
    r = client.patch(f"/api/books/{book.id}/subjects/{subjects['1001'].id}",
                     headers=auth, json={"carry_forward": True})
    assert r.status_code == 422, f"资产科目被标成结转科目：HTTP {r.status_code} {r.text}"
    assert "损益类" in r.json()["detail"], r.json()


def test_损益类科目可以标成结转科目(client, auth, book):
    """闸门不能把正常用法也堵死 —— 损益类科目照旧可以勾。"""
    r = client.post(f"/api/books/{book.id}/subjects", headers=auth, json={
        "code": "6051", "name": "其他业务收入", "category": "PROFIT",
        "direction": "CREDIT", "carry_forward": True,
    })
    assert r.status_code == 201, r.text


def test_把损益类改成非损益类时不能留着结转标记(client, auth, book, subjects):
    """反方向也要挡：类别改成资产、结转标记还留着 = 同样的违规。"""
    s = subjects["5001"]
    r = client.patch(f"/api/books/{book.id}/subjects/{s.id}",
                     headers=auth, json={"category": "ASSET"})
    assert r.status_code == 422, f"改类别后结转标记还留着：HTTP {r.status_code} {r.text}"


# --------------------------------------------------------------------- P1-1 读侧


def test_读侧不把资产科目结进本年利润(db, book, subjects, periods, admin_user,
                                   make_voucher):
    """**库里已经有的脏标记**也要挡住（写侧拦不住存量）。

    这里绕过接口直接改库 —— 那正是「修复之前就躺在库里」的那种行。
    """
    make_voucher("2026-01-10", dr=[("1001", 2000, "现金收入")],
                 cr=[("5001", 2000, "现金收入")])
    asset = db.get(Subject, subjects["1001"].id)
    asset.carry_forward = True          # 脏标记：资产科目被勾成结转科目
    db.commit()

    carry_forward_profit(db, book.id, periods[1], admin_user)

    assert _closing(db, book, subjects["1001"].id, periods[1]) == Decimal("2000.00"), (
        "资产科目的余额被结走了 —— 钱凭空消失，而借贷仍然平衡")
    # 本年利润里只有收入那 2000（贷方），没有把资产当成费用
    assert _closing(db, book, subjects["3103"].id, periods[1]) == Decimal("-2000.00"), (
        "资产余额被当成损益结进了本年利润")


def test_结转科目集合只认损益类(db, book, subjects):
    """共享 helper 的判据：carry_forward + 末级 + **损益类**，且排除本年利润。"""
    asset = db.get(Subject, subjects["1001"].id)
    asset.carry_forward = True
    profit = db.get(Subject, subjects["3103"].id)
    profit.carry_forward = True
    db.commit()

    # 年度是必填：科目表每个年度各有一份（迁移 0032）
    codes = {s.code for s in carry_forward_subjects(db, book.id, book.start_year)}
    assert "1001" not in codes, "资产科目进了结转集合"
    assert "3103" not in codes, "本年利润进了结转集合（它是结转的目的科目）"
    assert "5001" in codes, "损益科目不在结转集合里"


# --------------------------------------------------------------------- P1-2


def test_本年利润被标成结转科目后该期间仍能结账(db, book, subjects, periods,
                                             admin_user, make_voucher):
    """**这一条以前是死局**：本年利润被标成结转科目 → 结账永远被拒。

    原因：结转集合排除了本年利润（它是结转的**目的**科目），而结平校验
    却把它算成「待结转的损益科目」—— 两处口径不一致，差它一个余额。
    """
    make_voucher("2026-01-10", dr=[("1001", 1000, "收入")],
                 cr=[("5001", 1000, "收入")])
    profit = db.get(Subject, subjects["3103"].id)
    profit.carry_forward = True       # 脏标记：本年利润被勾成结转科目
    db.commit()

    carry_forward_profit(db, book.id, periods[1], admin_user)
    # 关键：结账**必须能过**
    r = close_period(db, periods[1], admin_user)
    assert r["period"] == periods[1].code


# --------------------------------------------------------------------- P2-1


def test_链上有空月份时结账被拒(db, book, periods, admin_user, make_voucher):
    """级联不许把更早的空月份**静默**封进历史（审查 P2-1）。

    以前那道闸只看被结的那一期：1~11 月一张凭证都没有、只有 12 月有，
    点「结账 12 月」会把 11 个空月份一起锁上 —— 补录就得反结账，
    而反结账会连带把后面已结好的月份全部退回。
    """
    make_voucher("2026-03-05", dr=[("1002", 10, "x")], cr=[("1001", 10, "x")])
    with pytest.raises(ValueError) as ei:
        close_period(db, periods[3], admin_user)
    db.rollback()
    msg = str(ei.value)
    assert "2026-01" in msg and "2026-02" in msg, msg
    assert [db.get(Period, periods[m].id).locked for m in (1, 2, 3)] == [False] * 3


# --------------------------------------------------------------------- P1-3 断号


def _post(db, voucher_id):
    db.execute(update(Voucher).where(Voucher.id == voucher_id)
               .values(status="POSTED", posted_by="测试"))
    db.commit()


def test_删除会留下补不上的断号时被拦下(client, auth, db, book, subjects,
                                       make_voucher):
    """「已记账凭证 + 断号」是死路，所以**别让它被创建出来**（审查 P1-3）。

    记0001（未记账） 记0002（未记账） 记0003（**已记账**），
    删掉 记0002 → 洞在 0002，而整理只给未记账的凭证重新发号、补不上它。
    """
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")])
    _post(db, v3.id)

    r = client.delete(f"/api/books/{book.id}/vouchers/{v2.id}", headers=auth)
    assert r.status_code == 400, f"补不上的断号被放行了：HTTP {r.status_code} {r.text}"
    detail = r.json()["detail"]
    assert "反结账" in detail, detail
    db.expire_all()
    assert db.get(Voucher, v2.id) is not None, "被拒之后凭证不该被删掉"


def test_删掉之后号段仍修得回来时照旧允许(client, auth, db, book, subjects,
                                        make_voucher):
    """闸门不能变成「什么都不许删」。

    记0001（未记账） 记0002（未记账） 记0003（已记账）：删掉 记0001 之后
    剩下 0002、0003，整理能把 0002 压成 0001 —— 洞补得上，放行。
    """
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")])
    _post(db, v3.id)

    r = client.delete(f"/api/books/{book.id}/vouchers/{v1.id}", headers=auth)
    assert r.status_code == 200, f"修得回来的断号也被拦了：HTTP {r.status_code} {r.text}"


def test_断号提示指向真正走得通的路(db, book, subjects, periods, admin_user,
                                  make_voucher):
    """报错不能再让人去点那个无效的动作（审查 P1-3）。

    以前一律写「请先到「整理凭证」把号排好再来」，而有一类状态整理凭证
    对它无能为力 —— 用户按报错去点，整理改 0 张，还是这个错。
    """
    from app.accounting import organize as svc_organize

    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")])
    _post(db, v3.id)
    db.delete(v2)
    db.commit()

    with pytest.raises(ValueError) as ei:
        svc_organize.assert_tidy(db, [periods[1]])
    db.rollback()
    msg = str(ei.value)
    assert "整理凭证" in msg, msg
    # 必须说清「整理凭证补不上」这件事，并给出真正的出路
    assert "反结账" in msg, msg
