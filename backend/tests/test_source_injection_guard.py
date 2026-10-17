"""客户端不能伪造 CARRY_FORWARD 来绕过两条不变量（审计 P1-02）。

``VoucherIn.source`` 原来和 MANUAL/IMPORT/INVOICE 一起写在 Literal 里，
于是**任何有账套写权限的人**都能直接调接口把一张自己的凭证标成「系统结转」。
这个标记不是装饰，它是两处例外的钥匙：

* ``create_voucher`` 过去按 ``source in SYSTEM_SOURCES`` 放开「停用科目也能记账」
  这道闸（``_validate_entries`` 的 ``Subject.active.is_(True)`` 过滤）。系统的
  结转搬的是**已经存在**的余额，不该被这条闸拦住（审计 P2-08 的本意）；
* ``periods._drop_carry_forward`` 删本期结转凭证时**不看状态**，而正常删除路径
  ``vouchers.delete_voucher`` 明确拒绝删已记账凭证，数据库触发器
  ``fn_posted_voucher_guard`` 也专门给 CARRY_FORWARD 开了口子。

两条叠起来是一条完整的越权链：

    自己造一张 source=CARRY_FORWARD 的凭证 → 记账 → 点「重新结转损益」
    → 自己的**已记账**凭证被悄悄删掉，库里没有任何提示
    （本系统凭证号可复用，删掉只能靠审计快照追溯）

这里钉三件事，每件都断言**可观察的后果**，不是「某个分支被走到了」：

1. 客户端提交 CARRY_FORWARD 压根进不来（422），库里也不会多出这种凭证。
2. 豁免（停用科目可记账）只认 ``internal=True``，source 说了不算 ——
   服务层直接断言「标着 CARRY_FORWARD 但没有 internal 的凭证碰到停用科目被拒」。
3. 库里就算躺着一张伪造的结转凭证（修复之前造的、或内部代码误造的），
   「重新结转」也**不会删它**：操作 fail closed，一张都不删。
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.accounting import periods as svc
from app.accounting.vouchers import create_voucher
from app.models import Period, Voucher

CARRY = "CARRY_FORWARD"


def _period(db, book, month: int) -> Period:
    return db.scalar(select(Period).where(
        Period.book_id == book.id, Period.month == month))


def _plain_entry(subjects, amount="100.00"):
    """一张手工凭证的正常分录（借贷平衡、科目都是启用的）。"""
    return [
        {"subject_id": subjects["1002"].id, "direction": "DEBIT",
         "amount": amount, "summary": "x"},
        {"subject_id": subjects["5001"].id, "direction": "CREDIT",
         "amount": amount, "summary": "x"},
    ]


def _forge(db, book, admin_user, subjects, *, posted=False, remark="用户自己写的备注"):
    """造一张**伪造**的 CARRY_FORWARD 凭证。

    修复之后客户端已经造不出这种凭证了（下面第一条用例就在钉这件事），
    但库里可能已经有 —— 修复之前造的就留在那儿了。所以这条路径必须
    **照样安全**：重新结转不许碰它。
    """
    from app.accounting.vouchers import post_vouchers

    v = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 10),
        entries=_plain_entry(subjects), source=CARRY, remark=remark, commit=False,
    )
    db.flush()
    if posted:
        post_vouchers(db, book.id, [v.id], admin_user, commit=False)
    db.commit()
    return v


# --------------------------------------------------------------------- 1. 客户端进不来


def test_客户端提交CARRY_FORWARD被拒且不落库(
    db, book, subjects, admin_user, client, auth
):
    """Literal 里没有它了，所以是 422（参数不合法），而不是 201 + 一张伪造凭证。"""
    payload = {
        "voucher_date": "2026-01-10",
        "entries": [
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "100.00", "summary": "x"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "100.00", "summary": "x"},
        ],
        "source": CARRY,
    }
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json=payload)

    assert r.status_code == 422, \
        f"客户端提交 {CARRY} 应当被拒，实际 {r.status_code}：{r.text[:300]}"
    db.expire_all()
    assert db.scalars(select(Voucher).where(
        Voucher.book_id == book.id, Voucher.source == CARRY)).all() == [], \
        "被拒的请求不该在库里留下一张结转凭证"
    # 也不能「降级成 MANUAL 悄悄收下」—— 那等于换了个名字继续伪造
    assert db.scalars(select(Voucher).where(
        Voucher.book_id == book.id)).all() == [], "一张凭证都不该写进来"


def test_客户端正常来源仍然可用(db, book, subjects, admin_user, client, auth):
    """对照：闸门不能把手工/导入来源一起拦了。"""
    payload = {
        "voucher_date": "2026-01-10",
        "entries": [
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "100.00", "summary": "x"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": "100.00", "summary": "x"},
        ],
        "source": "MANUAL",
    }
    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json=payload)
    assert r.status_code == 201, r.text[:300]
    assert r.json()["source"] == "MANUAL"


# --------------------------------------------------------------------- 2. 豁免只认 internal


def test_标着结转来源但没有internal照样过不了停用科目这道闸(
    db, book, subjects, admin_user
):
    """``allow_inactive`` 唯一的来源是 ``internal``，source 说了不算。

    过去是 ``source in SYSTEM_SOURCES``：只要把 source 写成 CARRY_FORWARD，
    「停用科目不能新记」这道闸就绕过去了 —— 而它挡的正是「已经不用了的科目
    被重新记上一笔」，老凭证指着它的余额会因此变得莫名其妙。
    """
    target = subjects["560206"]
    target.active = False
    db.flush()

    with pytest.raises(ValueError, match="停用"):
        create_voucher(
            db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 10),
            entries=[
                {"subject_id": subjects["1002"].id, "direction": "DEBIT",
                 "amount": "100.00", "summary": "x"},
                {"subject_id": target.id, "direction": "CREDIT",
                 "amount": "100.00", "summary": "x"},
            ],
            source=CARRY, commit=False,
        )
    db.rollback()


def test_internal的凭证仍然可以记停用科目(db, book, subjects, admin_user):
    """正向路径不能被改坏：结转损益搬的就是停用科目的余额，它必须还能记。

    结转的语义是「把已经存在的余额搬到本年利润」，不是「给这个科目新记一笔」，
    所以豁免对它成立（审计 P2-08 的原意）。
    """
    target = subjects["560206"]
    target.active = False
    db.flush()

    v = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=date(2026, 1, 10),
        entries=[
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "100.00", "summary": "期间损益结转"},
            {"subject_id": target.id, "direction": "CREDIT",
             "amount": "100.00", "summary": "期间损益结转"},
        ],
        source=CARRY, internal=True, commit=False,
    )
    db.commit()
    assert v.source == CARRY
    assert db.get(Voucher, v.id).source == CARRY


def test_停用科目对普通凭证仍然照拦(db, book, subjects, admin_user, client, auth):
    """闸门没有因为多了 internal 就变松：普通来源碰到停用科目照样 400。"""
    target = subjects["560206"]
    target.active = False
    db.commit()

    r = client.post(f"/api/books/{book.id}/vouchers", headers=auth, json={
        "voucher_date": "2026-01-10",
        "entries": [
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": "100.00", "summary": "x"},
            {"subject_id": target.id, "direction": "CREDIT",
             "amount": "100.00", "summary": "x"},
        ],
    })
    assert r.status_code == 400, r.text[:300]


# --------------------------------------------------------------------- 3. 伪造凭证不会被删


def test_重新结转不会删掉来源不明的已记账凭证(
    db, book, periods, subjects, admin_user, make_voucher
):
    """缺陷的核心断言：**那张凭证必须还在**。

    修好之前：``_drop_carry_forward`` 只看 ``source == CARRY_FORWARD``、不看状态，
    伪造的那张会被删掉，凭证号还会被回收给新生成的结转凭证 —— 用户看着
    「重新结转」成功，账上少了一张自己记过的东西，且没有任何提示。

    所以这里断言三件事：结转**失败并说清原因**、伪造凭证**一张不少**、
    本期**没有多出**任何结转凭证（fail closed，不是「删有把握的那几张」）。
    """
    make_voucher("2026-01-10", dr=[("5001", 1000, "收入")],
                 cr=[("1002", 1000, "收款")], posted=True)
    forged = _forge(db, book, admin_user, subjects, posted=True)
    forged_id, forged_no = forged.id, forged.voucher_no
    db.expire_all()
    assert db.get(Voucher, forged_id).status == "POSTED", "前置：伪造凭证已记账"

    p = _period(db, book, 1)

    with pytest.raises(ValueError) as exc:
        svc.carry_forward_profit(db, book.id, p, admin_user)
    assert "来源不明" in str(exc.value), f"报错要说清为什么停下：{exc.value}"
    db.rollback()

    db.expire_all()
    still = db.get(Voucher, forged_id)
    assert still is not None, "伪造的已记账凭证被重新结转删掉了 —— 这就是缺陷本身"
    assert still.voucher_no == forged_no, "凭证号不该变（变了说明号被回收给别人了）"
    assert still.status == "POSTED", "凭证的记账状态不该被改动"
    # fail closed：一部分都不许删，也不能「跳过它照样结转成功」
    assert db.scalars(select(Voucher).where(
        Voucher.period_id == periods[1].id, Voucher.source == CARRY)).all() == [
        db.get(Voucher, forged_id)], "除了那张伪造凭证，不该多出任何结转凭证"


def test_伪造凭证拦下来之后正常的重新结转仍然可用(
    db, book, periods, subjects, admin_user, make_voucher
):
    """拦下不认的凭证之后，把人工处理掉就能继续用 —— 不能把功能锁死。

    这条同时钉住正向路径：系统自己生成的结转凭证签名对得上，
    「重新结转」照常工作（删旧的、生成新的）。
    """
    make_voucher("2026-01-10", dr=[("5001", 1000, "收入")],
                 cr=[("1002", 1000, "收款")], posted=True)
    forged = _forge(db, book, admin_user, subjects, posted=True)
    forged_id = forged.id
    period = _period(db, book, 1)
    db.commit()

    with pytest.raises(ValueError, match="来源不明"):
        svc.carry_forward_profit(db, book.id, period, admin_user)
    db.rollback()

    # 用户按提示手工处理掉那张凭证，再点一次结转
    db.delete(db.get(Voucher, forged_id))
    db.commit()

    result = svc.carry_forward_profit(db, book.id, period, admin_user)
    db.expire_all()
    assert result["vouchers"], "手工处理完之后应当能正常结转"
    assert db.get(Voucher, forged_id) is None, "前置：伪造凭证已被人工删掉"


def test_系统自己生成的结转凭证可以被正常重新结转(
    db, book, periods, subjects, admin_user, make_voucher
):
    """正向路径的回归护栏：签名检查不能把真正的结转凭证也拦下。

    真结转出来的凭证 remark 与摘要都是生成方写死的，签名认得出来；
    连着结两次都必须成功 —— 这是「月末结完发现金额不对，改完再结一次」的
    日常动作（``carry_forward_profit`` 的 replace 语义）。
    """
    make_voucher("2026-01-10", dr=[("5001", 1000, "收入")],
                 cr=[("1002", 1000, "收款")], posted=True)
    period = _period(db, book, 1)
    db.commit()

    first = svc.carry_forward_profit(db, book.id, period, admin_user)
    assert first["vouchers"], "前置：第一次结转应当生成凭证"

    second = svc.carry_forward_profit(db, book.id, period, admin_user)
    db.expire_all()

    assert second["vouchers"], "第二次（重新）结转被签名检查误伤了"
    carry = db.scalars(select(Voucher).where(
        Voucher.period_id == period.id, Voucher.source == CARRY)).all()
    assert len(carry) == len(first["vouchers"]), \
        f"重新结转之后结转凭证应当被替换而不是叠加：{len(carry)} 张"
