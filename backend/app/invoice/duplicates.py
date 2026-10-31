"""发票 ↔ 账上分录的「可能重复记账」排查。

**为什么要有这件事。**

有些发票是**手工先记了账**，后来才把票传上来导入 —— 这时再生成一张凭证，
同一笔钱就在账上记了两遍。系统拦不住这种重复：发票号码是这张票的**唯一
身份**，而手工记的那笔账根本没有号码可比（号码级拦截见
``service._existing_invoice_numbers``）。

所以这里**只提示，不拦**。

**判据：同月 + 同额 + 同向 + 同结算科目。**

* **期间**：按发票的**开票日期**落在哪个会计期间。制证时凭证日期就是开票
  日期（``service.commit_batch``），同一张票在账上就是那个月的事。
* **金额**：发票的**价税合计**（``resolution.total``），也就是将来写进
  凭证里那条结算分录的金额。
* **方向**：进项找**贷方**（付钱，钱从账户出去）、销项找**借方**（收钱，
  钱进账户）—— 与制证时那条结算分录的方向**完全一致**
  （``service._input_entries`` / ``service._output_entries``）。
* **科目**：还要与这张发票的**结算科目**相同。少了这一条，「同额同向」会
  把不相干的钱也算进来 —— 10 月的一笔销售回款，贷方也是 1000 元。

**为什么跨期不算。**

管理费这类按月开票的场景，**每个月金额都一样**（2026-10-22 生产实测：
合同 #76 每月都是 906.93 / 3867.96 元）。把相邻期间也纳入，等于天天报警，
提示就没人看了。用户要的是「那个月账上有没有对应分录」，就按那个月算。

**为什么这些命中不能当成「就是同一笔」。**

2026-10-22 的教训：判重必须用**唯一身份**（发票号码），业务特征相同并不
代表是同一张票 —— 管理费每月同额，两个月各开一张是两张票，不是重复。所以
这里给出的是**待人工复核的提示**，用户自己判断；系统不拿它去挡导入。

（对账那条线上的同类口径见 ``app/bankstatement/matching.py``：那边也是
「同额同向」起步，但它是流水 ↔ 分录的一对一分配，与这里的「提示」不是
一回事。）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting.util import money
from app.models import Entry, Period, Subject, Voucher

#: 发票类型 → **结算方向**。
#:
#: 进项是「付钱」：钱从账户出去，结算那条分录是**贷方**（贷 银行存款）。
#: 销项是「收钱」：钱进账户，结算那条分录是**借方**（借 应收账款）。
#:
#: 必须与制证时那条结算分录同向（``service._input_entries`` /
#: ``service._output_entries``）—— 方向反了就不是同一笔钱了。
SETTLEMENT_DIRECTION = {"INPUT": "CREDIT", "OUTPUT": "DEBIT"}


def settlement_direction(invoice_type: str) -> str:
    """发票类型 → 结算方向；类型还没定（UNKNOWN）时给空串。"""
    return SETTLEMENT_DIRECTION.get(str(invoice_type or ""), "")


@dataclass(frozen=True)
class Probe:
    """要问账上的一个问题：「这个月有没有同额同向同科目的分录」。

    四件套缺一不可：期间、方向、金额、科目。
    """

    period_ord: int
    direction: str
    amount: Decimal
    subject_id: int

    @property
    def key(self) -> tuple[int, str, Decimal, int]:
        return (self.period_ord, self.direction, self.amount, self.subject_id)


def period_ords(db: Session, book_id: int, dates: Iterable[date]) -> dict[tuple[int, int], int]:
    """(年, 月) → 账套内的期间序号。

    **一次查完，不逐张发票查** —— 一个批次几百张发票，逐张查就是几百次
    数据库往返（与 ``service.subjects_by_id`` 同一条理由）。

    找不到的（那一天不在本账套的会计期间范围内）**不在结果里**：
    那种票本来就入不了账，不该由这里报错。
    """
    pairs = {(d.year, d.month) for d in dates if d is not None}
    if not pairs:
        return {}
    rows = db.execute(
        select(Period.year, Period.month, Period.ord).where(
            Period.book_id == book_id,
            Period.year.in_({year for year, _ in pairs}),
            Period.month.in_({month for _, month in pairs}),
        )
    ).all()
    return {(year, month): ord_ for year, month, ord_ in rows}


def find_matches(
    db: Session,
    book_id: int,
    probes: Iterable[Probe],
    exclude_voucher_ids: Iterable[int] = (),
) -> dict[tuple[int, str, Decimal, int], list[dict[str, Any]]]:
    """按「期间 + 方向 + 金额 + 科目」四件套找账上已有的分录。

    **一次查完，不按发票逐个查** —— 同上，一个批次几百张发票逐张查就是几百次
    数据库往返。

    ``exclude_voucher_ids`` 是**本批次自己生成的凭证**：同一批里两张金额
    相同的发票不是重复记账（那是两张票），拿自己刚生成的凭证去提示自己是
    噪声。

    返回：键是 ``Probe.key``，值是命中的分录清单（按凭证日期、凭证号排序）。
    """
    items: list[Probe] = list(probes)
    if not items:
        return {}
    stmt = (
        select(Entry, Voucher)
        .join(Voucher, Voucher.id == Entry.voucher_id)
        .where(
            Entry.book_id == book_id,
            Entry.period_ord.in_({p.period_ord for p in items}),
            Entry.direction.in_({p.direction for p in items}),
            Entry.amount.in_({p.amount for p in items}),
            Entry.subject_id.in_({p.subject_id for p in items}),
        )
    )
    exclude = {int(v) for v in exclude_voucher_ids if v}
    if exclude:
        stmt = stmt.where(Voucher.id.notin_(exclude))
    rows = db.execute(stmt).all()
    if not rows:
        return {}
    # 命中的科目未必是这张发票自己用的那些（手工记账用的科目五花八门），
    # 所以科目表在这里另查一次 —— 仍然是一次，不按分录逐条查。
    subjects = {
        row.id: row
        for row in db.scalars(
            select(Subject).where(Subject.id.in_({entry.subject_id for entry, _ in rows}))
        ).all()
    }
    hits: dict[tuple[int, str, Decimal, int], list[dict[str, Any]]] = {}
    for entry, voucher in rows:
        amount = money(entry.amount)
        key = (entry.period_ord, entry.direction, amount, entry.subject_id)
        subject = subjects.get(entry.subject_id)
        hits.setdefault(key, []).append({
            "voucher_id": voucher.id,
            "voucher_no": voucher.voucher_no,
            "voucher_date": voucher.voucher_date.isoformat() if voucher.voucher_date else "",
            # 已记账 / 未记账：用户手工做的那笔常常还没过账，但**重复的
            # 风险一样存在**（那张凭证迟早会被记账）。所以两种都列出来，
            # 让用户自己看是哪一张。
            "voucher_status": voucher.status,
            "voucher_source": voucher.source,
            "subject_id": entry.subject_id,
            "subject_code": subject.code if subject else "",
            "subject_name": subject.name if subject else "",
            "direction": entry.direction,
            "amount": format(amount, "f"),
            "summary": entry.summary or "",
        })
    for found in hits.values():
        found.sort(key=lambda item: (item["voucher_date"], item["voucher_no"], item["voucher_id"]))
    return hits


def match_summary(matches: Sequence[dict[str, Any]]) -> str:
    """把命中的分录拼成一句人话（给日志/接口摘要用；界面上另有一份渲染）。"""
    parts = []
    for item in matches:
        subject = " ".join(x for x in (item.get("subject_code"), item.get("subject_name")) if x)
        parts.append(
            f"{item.get('voucher_no', '')}（{item.get('voucher_date', '')}，"
            f"{'借' if item.get('direction') == 'DEBIT' else '贷'} {subject} "
            f"{item.get('amount', '')}）"
        )
    return "、".join(parts)
