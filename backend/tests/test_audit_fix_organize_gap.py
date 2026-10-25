"""审计修复 §3.17：「断号补不补得上」只有**一份**判据。

**这条缺陷是什么。**

同一条规则在仓里有**两份实现**，而且互相矛盾：

* ``organize.irreparable_gaps()``（删凭证的入口闸门）用 **M** 判据；
* ``organize.assert_tidy()``（记账 / 结账的闸门）用 **N** 判据。

于是同一件事有两个答案：用户删掉一张凭证，入口说「这个洞「整理凭证」补得上，
放行」；紧接着去记账，闸门说「补不上」，而且给出**两条错出路**：

* ① 手工补一张用那个号的凭证（导入也可以），把号段接上 ——
  为了凑一个号而**在账上凭空造一张金额任意的凭证**；
* ② 把已记账的凭证全部反结账 —— 为了改一个号而**把整段已记账的凭证退回未记账**。

**M 与 N 的差别。** 整理只给**未记账**的凭证重新发号（已记账的号原样留着），
它把未记账的那些压紧成 1..N（跳过已记账占着的号）。所以：

* N = 未记账的凭证数；
* M = 第 N 个「不在已记账占着的号里」的正整数 —— 整理**真正发出去的最大序号**。

断号的序号 <= M → **填得上**；> M → 填不上。

**把两者算岔的例子**（就是本文件复现的那一组）：

    记0001（未记账） 记0002（已记账） 记0004（未记账），记0003 被删了

    N = 2  → 3 > 2  → 「补不上」   ❌
    M = 3  → 3 <= 3 → 「点一次「整理凭证」就能补上」  ✅（实测：记0004 → 记0003）

修法：把判据抽成**唯一**的共享函数 ``organize.fixable_gaps(rows)``，
两处都调它 —— 并且 ``assert_tidy`` 的提示同步改成「点一次「整理凭证」就能补上」。

**后来又修了一次（同一个缺陷的另一半）。**

「只有一份实现」解决了「两个答案互相矛盾」，但**那一份实现本身是错的** ——
复核方用**差分测试 + 变异测试**查出：M 判据与「整理真的补不补得上」
**不等价**（310 个号段配置里 44 个误报）。

**误报的确切条件：号段顶端（最大序号那张）是未记账凭证时。**
断号的定义是「号段 1..max 里缺的那些号」，而 **max 会变** ——
整理把号段顶端那张压到前面去之后，整段的上限跟着变小，
那些高位空号**因此不再是断号**（退出了号段），整理一次就把洞补上了。
M 判据只比序号、不看整理之后号段还剩多长，于是判错了。

所以判据现在改成：**照整理的规则模拟一次整理，看这个号之后还是不是一个断号**。
本文件下半部分的用例钉的就是这一版。
"""
from __future__ import annotations

import pytest
from datetime import date

from sqlalchemy import select

from app.accounting import organize as svc_organize
from app.accounting.vouchers import delete_voucher
from app.models import Period, Voucher


def _rows(db, period) -> list:
    """这个期间的凭证行（体检要用的那三个字段）。"""
    return svc_organize._period_rows(db, period)


def test_整理补得上的断号不再被说成补不上(db, book, periods, make_voucher):
    """审计报告 §3.17 的那一组：入口说补得上，闸门也得说补得上。"""
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")],
                      posted=True)
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")])
    v4 = make_voucher("2026-01-08", dr=[("1001", 40, "x")], cr=[("5001", 40, "x")])
    assert [v.voucher_no for v in (v1, v2, v3, v4)] == ["记0001", "记0002", "记0003", "记0004"]

    # 删掉 记0003 → 洞在 0003
    db.delete(v3)
    db.commit()

    period = db.get(Period, periods[1].id)
    # 共享判据说它**补得上**
    fixable, stuck = svc_organize.fixable_gaps(_rows(db, period))
    assert fixable == ["记0003"], f"共享判据说补不上：fixable={fixable} stuck={stuck}"
    assert stuck == [], f"共享判据说补不上：stuck={stuck}"

    # 删除入口那道闸：同样说补得上（它本来就是 M 判据）
    assert svc_organize.irreparable_gaps(db, book.id, period, set()) == []

    # 记账 / 结账那道闸：必须与上面**一致**
    with pytest.raises(ValueError) as ei:
        svc_organize.assert_tidy(db, [period])
    db.rollback()
    msg = str(ei.value)
    assert "点一次「整理凭证」就能补上" in msg, msg
    assert "补不上" not in msg, f"仍在说「补不上」，并给出两条错出路：{msg}"


def test_整理真补不上的断号仍然说补不上(db, book, periods, make_voucher):
    """对照组：M 判据说补不上的，**仍然**要说补不上（不许把闸门放松掉）。"""
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")],
                      posted=True)
    db.delete(v2)          # 记0002 空出来，而 记0003 已记账占着号
    db.commit()

    period = db.get(Period, periods[1].id)
    fixable, stuck = svc_organize.fixable_gaps(_rows(db, period))
    assert fixable == [], f"这个洞补不上：fixable={fixable}"
    assert stuck == ["记0002"], f"这个洞补不上：stuck={stuck}"

    assert svc_organize.irreparable_gaps(db, book.id, period, set()) == ["记0002"]

    with pytest.raises(ValueError) as ei:
        svc_organize.assert_tidy(db, [period])
    db.rollback()
    msg = str(ei.value)
    assert "补不上" in msg, msg
    # 真补不上时，那两条出路仍然是唯一走得通的两条
    assert "反结账" in msg, msg


def test_两处闸门对同一批数据给同一个答案(db, book, periods, make_voucher):
    """**同一个洞，删除入口与记账闸门不许给出两个答案。**

    这是这条缺陷的本质：不是「某个提示写得不准」，是**两份实现**。
    """
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")],
                      posted=True)
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")])
    v4 = make_voucher("2026-01-08", dr=[("1001", 40, "x")], cr=[("5001", 40, "x")])
    db.delete(v3)
    db.commit()

    period = db.get(Period, periods[1].id)
    _fixable, stuck = svc_organize.fixable_gaps(_rows(db, period))
    irreparable = svc_organize.irreparable_gaps(db, book.id, period, set())
    assert irreparable == stuck, (
        f"两份实现给了两个答案：删除入口说 {irreparable}，记账闸门说 {stuck}")


def test_整理凭证真的能把这个洞补上(db, book, periods, make_voucher, admin_user):
    """提示语说「点一次「整理凭证」就能补上」——那就**真的**能补上。

    提示与事实必须对上：否则又是一句骗人的提示（这正是本条缺陷的教训）。
    """
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")],
                      posted=True)
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")])
    v4 = make_voucher("2026-01-08", dr=[("1001", 40, "x")], cr=[("5001", 40, "x")])
    db.delete(v3)
    db.commit()

    period = db.get(Period, periods[1].id)
    with pytest.raises(ValueError):
        svc_organize.assert_tidy(db, [period])
    db.rollback()

    svc_organize.apply_organize(db, book_id=book.id, period_id=period.id,
                                 strategy="RENUM", user=admin_user)
    db.commit()

    rep = svc_organize.check_period(db, period)
    assert rep["gaps"] == [], f"整理之后洞还在：{rep['gaps']}"
    assert rep["clean"] is True, rep


# ===================================================================== §3.17 的真旁路
#
# 复核方用**差分测试 + 变异测试**查出：M 判据与「整理真的补不补得上」
# **不等价** —— 310 个号段配置里有 44 个误报。
#
# **误报的确切条件：号段顶端（最大序号那张）是未记账凭证时。**
# 断号的定义是「号段 1..max 里缺的那些号」，而 **max 会变** —— 整理把
# 号段顶端那张压到前面去之后，整段的上限跟着变小，那些高位空号
# **因此不再是断号**（退出了号段），整理一次就把洞补上了。
#
# M 判据（「断号的序号 > 整理发出去的最大序号 M → 补不上」）看不到这一层：
# 它只比序号，不看整理之后号段还剩多长。
#
# 本地穷举复核（1..5 号的任意子集 × 任意「哪些已记账」= 243 种组合）：
# 旧判据在其中 **26** 种上与真值不一致 —— **全部**是误报（说补不上、
# 其实整理一次就补上了），没有一个反例。
#
# 真值 =「**照整理的规则跑一次整理，这个号之后还是不是一个断号**」。


def _rows_of(db, period) -> list:
    return svc_organize._period_rows(db, period)


def _truth_stuck(rows) -> list[str]:
    """真值（按定义独立算一遍）：整理跑完之后**仍然是断号**的那些。

    发号规则照 ``plan_organize`` 的定义独立写一遍（已记账的号跳过，
    其余的从小到大压紧），断号用**生产代码同一份** ``_analyze_rows`` 算 ——
    断号语义本来就该只有一份。
    """
    reserved = {svc_organize._seq_of(r.no) for r in rows if r.status == "POSTED"}
    assigned: list[int] = []
    n = 0
    for row in rows:
        if row.status == "POSTED":
            continue
        n += 1
        while n in reserved:
            n += 1
        assigned.append(n)
    final = reserved | set(assigned)
    top = max(final) if final else 0
    after = {x for x in range(1, top + 1) if x not in final}
    before = svc_organize._analyze_rows(rows)["gaps"]
    return [g for g in before if svc_organize._seq_of(g) in after]


def test_判据与整理真的发号结果完全一致():
    """穷举差分：判据必须与「整理跑完之后还是不是一个断号」**逐例相等**。

    这条是本文件的**总闸**：下面几条钉的是具体形状，这里钉的是
    「判据 == 真值」这个性质本身 —— 少一个组合对不上就红。
    """
    import itertools

    universe = [1, 2, 3, 4, 5]
    mismatched = []
    total = 0
    for k in range(len(universe) + 1):
        for present in itertools.combinations(universe, k):
            for r in range(len(present) + 1):
                for posted in itertools.combinations(present, r):
                    total += 1
                    rows = [
                        svc_organize._VoucherRow(
                            f"记{s:04d}", date(2026, 1, 5),
                            "POSTED" if s in posted else "UNPOSTED")
                        for s in present
                    ]
                    if not rows:
                        continue
                    _fixable, stuck = svc_organize.fixable_gaps(rows)
                    truth = _truth_stuck(rows)
                    if set(stuck) != set(truth):
                        mismatched.append((present, sorted(posted), stuck, truth))
                    # **附带钉住一条恒等式**：一个断号整理之后仍然空着 ⟹
                    # 一定有一张已记账的凭证占着比它更大的号。
                    #
                    # 这条恒等式是「它后面有已记账的凭证占着号」这句话**能说**
                    # 的全部依据 —— 恒等式一旦破了，那句话就必须改成按事实
                    # 分支（``_gap_advice`` 里就是这么写的）。
                    posted_seqs = [s for s in present if s in posted]
                    for g in stuck:
                        gs = svc_organize._seq_of(g)
                        assert any(s > gs for s in posted_seqs), (
                            f"断号 {g} 整理之后仍然空着，却没有任何已记账的凭证"
                            f"占着比它更大的号：present={present} posted={posted}")
    assert not mismatched, (
        f"{len(mismatched)}/{total} 种号段配置上判据与真值不一致，"
        f"例如：{mismatched[:3]}")


def test_号段顶端是未记账凭证时断号判为补得上(db, book, periods, make_voucher,
                                           admin_user):
    """复核方复现 A 的那个形状：期间里只剩 记0003（未记账）。

    断号是 记0001、记0002 —— 但整理把 记0003 压成 记0001 之后，
    **整段只剩 1 号，断号消失**。旧判据说 记0002「补不上」，
    于是闸门把用户引向「手工补一张假凭证」或「反结账」这两条死路。
    """
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")])
    # 前两张被删了 → 期间里只剩 记0003（未记账），号段顶端那张就是它
    delete_voucher(db, v1, admin_user)
    delete_voucher(db, v2, admin_user)
    db.commit()

    period = db.get(Period, periods[1].id)
    rows = _rows_of(db, period)
    assert [r.no for r in rows] == ["记0003"], [r.no for r in rows]
    assert svc_organize.check_period(db, period)["gaps"] == ["记0001", "记0002"]

    fixable, stuck = svc_organize.fixable_gaps(rows)
    assert stuck == [], f"误报：这个洞整理一次就补得上，却说补不上：{stuck}"
    assert fixable == ["记0001", "记0002"], fixable

    # 闸门（记账 / 结账）：说「点一次整理凭证就能补上」
    with pytest.raises(ValueError) as ei:
        svc_organize.assert_tidy(db, [period])
    db.rollback()
    msg = str(ei.value)
    assert "点一次「整理凭证」就能补上" in msg, msg
    # **不许**出现那两句把用户引向死路的话
    assert "补不上" not in msg, f"仍在说「补不上」：{msg}"
    assert "它后面有已记账的凭证占着号" not in msg, msg
    assert "手工补一张" not in msg, msg
    assert "反结账" not in msg, msg

    # 而且真的整理一次，洞确实消失
    svc_organize.apply_organize(db, book_id=book.id, period_id=period.id,
                                 strategy="RENUM", user=admin_user)
    db.commit()
    rep = svc_organize.check_period(db, period)
    assert rep["gaps"] == [], f"整理之后洞还在：{rep['gaps']}"
    assert rep["clean"] is True, rep


def test_删掉凭证不再被误拦(client, auth, db, book, periods, make_voucher):
    """复核方复现 B：删除入口把一次**合法**的删除误拦成 400。

    删完只剩 记0003 —— 整理一次就是 记0001，号段干干净净。
    旧判据说这里「补不上」，于是 400 把用户挡在门外。
    """
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")])

    # 先删 记0002：删完是 {记0001, 记0003}，整理一次 → {记0001, 记0002}，无洞
    r = client.delete(f"/api/books/{book.id}/vouchers/{v2.id}", headers=auth)
    assert r.status_code == 200, r.text

    # 再删 记0001：删完只剩 {记0003}，整理一次 → {记0001}，**也无洞**
    r = client.delete(f"/api/books/{book.id}/vouchers/{v1.id}", headers=auth)
    assert r.status_code == 200, (
        f"删除被误拦了：{r.status_code} {r.text} —— "
        "删完的状态整理一次就没有断号，不是死路")

    # 反向确认：洞真的没了
    period = db.get(Period, periods[1].id)
    db.expire_all()
    rows = _rows_of(db, period)
    assert [r.no for r in rows] == ["记0003"], [r.no for r in rows]
    fixable, stuck = svc_organize.fixable_gaps(rows)
    assert stuck == [], f"判据说补不上：{stuck}"


def test_真会留死路的删除仍然被拦(client, auth, db, book, periods, make_voucher):
    """对照组（对称事实的另一半）：**真的**会留死路的删除，**仍然**要拦。

    期间里有 记0001、记0002（未记账）和 记0003（**已记账**）。
    删掉 记0002 之后只剩 {记0001, 记0003} —— 整理只给未记账的凭证重新发号，
    记0003 动不了，于是 记0002 空着、洞一直在原地。这才是死路。
    """
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")],
                      posted=True)

    # 先删 记0001 → 剩 {记0002, 记0003}：整理能补上（记0002 会被压成 记0001），
    # 放行
    r = client.delete(f"/api/books/{book.id}/vouchers/{v1.id}", headers=auth)
    assert r.status_code == 200, r.text

    # 再删 记0002 → 只剩 {记0003}（已记账）：洞补不上，**必须拦**
    r = client.delete(f"/api/books/{book.id}/vouchers/{v2.id}", headers=auth)
    assert r.status_code == 400, f"真死路却没有被拦：{r.status_code} {r.text}"
    assert "补不上" in r.text, r.text
    assert "反结账" in r.text, r.text


def test_没有已记账凭证时不说它后面有已记账的凭证占着号():
    """文案只陈述**成立**的事实。

    「它后面有已记账的凭证占着号」这句话的前提是真的有已记账的凭证。
    那个期间一张已记账的凭证都没有时说这句话，就是把用户引向
    「手工补一张假凭证」或「反结账」这两条死路。

    直接调文案函数（而不是去凑一个现实中凑不出的号段状态）：
    事实由参数给足，文案必须与事实一致。
    """
    rows = [svc_organize._VoucherRow("记0003", date(2026, 1, 5), "UNPOSTED")]
    msg = svc_organize._gap_advice("2026-01", rows, ["记0002"], ["记0002"])
    assert "它后面有已记账的凭证占着号" not in msg, msg
    assert "手工补一张" not in msg, msg
    assert "反结账" not in msg, msg

    # 对称事实的另一半：确实有已记账的凭证占在后面时，这两样都要在
    rows = [svc_organize._VoucherRow("记0001", date(2026, 1, 5), "UNPOSTED"),
            svc_organize._VoucherRow("记0003", date(2026, 1, 6), "POSTED")]
    msg = svc_organize._gap_advice("2026-01", rows, ["记0002"], ["记0002"])
    assert "它后面有已记账的凭证占着号" in msg, msg
    assert "反结账" in msg, msg


def test_整理计划与判据认同同一份发号规则(db, book, periods, make_voucher,
                                       admin_user):
    """判据模拟的整理结果，必须与**真的** ``apply_organize`` 一致。

    判据与执行各写一份发号规则的话，判据就会在整理明明能补上的时候
    说补不上 —— 那正是本条缺陷的根。

    **这条同时把整条链闭合**：
    「判据 == 真值」由上面那条穷举用例钉（真值是测试里独立写的），
    「模拟 == 现实」由这里钉 —— 于是
    **判据 == 真值 == 现实**，三者指向同一个答案。
    """
    period = db.get(Period, periods[1].id)
    v1 = make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")])
    v2 = make_voucher("2026-01-06", dr=[("1001", 20, "x")], cr=[("5001", 20, "x")])
    v3 = make_voucher("2026-01-07", dr=[("1001", 30, "x")], cr=[("5001", 30, "x")],
                      posted=True)
    # 制造一个**整理补不上**的洞：删掉 记0002，而 记0003 已记账占着号
    # （已记账的凭证删不掉 —— 那是另一道闸）
    delete_voucher(db, v2, admin_user)
    db.commit()

    rows = _rows_of(db, period)
    before_gaps = svc_organize.check_period(db, period)["gaps"]
    assert before_gaps == ["记0002"], before_gaps
    _fixable, stuck_before = svc_organize.fixable_gaps(rows)
    assert stuck_before == ["记0002"], stuck_before

    simulated = svc_organize._analyze_rows(
        svc_organize._organize_resulting_rows(rows))["gaps"]

    svc_organize.apply_organize(db, book_id=book.id, period_id=period.id,
                                 strategy="RENUM", user=admin_user)
    db.commit()
    real = svc_organize.check_period(db, period)["gaps"]
    assert simulated == real, (
        f"判据模拟的整理结果与真的整理不一致：模拟 {simulated} / 真库 {real}")

    # **链闭合**：判据说「补不上」的那些号，正好就是真跑一次整理之后
    # **仍然是断号**的那些号 —— 一个不多，一个不少。
    assert set(stuck_before) == {g for g in before_gaps if g in real}, (
        f"判据与现实给了两个答案：判据说 {stuck_before}，"
        f"真跑一次整理之后仍然是断号的是 "
        f"{[g for g in before_gaps if g in real]}")
