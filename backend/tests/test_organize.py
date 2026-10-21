"""整理凭证：断号体检 / 重编号 / 按日期排序重排。

这个模块的价值在于「两阶段」：先 plan 预览差异，再 apply 落库。
财务改号直接影响已经打印出去的纸质凭证，必须能先看清楚再点确认。
"""
import pytest
from sqlalchemy import select

from app.accounting.organize import (
    PAYMENT,
    build_income_lookup,
    RECEIPT,
    TRANSFER,
    apply_organize,
    cash_flow_kind,
    check_period,
    plan_organize,
    split_no,
)
from app.accounting.vouchers import delete_voucher
from app.models import Subject, Voucher


def _mk(db, book, admin_user, subjects, date_, no=None, amount=10):
    from datetime import date as _d

    from app.accounting.vouchers import create_voucher
    d = _d.fromisoformat(date_) if isinstance(date_, str) else date_
    v = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=d,
        voucher_no=no,
        entries=[
            {"subject_id": subjects["1002"].id, "direction": "DEBIT",
             "amount": str(amount), "summary": "x"},
            {"subject_id": subjects["5001"].id, "direction": "CREDIT",
             "amount": str(amount), "summary": "x"},
        ],
    )
    return v


def _nos(db, book):
    return [(v.id, v.voucher_no) for v in db.scalars(
        select(Voucher).where(Voucher.book_id == book.id).order_by(Voucher.voucher_no)
    ).all()]


# --------------------------------------------------------------------- 解析


def test_拆凭证号():
    assert split_no("记001") == ("记", 1)
    assert split_no("记-0012") == ("记-", 12)
    assert split_no("银007") == ("银", 7)
    assert split_no("手工") == ("手工", 0)      # 解析不了就原样保留


# --------------------------------------------------------------------- 体检


def test_连续编号没有断号(db, book, periods, subjects, admin_user):
    for i in range(1, 4):
        _mk(db, book, admin_user, subjects, f"2026-01-0{i}")
    r = check_period(db, periods[1])
    assert r["total"] == 3
    assert r["gaps"] == []
    assert r["clean"] is True


def test_删除中间一张会断号(db, book, periods, subjects, admin_user):
    """「作废不留号」的代价：号会留洞，所以需要整理功能来补。"""
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)

    r = check_period(db, periods[1])
    assert r["gaps"] == ["记0002"]
    assert r["clean"] is False


def test_体检不改数据(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)
    before = _nos(db, book)
    check_period(db, periods[1])
    assert _nos(db, book) == before


# --------------------------------------------------------------------- 计划


def test_体检只是查断号_不排号也不改号(db, book, periods, subjects, admin_user):
    """断号体检不再是整理方式了，它是列表页顶上的那句提醒，纯只读。"""
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)
    before = _nos(db, book)

    assert check_period(db, periods[1])["gaps"] == ["记0002"]
    assert _nos(db, book) == before


def test_计划阶段绝对不碰数据(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)
    before = _nos(db, book)

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="RENUM")
    plan = plans[0]
    assert plan.gaps == ["记0002"]
    assert any(c.changed for c in plan.changes)
    assert _nos(db, book) == before, "计划阶段绝对不能碰数据"


def test_RENUM补断号(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="RENUM")
    changes = {c.old_no: c.new_no for c in plans[0].changes if c.changed}
    assert changes == {"记0003": "记0002"}      # 记0001 原地不动，不算改动


def test_重排会交换号_两阶段必须生效(db, book, periods, subjects, admin_user):
    """记0001 与 记0002 互换编号，直接改会撞唯一约束。"""
    a = _mk(db, book, admin_user, subjects, "2026-01-20", no="记0001")
    b = _mk(db, book, admin_user, subjects, "2026-01-10", no="记0002")

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    changes = {c.voucher_id: (c.old_no, c.new_no) for c in plans[0].changes}
    assert changes[a.id] == ("记0001", "记0002")
    assert changes[b.id] == ("记0002", "记0001")

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")

    db.expire_all()
    assert db.get(Voucher, a.id).voucher_no == "记0002"
    assert db.get(Voucher, b.id).voucher_no == "记0001"


def test_只有升序没有倒序(db, book, periods, subjects, admin_user):
    """倒序编号撤了：按日期倒着编号，纸质凭证翻出来永远是从后往前，不符合记账习惯。"""
    with pytest.raises(ValueError, match="未知的整理方式"):
        plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE_DESC")
    db.rollback()


def test_结转损益排在最后(db, book, periods, subjects, admin_user):
    a = _mk(db, book, admin_user, subjects, "2026-01-05", no="记0001")
    cf1 = _mk(db, book, admin_user, subjects, "2026-01-31", no="记0002")
    cf2 = _mk(db, book, admin_user, subjects, "2026-01-31", no="记0003")
    for v in (cf1, cf2):
        v.source = "CARRY_FORWARD"
    db.commit()

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    new_no = {c.voucher_id: c.new_no for c in plans[0].changes}
    assert new_no[a.id] == "记0001"
    assert {new_no[cf1.id], new_no[cf2.id]} == {"记0002", "记0003"}

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")
    db.expire_all()
    assert db.get(Voucher, cf1.id).voucher_no in ("记0002", "记0003")
    assert db.get(Voucher, cf2.id).voucher_no in ("记0002", "记0003")
    assert db.get(Voucher, a.id).voucher_no == "记0001"


def test_保留原来的凭证字(db, book, periods, subjects, admin_user):
    """多数凭证用「银」字，重排后不能全变成「记」。"""
    _mk(db, book, admin_user, subjects, "2026-01-05", no="银0001")
    _mk(db, book, admin_user, subjects, "2026-01-06", no="银0002")

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")
    assert [n for _i, n in _nos(db, book)] == ["银0001", "银0002"]


# --------------------------------------------------------------------- 时序 / 闸门


def test_时序不对查得出来(db, book, periods, subjects, admin_user):
    """号大的那张日期反而更早 = 这个月是回头补记的，号段已经乱了。"""
    _mk(db, book, admin_user, subjects, "2026-01-25", no="记0001")
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记0002")

    rep = check_period(db, periods[1])
    assert len(rep["time_out_of_order"]) == 1
    assert rep["time_out_of_order"][0]["prev_no"] == "记0001"
    assert rep["time_out_of_order"][0]["voucher_no"] == "记0002"
    assert rep["clean"] is False
    assert any("时序不对" in b for b in rep["blocking"])


def test_同日多张不算时序不对(db, book, periods, subjects, admin_user):
    """同一天的两张凭证谁先谁后不算错 —— 日期相同，比不出先后。"""
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记0001")
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记0002")
    assert check_period(db, periods[1])["clean"] is True


def test_时序不对时不能用当前顺序编号(db, book, periods, subjects, admin_user):
    _mk(db, book, admin_user, subjects, "2026-01-25", no="记0001")
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记0002")

    with pytest.raises(ValueError, match="时序"):
        plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="RENUM")
    db.rollback()
    # 换个方式就好：先按日期排好时序，正是这条规则存在的意义
    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    assert [c.new_no for c in plans[0].changes] == ["记0001", "记0002"]


def test_已记账的凭证不改号(db, book, periods, subjects, admin_user):
    """已记账的占着号原样不动，其余凭证只在空出来的号里重新发。"""
    from app.accounting.vouchers import post_vouchers

    a = _mk(db, book, admin_user, subjects, "2026-01-05")
    b = _mk(db, book, admin_user, subjects, "2026-01-06")
    c = _mk(db, book, admin_user, subjects, "2026-01-07")
    post_vouchers(db, book.id, [b.id], admin_user)
    db.expire_all()

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id, strategy="SORT_DATE")
    new_no = {c_.voucher_id: c_.new_no for c_ in plans[0].changes}
    assert new_no[b.id] == b.voucher_no, "已记账那张号不能动"
    assert new_no[a.id] == "记0001"
    assert new_no[c.id] == "记0003"

    apply_organize(db, book_id=book.id, user=admin_user, period_id=periods[1].id)
    db.expire_all()
    assert db.get(Voucher, b.id).status == "POSTED"
    assert db.get(Voucher, b.id).voucher_no == b.voucher_no


def test_断号的月份不许记账(db, book, periods, subjects, admin_user):
    from app.accounting.vouchers import post_vouchers

    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 4)]
    delete_voucher(db, vs[1], admin_user)          # 留个洞

    with pytest.raises(ValueError, match="还没整理干净"):
        post_vouchers(db, book.id, [vs[0].id, vs[2].id], admin_user)
    db.rollback()

    apply_organize(db, book_id=book.id, user=admin_user, period_id=periods[1].id)
    post_vouchers(db, book.id, [v.id for v in (vs[0], vs[2])], admin_user)
    assert db.get(Voucher, vs[0].id).status == "POSTED"


def test_时序不对的月份不许结账(db, book, periods, subjects, admin_user):
    from app.accounting.periods import close_period

    a = _mk(db, book, admin_user, subjects, "2026-01-25", no="记0001")
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记0002")
    with pytest.raises(ValueError, match="时序不对"):
        close_period(db, periods[1], admin_user)
    db.rollback()


# --------------------------------------------------------------------- 护栏


def test_导入进来的损益结转凭证也垫底(db, book, periods, subjects, admin_user):
    """用户 2026-10-19：摘要写着「期间损益结转」的凭证，整理时要排在最后。

    原来只认 ``source == "CARRY_FORWARD"``，也就是只认**系统**生成的那些。
    但用户的结转凭证是从别的账套导进来的，``source`` 是 ``IMPORT`` ——
    实测生产库里 CARRY_FORWARD 凭证数为 0，优境 9 个月每月各有一张结转凭证
    全是导入的，于是 tail 恒为 0，结转凭证被排进号段中间，一眼认不出期末那几张。

    这里刻意把结转凭证排在**中间**（记0002，日期也在中间），确保测的是
    「它被拉到末尾」而不是「它本来就在末尾」。
    """
    from app.models import Entry

    m1 = _mk(db, book, admin_user, subjects, "2026-01-05", no="记0001")
    cf = _mk(db, book, admin_user, subjects, "2026-01-20", no="记0002")
    m3 = _mk(db, book, admin_user, subjects, "2026-01-25", no="记0003")
    cf.source = "IMPORT"          # 导入进来的结转凭证就是这样
    cf.entries[0].summary = "期间损益结转"
    db.commit()
    assert cf.source != "CARRY_FORWARD", "这个用例的前提就是它不是系统生成的"

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id,
                          strategy="SORT_DATE")
    new_no = {c.voucher_id: c.new_no for c in plans[0].changes}
    assert new_no[cf.id] == "记0003", f"结转凭证没垫底，拿到了 {new_no[cf.id]}"
    assert {new_no[m1.id], new_no[m3.id]} == {"记0001", "记0002"}


def test_普通凭证里带结转二字的不算结转(db, book, periods, subjects, admin_user):
    """标记词只认「期间损益结转」「结转损益」两个完整短语。

    只写「结转」两个字太宽：手工凭证里「结转上月的余额」这种摘要会被误判，
    把一张正常凭证扔到号段末尾。
    """
    from app.models import Entry

    a = _mk(db, book, admin_user, subjects, "2026-01-05", no="记0001")
    b = _mk(db, book, admin_user, subjects, "2026-01-06", no="记0002")
    b.entries[0].summary = "结转上月的余额"
    db.commit()

    plans = plan_organize(db, book_id=book.id, period_id=periods[1].id,
                          strategy="SORT_DATE")
    new_no = {c.voucher_id: c.new_no for c in plans[0].changes}
    assert new_no[b.id] == "记0002", "普通凭证被误判成结转凭证了"


def test_结转凭证垫底_录入单在前(db, book, periods, subjects, admin_user):
    """结转损益也参与重编号，但排序键里它排在最后 —— 所以永远拿到末尾的号。

    早先的做法是「结转凭证不参与重编号」，于是手工凭证压紧之后会踩到结转占着的号：
    三张手工凭证压成 0001~0003，而结转那张还占着 002，直接撞唯一约束。
    """
    m1 = _mk(db, book, admin_user, subjects, "2026-01-05", no="记0001")
    m2 = _mk(db, book, admin_user, subjects, "2026-01-06", no="记0003")
    cf = _mk(db, book, admin_user, subjects, "2026-01-31", no="记0002")
    cf.source = "CARRY_FORWARD"
    db.commit()

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")
    db.expire_all()
    assert db.get(Voucher, m1.id).voucher_no == "记0001"
    assert db.get(Voucher, m2.id).voucher_no == "记0002"
    assert db.get(Voucher, cf.id).voucher_no == "记0003"


def test_按当前顺序重排时结转也在最后(db, book, periods, subjects, admin_user):
    """RENUM 保持存储顺序，但结转键在最高位，所以照样垫底。"""
    cf = _mk(db, book, admin_user, subjects, "2026-01-10", no="记0001")
    cf.source = "CARRY_FORWARD"
    m = _mk(db, book, admin_user, subjects, "2026-01-20", no="记0002")
    db.commit()

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="RENUM")
    db.expire_all()
    assert db.get(Voucher, m.id).voucher_no == "记0001"
    assert db.get(Voucher, cf.id).voucher_no == "记0002"


def test_已结账期间不能整理(db, book, periods, subjects, admin_user):
    from app.accounting.periods import carry_forward_profit, close_period
    from app.accounting.vouchers import post_vouchers

    v = _mk(db, book, admin_user, subjects, "2026-01-05", no="记0009")
    # 号段没整理干净就不许记账 —— 记账之前先把号排好，这是新的闸门
    with pytest.raises(ValueError, match="还没整理干净"):
        post_vouchers(db, book.id, [v.id], admin_user)
    db.rollback()
    apply_organize(db, book_id=book.id, user=admin_user, period_id=periods[1].id)

    post_vouchers(db, book.id, [v.id], admin_user)      # 结转损益只看已记账口径
    carry_forward_profit(db, book.id, periods[1], admin_user)
    close_period(db, periods[1], admin_user)
    db.refresh(periods[1])

    with pytest.raises(ValueError, match="已结账"):
        apply_organize(db, book_id=book.id, user=admin_user,
                       period_id=periods[1].id, strategy="RENUM")
    db.rollback()


def test_整账套整理按期间分组(db, book, periods, subjects, admin_user):
    _mk(db, book, admin_user, subjects, "2026-01-05", no="记0005")
    _mk(db, book, admin_user, subjects, "2026-02-05", no="记0005")

    r = apply_organize(db, book_id=book.id, user=admin_user, strategy="RENUM")
    assert len(r["periods"]) == 12               # 12 个期间各一段
    assert all(p["period_id"] is not None for p in r["periods"])

    db.expire_all()
    nos = sorted(n for _i, n in _nos(db, book))
    assert nos == ["记0001", "记0001"]            # 各自期间内都压成 0001


def test_未知整理方式报错(db, book, admin_user):
    with pytest.raises(ValueError, match="未知的整理方式"):
        plan_organize(db, book_id=book.id, strategy="MAGIC")
    db.rollback()


def test_断号检查不能被当成整理方式(db, book, periods, subjects, admin_user):
    _mk(db, book, admin_user, subjects, "2026-01-05")
    with pytest.raises(ValueError, match="未知的整理方式"):
        apply_organize(db, book_id=book.id, user=admin_user,
                       period_id=periods[1].id, strategy="GAPS")
    db.rollback()


def test_整理后编号连续无断号(db, book, periods, subjects, admin_user):
    vs = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 5)]
    delete_voucher(db, vs[1], admin_user)
    delete_voucher(db, vs[3], admin_user)

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="RENUM")

    r = check_period(db, periods[1])
    assert r["clean"] is True
    assert sorted(n for _i, n in _nos(db, book)) == ["记0001", "记0002"]


# --------------------------------------------------------------------- 收 / 付 / 转账
#
# 下面这组钉的是「从分录自动判定 + 只用于同日排序」这条口径（用户 2026-10-13 定的）。
# 关键约束三条：
#   1. 判定**不新增凭证字** —— 号段仍是单一「记」段，check_period 的混用闸门不触发；
#   2. 判定**按现金类科目的借贷方向**，不按金额大小；
#   3. 借贷两边都有现金 → 转账（提现、银行手续费这类收支两抵的）。
# 这三条任一被改写，同日顺序就会在真实账里翻车，所以都要有钉子。


def _mk_cash(db, book, admin_user, subjects, date_, dr, cr, insert_after_id=None):
    """dr / cr 是 (科目编码, 金额) 列表。"""
    from datetime import date as _d

    from app.accounting.vouchers import create_voucher
    d = _d.fromisoformat(date_) if isinstance(date_, str) else date_
    return create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=d,
        insert_after_id=insert_after_id,
        entries=(
            [{"subject_id": subjects[dc].id, "direction": "DEBIT",
              "amount": str(da), "summary": "x"} for dc, da in dr]
            + [{"subject_id": subjects[cc].id, "direction": "CREDIT",
                "amount": str(ca), "summary": "x"} for cc, ca in cr]
        ),
    )


def _kind_of(db, voucher):
    """只按现金净额判定 —— 不传 income_of，走的是「压根没碰现金就一律转账」的兜底。

    真实调用路径一定会传 income_of（plan_organize 建好透传下来），所以下面凡是
    判「没碰现金」的用例，都用 :func:`_kind_by_profit` 走完整路径。
    """
    from app.accounting.organize import cash_flow_kind
    db.refresh(voucher)
    return cash_flow_kind(voucher.entries)


def _kind_by_profit(db, book, voucher):
    """按整理页真正调用的那条路判定：先建收入方 lookup，再传给 cash_flow_kind。"""
    from app.accounting.organize import build_income_lookup, cash_flow_kind
    db.refresh(voucher)
    return cash_flow_kind(voucher.entries, build_income_lookup(db, book.id))


def _add_detail_subject(db, book, parent_code, code, name):
    """在既有科目下挂一个明细科目，返回它。

    真实账里明细科目的名字就是「157」「503」这种单元编号，判据必须回溯到顶层
    才认得出来 —— 所以测试也得用这种「看不出名堂」的名字，否则测的是名字而不是链。
    """
    parent = db.scalar(
        select(Subject).where(Subject.book_id == book.id, Subject.code == parent_code))
    assert parent is not None, f"父科目不存在：{parent_code}"
    child = Subject(book_id=book.id, year=parent.year, code=code, name=name,
                    category=parent.category,
                    direction=parent.direction, level=parent.level + 1,
                    parent_id=parent.id, is_leaf=True, sort_no=99)
    db.add(child)
    parent.is_leaf = False
    db.flush()
    return child


def test_判定_钱进账为收_钱出账为付_两边都是转账(db, book, periods, subjects, admin_user):
    # 借 银行存款 = 资产增加 = 钱进账 → 收
    receipt = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                       dr=[("1002", 900)], cr=[("5001", 900)])
    # 贷 银行存款 = 资产减少 = 钱出账 → 付
    payment = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                       dr=[("560206", 100)], cr=[("1002", 100)])
    # 借 1001 库存现金 + 贷 1002 银行存款 = 提现，收支两抵 → 转账
    transfer = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                        dr=[("1001", 500)], cr=[("1002", 500)])

    assert _kind_of(db, receipt) == RECEIPT
    assert _kind_of(db, payment) == PAYMENT
    assert _kind_of(db, transfer) == TRANSFER


def test_判定_没碰现金的凭证按损益方向_计提工资判付(
        db, book, periods, subjects, admin_user):
    """计提工资（借 管理费用-员工工资 / 贷 应付职工薪酬）→ **付**。

    工资这笔钱是**欠**出去了，只是不在这张凭证上付 —— 业务性质是支出，
    按旧规则「没碰现金就是转账」会把它和「借 应收账款 / 贷 其他应付款」这种
    纯往来划拨混在同段，对账时找不着这笔已经发生的成本。
    """
    accrue = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                      dr=[("560209", 800)], cr=[("221101", 800)])
    assert _kind_by_profit(db, book, accrue) == PAYMENT


def test_判定_挂应收确认管理费判收(db, book, periods, subjects, admin_user):
    """记-0045 的真实形态：挂应收确认管理费收入 → **收**。

    星悦荟商管 2026-09-30 记-0045「管理费2026-09-25-2026-12-24/周淑琴」
    就是这个形态：一分钱现金都不碰，借 应收账款、贷 主营业务收入 + 预收账款。
    债权进来了，钱在路上 —— 业务性质是收，判成转账就等于把这张凭证从收入段里抹掉。
    """
    v = _mk_cash(db, book, admin_user, subjects, "2026-09-30",
                 dr=[("1122", 112000)], cr=[("5001", 70000), ("2203", 42000)])
    assert _kind_by_profit(db, book, v) == RECEIPT


def test_判定_折旧摊销没碰现金判付(db, book, periods, subjects, admin_user):
    """折旧（借 管理费用-折旧 / 贷 累计折旧）→ **付**。"""
    v = _mk_cash(db, book, admin_user, subjects, "2026-09-30",
                 dr=[("560210", 966)], cr=[("1602", 966)])
    assert _kind_by_profit(db, book, v) == PAYMENT


def test_判定_损益收支相等归转账(db, book, periods, subjects, admin_user):
    """收入 500 = 费用 500 → 转账（成本分摊一类的对冲）。

    星悦荟商管「9月停车费支出」那张（借 停车费支出 / 贷 停车费收入，净额 0）
    就是这个形态：钱既没进也没出，两边抵平了，硬判成收或付都是替用户编一个方向。
    """
    v = _mk_cash(db, book, admin_user, subjects, "2026-09-30",
                 dr=[("1122", 500), ("560206", 500)],
                 cr=[("5001", 500), ("2203", 500)])
    assert _kind_by_profit(db, book, v) == TRANSFER


def test_判定_没碰现金也没损益科目的往来划拨归转账(
        db, book, periods, subjects, admin_user):
    """借 应收账款 / 贷 预收账款：既没现金也没损益科目 → 转账。

    这条守着兜底分支的另一半：不能因为「现在按损益方向判了」，就把这类纯往来
    划拨也硬塞成收付 —— 方向是真的没有，编一个出来比留白更坏。
    """
    v = _mk_cash(db, book, admin_user, subjects, "2026-09-30",
                 dr=[("1122", 500)], cr=[("2203", 500)])
    assert _kind_by_profit(db, book, v) == TRANSFER


def test_判定_收入方要回溯到顶层科目(db, book, periods, subjects, admin_user):
    """明细科目名是「157」这种单元编号，判不出来 —— 判据必须挂在顶层科目上。

    星悦荟商管真实的顶层就是靠名字分开的三棵：5001 主营业务收入物业管理费、
    5004 停车费支出、5005 停车费收入。三者都挂在损益下，光看 category 分不出
    收入还是支出，名字也只在顶层才有信息量。
    """
    income_detail = _add_detail_subject(db, book, "5001", "5001999", "157")
    expense_detail = _add_detail_subject(db, book, "5602", "5602999", "503")
    subjects["5001999"] = income_detail
    subjects["5602999"] = expense_detail

    # 名字里没有任何收入字样，但挂在 5001 主营业务收入下面 → 收
    rec = _mk_cash(db, book, admin_user, subjects, "2026-09-30",
                   dr=[("1122", 300)], cr=[("5001999", 300)])
    assert _kind_by_profit(db, book, rec) == RECEIPT

    # 同理挂在 5602 管理费用下面 → 付
    pay = _mk_cash(db, book, admin_user, subjects, "2026-09-30",
                   dr=[("5602999", 200)], cr=[("2203", 200)])
    assert _kind_by_profit(db, book, pay) == PAYMENT


def test_判定_收付混合按净额_净流入判收(db, book, periods, subjects, admin_user):
    """手续费(付 186) + 利息(收 980) → 净 +794 → **收**。

    真实账里这个形态反复出现（启明物业 13 张、星悦荟商管 39 张）：一张凭证
    里既有一笔现金类对外支付（手续费），又有一笔现金类收入（利息），现金类
    借贷都有。旧规则「借贷都有就算转账」把它整张打成转账，业务上却明明是
    收到利息 —— 用户 2026-10-16 定的口径是「同属收和转的，认定为收」。
    """
    mixed = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                     dr=[("1002", 980), ("560302", 186)],
                     cr=[("1002", 186), ("5111", 980)])
    assert _kind_of(db, mixed) == RECEIPT


def test_判定_收付混合按净额_净流出判付(db, book, periods, subjects, admin_user):
    """同一形态的另一面：净额为负就判付。

    这一条和上面那条是同一个判据的两半。少写一半的话，「一律判收」会把
    真实账里净流出的那 16 张付款凭证错塞进收段（星悦荟商管 39 张混合凭证里
    有 15 张净流出）。
    """
    mixed = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                     dr=[("1002", 800), ("560302", 200)],
                     cr=[("1002", 1000)])
    assert _kind_of(db, mixed) == PAYMENT


def test_判定_打包了一笔内部划转的凭证看净额(db, book, periods, subjects, admin_user):
    """「折旧 + 收入 + 微信↔银行对倒」这种打包凭证，现金净额为 0 → 转账。

    对倒那一对现金分录在净额里自己抵掉了，留在净额里的只有真正的钱进出；
    这里进出相抵，所以它是转账，不是收也不是付。
    """
    packed = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                      dr=[("1002", 4967), ("1002", 4728)],
                      cr=[("1002", 4728), ("1002", 4967)])
    assert _kind_of(db, packed) == TRANSFER


def test_判定_混合收款里夹一笔内部划转仍判收(db, book, periods, subjects, admin_user):
    """真实形态：收电费，微信收到一笔，其中一部分当场转到银行。

    划转部分自己抵掉，净额只剩真正的收款 → 收。
    """
    with_move = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                         dr=[("1002", 77735), ("1002", 100)],
                         cr=[("1002", 100), ("1122", 77735)])
    assert _kind_of(db, with_move) == RECEIPT


def test_同日排序_先收后付再转账(db, book, periods, subjects, admin_user):
    # 故意乱序录入：转账、付、收 —— 期望按日期整理后，收在前、转账在后
    _mk_cash(db, book, admin_user, subjects, "2026-01-05",
             dr=[("1001", 500)], cr=[("1002", 500)])           # 转账
    pay = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                   dr=[("560206", 300)], cr=[("1002", 300)])    # 付
    rec = _mk_cash(db, book, admin_user, subjects, "2026-01-05",
                   dr=[("1002", 700)], cr=[("5001", 700)])   # 收

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="SORT_DATE")

    ordered = db.scalars(
        select(Voucher).where(Voucher.book_id == book.id)
        .order_by(Voucher.voucher_no)
    ).all()
    kinds = [cash_flow_kind(v.entries) for v in ordered]
    # 三张同为 2026-01-05，应严格按 收→付→转账
    assert kinds == [RECEIPT, PAYMENT, TRANSFER], f"实际顺序 {kinds}"
    assert rec.id == ordered[0].id
    assert pay.id == ordered[1].id


def test_同日排序_没碰现金的两张也分得出先后(
        db, book, periods, subjects, admin_user):
    """接线守卫：income_of 真的从 plan_organize 透传到了排序。

    判定逻辑单测全对也可能在链路上白写 —— 只要 ``_sort_key`` 忘了接收这个参数，
    排序就会静默退回「没碰现金的一律转账」。所以这里必须从 ``apply_organize``
    整条路走一遍，验的是**排出来的号**，不是某个函数的返回值。

    故意按「转账 → 付 → 收」录：旧规则下三张全是转账、顺序不变；新规则下
    期望严格 收 → 付 → 转账。这张用例挂了，就说明链路没接上。
    """
    _mk_cash(db, book, admin_user, subjects, "2026-09-30",
             dr=[("1001", 500)], cr=[("1002", 500)])            # 转账（碰过现金）
    pay = _mk_cash(db, book, admin_user, subjects, "2026-09-30",
                   dr=[("560209", 800)], cr=[("221101", 800)])  # 没碰现金 → 付
    rec = _mk_cash(db, book, admin_user, subjects, "2026-09-30",
                   dr=[("1122", 1000)], cr=[("5001", 1000)])   # 没碰现金 → 收

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[9].id, strategy="SORT_DATE")

    ordered = db.scalars(
        select(Voucher).where(Voucher.book_id == book.id)
        .order_by(Voucher.voucher_no)
    ).all()
    # 三张同为 2026-09-30，应严格按 收→付→转账
    assert [v.id for v in ordered] == [rec.id, pay.id, ordered[2].id]
    kinds = [cash_flow_kind(v.entries, build_income_lookup(db, book.id))
             for v in ordered]
    assert kinds == [RECEIPT, PAYMENT, TRANSFER], f"实际顺序 {kinds}"


def test_判定不新增凭证字_号段仍是单一段(db, book, periods, subjects, admin_user):
    """判定只服务排序，绝不能把「记」拆成收/付/转三段。"""
    _mk_cash(db, book, admin_user, subjects, "2026-01-05",
             dr=[("1002", 700)], cr=[("5001", 700)])
    _mk_cash(db, book, admin_user, subjects, "2026-01-06",
             dr=[("560206", 100)], cr=[("1002", 100)])

    r = check_period(db, periods[1])
    assert r["clean"] is True
    assert r["prefixes"] == ["记"]          # 仍然只有一个凭证字
    assert not any("凭证字" in b for b in r["blocking"])


# --------------------------------------------------------------------- RENUM 缺陷回归


def test_RENUM_插入凭证后不破坏时序(db, book, periods, subjects, admin_user):
    """回归：插入凭证把号顺延后，RENUM 必须按**当前号**排，不能按 id。

    修复前 RENUM 用 ``voucher.id`` 排序，而插入让新凭证拿中间的号、id 却最大，
    于是 RENUM 把它甩到末尾 —— 一个月从干净被整理成「时序不对」。
    """
    # 连记 5 张，号 记0001..记0005
    stack = [_mk(db, book, admin_user, subjects, f"2026-01-0{i}") for i in range(1, 6)]
    # 在 记0002 后面插一张，日期与参照凭证**同一天**（体检判它没错，RENUM 会放行）
    inserted = _mk_cash(db, book, admin_user, subjects, "2026-01-02",
                        dr=[("1002", 50)], cr=[("5001", 50)],
                        insert_after_id=stack[1].id)

    assert check_period(db, periods[1])["clean"] is True, "前置：插入后应当体检干净"

    apply_organize(db, book_id=book.id, user=admin_user,
                   period_id=periods[1].id, strategy="RENUM")

    r = check_period(db, periods[1])
    assert r["clean"] is True, f"RENUM 不该制造时序错乱，实际 {r['blocking']}"
    # 插入的那张仍待在号段中间（记0002），没被甩到最后
    final = {v.id: v.voucher_no for v in db.scalars(
        select(Voucher).where(Voucher.book_id == book.id)).all()}
    assert final[inserted.id] == "记0002", f"插入的凭证应留在 记0002，实际 {final[inserted.id]}"
