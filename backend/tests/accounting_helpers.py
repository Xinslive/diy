"""会计相关的测试辅助（与产品代码无关）。

放在这里而不是 ``conftest.py``：``conftest`` 是 pytest 的装配文件，
用例 import 一个「辅助函数」比 import 一个「装配模块」更说得清它是什么。
"""
from __future__ import annotations


def close_any(db, book, period, user, **kwargs):
    """给测试用的「结这一期」：**空月份先补一张凭证再结**。

    产品口径是硬的：一张凭证都没有的期间不许结账（见
    ``app/accounting/periods.py``），因为空月份恰好能穿过整理体检、自动记账和
    损益结平三道闸门，「这个月忘录了账」会被静默封进历史。

    **而且这条口径对「将被顺带锁上的那些期」同样成立**（审计 P2-1）：
    结账会级联把前面的期间一起锁上，以前那道闸只看被结的那一期，
    于是「1~11 月全空、只有 12 月有凭证」时，11 个空月份被**静默**封进历史 ——
    后果与直接结那个空月一模一样，工作量还更大。所以这里补的是
    **链上每一期**（ord <= 被结那一期、还没结账的），不只是被结的那一期。

    但这些用例里结账多半只是**准备动作** —— 为了走到年度结转、跨年账簿或
    期初连续性而先把某个月封上，被封的那个月本身有没有业务与它们要断言的事
    无关。逐个用例去补业务凭证既啰嗦又会污染它们各自断言的数字
    （累计发生额、期末余额），所以统一在这里补一张**中性凭证**：

        借 1001 库存现金 / 贷 1001 库存现金，金额相同。

    净效果为零 —— 任何余额、任何累计发生额都不变；而「本期有凭证」这件事
    成立了。科目选 1001 而不是 1002，是因为年度结转那批用例断言的是
    1002 银行存款的累计发生额。

    已有凭证的期间原样走 ``close_period``，不做任何干预。
    """
    from datetime import date as _date

    from sqlalchemy import func as _func, select as _select, update as _update

    from app.accounting.vouchers import create_voucher
    from app.models import Period as _Period, Subject as _Subject, Voucher as _Voucher

    # **已结账的期间直接交给 close_period**：那种用例要断言的正是「重复结账
    # 报错」之类，再补一张凭证会先撞上数据库触发器（会计期间已结账，禁止新增凭证），
    # 把要测的那条路径挡在前面。
    # 直接查列而不是 db.get：**db.get 会命中 identity map 返回会话里的旧对象**，
    # 而「结账读的是库里的状态而不是会话缓存」那条用例要的正是两者不一致。
    if db.scalar(_select(_Period.locked).where(_Period.id == period.id)):
        from app.accounting.periods import close_period as _close_locked

        return _close_locked(db, period, user, **kwargs)

    # 链上还没结账的期间（ord <= 被结那一期）。
    chain = db.scalars(
        _select(_Period)
        .where(_Period.book_id == book.id, _Period.ord <= period.ord,
               _Period.locked.is_(False))
        .order_by(_Period.ord)
    ).all()
    counts = dict(db.execute(
        _select(_Voucher.period_id, _func.count(_Voucher.id))
        .where(_Voucher.period_id.in_([p.id for p in chain]))
        .group_by(_Voucher.period_id)
    ).all())
    empty = [p for p in chain if not counts.get(p.id)]

    if empty:
        # 挑一个**资产类的末级且启用的**科目。1001/1002 在部分用例里被升格成了
        # 父节点，有的还被停用了 —— 往非末级或停用科目上记账都会被服务端拒掉
        # （那正是它该有的样子，所以这里要绕开，而不是放宽）。
        subject = db.scalar(
            _select(_Subject)
            .where(_Subject.book_id == book.id, _Subject.is_leaf.is_(True),
                   _Subject.active.is_(True), _Subject.code.like("100%"))
            .order_by(_Subject.code))
        if subject is None:
            raise AssertionError(f"账套 {book.id} 没有资产类末级科目，close_any 无法补中性凭证")
        for p in empty:
            entries = [{"subject_id": subject.id, "direction": "DEBIT", "amount": "100", "summary": "本月业务"},
                       {"subject_id": subject.id, "direction": "CREDIT", "amount": "100", "summary": "本月业务"}]
            voucher = create_voucher(db, book_id=book.id, user=user,
                                     voucher_date=_date(p.year, p.month, 10),
                                     entries=entries, source="MANUAL",
                                     remark="close_any 补的中性凭证", commit=False)
            if not kwargs.get("auto_post", True):
                # 调用方要走 auto_post=False（性能用例量 SQL 语句的那几条），
                # 而那条路上 _check_unposted 会拒绝任何未记账凭证 —— 补的凭证
                # 得**已经是记账状态**。直接改状态而不是走 post_vouchers：
                # 后者要「上一期已结账」，而这些用例的前几期正是开着的。
                db.execute(_update(_Voucher).where(_Voucher.id == voucher.id)
                           .values(status="POSTED", posted_by=user.username))
        db.commit()
    from app.accounting.periods import close_period as _close

    return _close(db, period, user, **kwargs)
