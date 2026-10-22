"""账簿查询：总账、明细账、科目余额表、试算平衡。

全部基于 app.ledger.engine 的派生量，不存在第二套余额计算。
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.ledger.engine import ZERO, Figures, LedgerEngine, display_side
from app.models import Book, Entry, Period, Subject, Voucher

CATEGORY_ORDER = {"ASSET": 0, "LIABILITY": 1, "EQUITY": 2, "COST": 3, "PROFIT": 4}


def _entry_stmt(book_id: int, include_unposted: bool) -> Select:
    stmt = (
        select(Entry, Voucher, Subject)
        .join(Voucher, Voucher.id == Entry.voucher_id)
        .join(Subject, Subject.id == Entry.subject_id)
        .where(Entry.book_id == book_id)
    )
    if not include_unposted:
        stmt = stmt.where(Voucher.status == "POSTED")
    return stmt


def subject_tree(db: Session, book_id: int, year: int) -> list[Subject]:
    """按会计科目表顺序返回**某个年度**的所有科目（含非明细级、**含停用**）。

    刻意不过滤 ``active``：停用的语义是「不让再选它」，不是「删掉它」——
    老凭证还指着它，账还得算得出来、还得看得见。过滤掉的话，历史余额
    在界面上直接消失，用户会以为账丢了。

    写路径另有闸门（``accounting/vouchers.py`` 建凭证时校验科目可用），
    这里是只读展示，不该再拦一道。

    **年度是必填的**：科目表每个年度各有一份（迁移 0032），
    2026 的 1002 和 2027 的 1002 是两行不同的记录。不圈定年度，
    拿到的就是「两个年度的科目混在一起」—— 同一个编码出现两次。
    """
    return list(
        db.scalars(
            select(Subject)
            .where(Subject.book_id == book_id, Subject.year == year)
            .order_by(Subject.category, Subject.code)
        ).all()
    )


def subjects_for_periods(db: Session, book_id: int, periods: list[Period]) -> list[Subject]:
    """区间涉及哪几个年度，就取哪几个年度的科目表（**并集**）。

    科目表每个年度各有一份（迁移 0032），而跨年区间要报的是「同一个编码
    在这段区间里怎么动的」—— 两个年度的编码是**两行不同的记录**，
    只取一张表，区间发生额会凭空少掉另一年那半截。

    排序按 (类别, 编码, 年度)：单年时与 ``subject_tree`` 逐字一致
    （年度是常量），跨年时同一编码的各年度挨在一起，便于按编码归并。
    """
    years = sorted({int(p.year) for p in periods})
    if not years:
        return []
    return list(
        db.scalars(
            select(Subject)
            .where(Subject.book_id == book_id, Subject.year.in_(years))
            .order_by(Subject.category, Subject.code, Subject.year)
        ).all()
    )


# --------------------------------------------------------------------- 明细账


def subject_of_book(db: Session, book_id: int, subject_id: int,
                    year: int | None = None) -> Subject:
    """取**属于本账套**的科目，取不到就 404。

    以前这里是裸的 ``db.get(Subject, subject_id)``，不带 book_id 约束 ——
    同一个文件里 ``general`` 校验了期间归属，``detail`` 却不校验科目，
    两种口径。后果不是泄露金额（分录查询自带 Entry.book_id 过滤），
    而是别的账套的科目元数据（编码、名称、余额方向）会跨账套参与计算，
    页面看着正常、实际是空账，用户会以为这个科目真的一分钱没有。

    更糟的是传一个**不存在**的 id：``db.get`` 返回 None，
    后面 ``display_side(None, ...)`` 直接 AttributeError → 500。
    用户手输一个错 id 就能让接口报服务器错误，而不是「科目不存在」。

    这里不拦 ``is_leaf``：上级科目是合法 ID，点它的小计行应当看到
    空账而不是 404。末级性由调用方在需要时自己判断。

    ``year`` 给了就只认**那个年度**的科目：科目表每个年度各有一份，
    2026 的 1002 和 2027 的 1002 是两行。不圈年度的话，
    「看 2027 年的明细账」可以传一个 2026 年的科目 id 进来。
    """
    subject = db.get(Subject, subject_id)
    if subject is None or subject.book_id != book_id:
        raise HTTPException(404, "科目不存在")
    if year is not None and subject.year != year:
        raise HTTPException(404, "科目不存在")
    return subject


def _detail_account_rows(
    node: Subject,
    *,
    opening: Decimal,
    range_periods: list[Period],
    entries_by_period: dict[int, list[tuple[Entry, Voucher]]],
    month_moves: dict[int, tuple[Decimal, Decimal]],
    ytd_moves: dict[int, tuple[Decimal, Decimal]],
    is_leaf: bool,
    from_period: Period,
    full_year: bool = False,
) -> list[dict]:
    """**一个科目的明细账行**：期初 + 逐笔 + 本月合计 + 累计。

    屏幕（``detail_ledger``）与账簿成册（``detail_ledger_book``）共用这一份。
    余额方向、逐月合计、累计的口径只有这一处实现 —— 两处各写一遍，
    迟早有一处算错，而纸上的账比屏幕上的更难被发现。

    参数都已按**本科目**收窄（键是期间 ord），调用方负责投影。
    """
    rows: list[dict] = []

    def account_row(kind: str, period: Period, **values) -> dict:
        row = {
            "kind": kind,
            "subject_id": node.id,
            "subject_code": node.code,
            "subject_name": node.name,
            "subject_level": node.level,
            "voucher_no": "",
            "voucher_date": None,
            "period": f"{period.year}-{period.month:02d}",
            "summary": "",
            "debit": ZERO,
            "credit": ZERO,
            "direction": "平",
            "balance": None,
        }
        row.update(values)
        return row

    running = opening
    side, amount = display_side(node, running)
    rows.append(account_row(
        "OPENING", from_period,
        voucher_no="期初余额", summary="期初余额", direction=side, balance=amount))

    for period in range_periods:
        if is_leaf:
            for entry, voucher in entries_by_period.get(period.ord, []):
                debit = entry.amount if entry.direction == "DEBIT" else ZERO
                credit = entry.amount if entry.direction == "CREDIT" else ZERO
                running += debit - credit
                side, amount = display_side(node, running)
                rows.append(account_row(
                    "ENTRY", period,
                    voucher_id=voucher.id,
                    voucher_no=voucher.voucher_no,
                    voucher_date=voucher.voucher_date.isoformat(),
                    summary=entry.summary,
                    debit=debit,
                    credit=credit,
                    direction=side,
                    balance=amount,
                    status=voucher.status,
                ))

        month_debit, month_credit = month_moves.get(period.ord, (ZERO, ZERO))
        ytd_debit, ytd_credit = ytd_moves.get(period.ord, (ZERO, ZERO))
        if not is_leaf:
            # 父级本身没有分录，它的余额按**下级汇总的发生额**推进。
            running += month_debit - month_credit
        balance_side, balance_amount = display_side(node, running)
        rows.append(account_row(
            "MONTH_TOTAL", period, summary="本月合计",
            debit=month_debit, credit=month_credit,
            direction=balance_side, balance=balance_amount))
        # 累计行的标签与总账**同一套规则**：整年打印时 12 月那行写「本年累计」，
        # 其余写「累　　计」（两个字之间是**两个全角空格** U+3000，用友一律
        # 按四字宽排版，普通空格只有半字宽，与样张并排看就是两个字的距离）。
        rows.append(account_row(
            "YTD_TOTAL", period,
            summary="本年累计" if (full_year and period.month == 12) else "累　　计",
            debit=ytd_debit, credit=ytd_credit,
            direction=balance_side, balance=balance_amount))
    return rows


def detail_ledger(
    db: Session,
    book_id: int,
    subject_id: int,
    from_period: Period,
    to_period: Period,
    include_unposted: bool = False,
    from_subject_id: int | None = None,
    to_subject_id: int | None = None,
) -> dict:
    """明细账：末级逐笔展示；父级按科目树汇总并展开所选子科目范围。"""
    # 科目表每个年度各有一份 —— 明细账看的是**区间所在年度**的那张表。
    year = to_period.year
    subject = subject_of_book(db, book_id, subject_id, year=year)
    if from_period.ord > to_period.ord:
        raise HTTPException(400, "起始期间不能晚于结束期间")

    eng = LedgerEngine.for_ledger(db, book_id, include_unposted)
    periods = eng.periods()
    range_periods = [p for p in periods if from_period.ord <= p.ord <= to_period.ord]
    if not range_periods:
        raise HTTPException(400, "所选期间范围内没有会计期间")
    # 整年 = 一个完整会计年度。整年时 12 月那条累计行写「本年累计」，
    # 其余写「累　　计」—— 与总账同一套规则（见 _detail_account_rows）。
    full_year = (range_periods[0].year == range_periods[-1].year
                 and range_periods[0].month == 1 and range_periods[-1].month == 12)
    first_period_by_year: dict[int, Period] = {}
    for period in periods:
        first_period_by_year.setdefault(period.year, period)

    all_subjects = subject_tree(db, book_id, year)
    children: dict[int, list[Subject]] = defaultdict(list)
    by_id = {s.id: s for s in all_subjects}
    for child in all_subjects:
        if child.parent_id in by_id:
            children[child.parent_id].append(child)
    for siblings in children.values():
        siblings.sort(key=lambda s: (s.sort_no, s.code))

    comprehensive = bool(children.get(subject.id))
    selected_branches: list[Subject] = []
    if comprehensive:
        siblings = children[subject.id]
        start_subject = subject_of_book(db, book_id, from_subject_id, year=year) if from_subject_id else siblings[0]
        end_subject = subject_of_book(db, book_id, to_subject_id, year=year) if to_subject_id else siblings[-1]
        if start_subject.parent_id != subject.id or end_subject.parent_id != subject.id:
            raise HTTPException(400, "起止科目必须是所选父科目的直接下级科目")
        sibling_index = {s.id: i for i, s in enumerate(siblings)}
        start_index = sibling_index[start_subject.id]
        end_index = sibling_index[end_subject.id]
        if start_index > end_index:
            raise HTTPException(400, "起始科目不能排在结束科目之后")
        selected_branches = siblings[start_index:end_index + 1]
    else:
        if from_subject_id is not None or to_subject_id is not None:
            raise HTTPException(400, "起止科目范围仅适用于有下级科目的父科目")

    included_nodes: set[int] = set()
    selected_leaf_ids: list[int] = []
    branch_order: dict[int, list[Subject]] = {}

    def collect(node: Subject) -> None:
        included_nodes.add(node.id)
        node_children = children.get(node.id, [])
        branch_order[node.id] = node_children
        if not node_children:
            selected_leaf_ids.append(node.id)
            return
        for child in node_children:
            collect(child)

    included_nodes.add(subject.id)
    branch_order[subject.id] = selected_branches if comprehensive else []
    if comprehensive:
        for branch in selected_branches:
            collect(branch)
    else:
        selected_leaf_ids = [subject.id]

    # ---- 跨年区间：科目表每个年度各有一份 --------------------------------
    #
    # 行结构用**止期**那张表（报告的「当前年」视角），而分录挂的是**各自
    # 年度**的那一行：2026 年的分录指向 2026 年的科目 id，拿 2027 的 id
    # 去查一条都匹配不到 —— 区间发生额会凭空少掉一整年。
    #
    # 所以按**编码**把各年度的科目对上：
    #   · ``id_to_row``：任意年度的科目 id → 止期那一行的 id（上卷走止期的树）
    #   · ``opening_id_of``：止期的科目 id → **取期初用的那一行**
    #     （期初是区间起点的余额，还在**起始年**那张表上）
    years_in_range = {p.year for p in range_periods}
    id_to_row: dict[int, int] = {}
    opening_id_of: dict[int, int] = {}
    query_ids: set[int] = set(selected_leaf_ids)
    if len(years_in_range) > 1:
        from_year = min(years_in_range)
        code_to_row = {s.code: s for s in all_subjects}
        for y in sorted(years_in_range):
            for s in subject_tree(db, book_id, y):
                target = code_to_row.get(s.code)
                if target is None:
                    continue
                id_to_row[s.id] = target.id
                if target.id in query_ids:
                    query_ids.add(s.id)
                if y == from_year and target.id in selected_leaf_ids:
                    opening_id_of[target.id] = s.id

    # 只计算本次范围内的末级科目。父级期初快照会包含所有子科目，不能直接拿来
    # 作为缩小范围后的期初余额；逐个汇总选中的末级科目才能排除范围外分支。
    eng.compute(sorted(query_ids), to_period.ord)
    opening_by_leaf = {
        leaf_id: eng.opening(opening_id_of.get(leaf_id, leaf_id), from_period.ord)
        for leaf_id in selected_leaf_ids
    }

    opening_by_node: dict[int, Decimal] = {}

    def opening_for_node(node_id: int) -> Decimal:
        node_children = branch_order.get(node_id, [])
        if node_children:
            opening = sum((opening_for_node(child.id) for child in node_children), ZERO)
        else:
            opening = opening_by_leaf.get(node_id, ZERO)
        opening_by_node[node_id] = opening
        return opening

    opening_for_node(subject.id)

    earliest_ytd_ord = min(first_period_by_year[p.year].ord for p in range_periods)
    monthly_moves: dict[tuple[int, int], list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    entries_by_leaf_period: dict[tuple[int, int], list[tuple[Entry, Voucher]]] = defaultdict(list)
    if selected_leaf_ids:
        stmt = (
            _entry_stmt(book_id, include_unposted)
            .where(Entry.subject_id.in_(sorted(query_ids)))
            .where(Entry.period_ord >= earliest_ytd_ord, Entry.period_ord <= to_period.ord)
            .order_by(Entry.period_ord, Voucher.voucher_date, Voucher.voucher_no, Entry.line_no)
        )
        parent_by_id = {s.id: s.parent_id for s in all_subjects}
        for entry, voucher, _subj in db.execute(stmt).all():
            debit = entry.amount if entry.direction == "DEBIT" else ZERO
            credit = entry.amount if entry.direction == "CREDIT" else ZERO
            # **折算到止期那一行的 id**（跨年区间）：分录挂的是各自年度的
            # 科目行，上卷与展示都走止期那张树。
            entry_id = id_to_row.get(entry.subject_id, entry.subject_id)
            current = entry_id
            while current in included_nodes:
                totals = monthly_moves[(current, entry.period_ord)]
                totals[0] += debit
                totals[1] += credit
                parent_id = parent_by_id.get(current)
                if parent_id is None:
                    break
                current = parent_id
            if entry.period_ord >= from_period.ord:
                entries_by_leaf_period[(entry_id, entry.period_ord)].append((entry, voucher))

    ytd_moves: dict[tuple[int, int], tuple[Decimal, Decimal]] = {}
    ytd_by_node = {node_id: [ZERO, ZERO] for node_id in included_nodes}
    current_year = None
    for period in periods:
        if period.ord < earliest_ytd_ord or period.ord > to_period.ord:
            continue
        if period.year != current_year:
            ytd_by_node = {node_id: [ZERO, ZERO] for node_id in included_nodes}
            current_year = period.year
        for node_id in included_nodes:
            month = monthly_moves[(node_id, period.ord)]
            ytd_by_node[node_id][0] += month[0]
            ytd_by_node[node_id][1] += month[1]
            if period.ord >= from_period.ord:
                ytd_moves[(node_id, period.ord)] = tuple(ytd_by_node[node_id])

    rows: list[dict] = []
    subject_by_id = by_id

    def append_account(node_id: int) -> None:
        node = subject_by_id[node_id]
        node_children = branch_order.get(node_id, [])
        rows.extend(_detail_account_rows(
            node,
            opening=opening_by_node.get(node_id, ZERO),
            range_periods=range_periods,
            entries_by_period={
                p.ord: entries_by_leaf_period.get((node_id, p.ord), [])
                for p in range_periods
            },
            month_moves={
                p.ord: tuple(monthly_moves[(node_id, p.ord)]) for p in range_periods
            },
            ytd_moves={
                p.ord: ytd_moves[(node_id, p.ord)] for p in range_periods
            },
            is_leaf=not node_children,
            from_period=from_period,
            full_year=full_year,
        ))
        for child in node_children:
            append_account(child.id)

    append_account(subject.id)
    range_info = None
    if comprehensive:
        range_info = {
            "from": {"id": selected_branches[0].id, "code": selected_branches[0].code,
                     "name": selected_branches[0].name},
            "to": {"id": selected_branches[-1].id, "code": selected_branches[-1].code,
                   "name": selected_branches[-1].name},
        }

    return {
        "subject": {"id": subject.id, "code": subject.code, "name": subject.name,
                    "direction": subject.direction, "category": subject.category,
                    "level": subject.level, "is_leaf": not comprehensive},
        "comprehensive": comprehensive,
        "subject_range": range_info,
        "from_period": f"{from_period.year}-{from_period.month:02d}",
        "to_period": f"{to_period.year}-{to_period.month:02d}",
        "rows": rows,
    }


# --------------------------------------------------------------------- 明细账（用友式成册）


def detail_ledger_groups(
    db: Session,
    book_id: int,
    year: int,
    *,
    subject_id: int | None = None,
    from_subject_id: int | None = None,
    to_subject_id: int | None = None,
) -> list[dict]:
    """明细账的**册页单位**：``[{subject, leaves, comprehensive}, ...]``。

    明细账与总账的**册页单位一样是一级科目**（用户 2026-10-22 批注 3：
    「明细账的这个同理，改成一级科目」）。

    以前是末级科目：几百个末级科目各占一份，页眉翻过去全是「某某明细账」，
    与总账的册页结构对不上。现在一个一级科目一份：

    * 一级科目本身就是末级（1001 库存现金）→ 就是它自己那份明细。
    * 一级科目下面有下级（1101 短期投资）→ 那一份是**综合明细账**：
      下级科目按科目表顺序一块块排下来，每块首行写「编码 名称 期初余额」。

    * **不给科目** —— 全账套的一级科目，逐个成册（用友明细账的做法）。
    * **给了科目** —— 那一个科目就是一份：末级就它自己；父级则它下面那几个
      （可用起止科目再框一段，语义与屏幕上的「子科目范围」一致）。

    ``comprehensive`` 说的是「这一份里有没有下级科目」—— 有的话，各下级科目
    的期初行要写上「编码 名称」，否则纸上分不清哪几行属于哪个下级。
    """
    tree = subject_tree(db, book_id, year)
    children: dict[int, list[Subject]] = defaultdict(list)
    for s in tree:
        if s.parent_id:
            children[s.parent_id].append(s)
    for siblings in children.values():
        siblings.sort(key=lambda s: (s.sort_no, s.code))

    def leaves_of(node: Subject) -> list[Subject]:
        out: list[Subject] = []

        def walk(n: Subject) -> None:
            kids = children.get(n.id, [])
            if not kids:
                out.append(n)
                return
            for kid in kids:
                walk(kid)

        walk(node)
        return out

    if subject_id:
        root = subject_of_book(db, book_id, subject_id, year=year)
        kids = children.get(root.id, [])
        if not kids:
            return [{"subject": root, "leaves": [root], "comprehensive": False}]
        start = (subject_of_book(db, book_id, from_subject_id, year=year)
                 if from_subject_id else kids[0])
        end = (subject_of_book(db, book_id, to_subject_id, year=year)
               if to_subject_id else kids[-1])
        if start.parent_id != root.id or end.parent_id != root.id:
            raise HTTPException(400, "起止科目必须是所选父科目的直接下级科目")
        index = {s.id: i for i, s in enumerate(kids)}
        if index[start.id] > index[end.id]:
            raise HTTPException(400, "起始科目不能排在结束科目之后")
        leaves: list[Subject] = []
        for branch in kids[index[start.id]:index[end.id] + 1]:
            leaves.extend(leaves_of(branch))
        return [{"subject": root, "leaves": leaves, "comprehensive": True}]

    groups: list[dict] = []
    for s in tree:
        if s.level != 1:
            continue
        kids = children.get(s.id, [])
        if not kids:
            groups.append({"subject": s, "leaves": [s], "comprehensive": False})
        else:
            groups.append({"subject": s, "leaves": leaves_of(s),
                           "comprehensive": True})
    # **册页单位按科目编码排**（见 ``subject_code_order``）：一册账的目录
    # 按编码排，翻账的人才找得到。下级科目（综合明细账里的那几块）仍按
    # 科目表顺序 —— 前端「子科目范围」下拉也是那个顺序。
    groups.sort(key=lambda g: subject_code_order(g["subject"]))
    return groups


def detail_ledger_book(
    db: Session,
    book_id: int,
    groups: list[dict],
    from_period: Period,
    to_period: Period,
    include_unposted: bool = False,
    *,
    eng: LedgerEngine | None = None,
) -> list[dict]:
    """明细账成册：**一个册页单位一份**，返回 ``[{code, name, rows}, ...]``。

    ``groups`` 是 ``detail_ledger_groups`` 的结果：每个单位一个一级科目
    （或「仅当前科目」时的那一个科目），底下挂着它要印的末级科目。

    与总账成册同一个思路：**一本账 = 连续的若干个册页单位**，单位之间不插页、
    不分册，页码由渲染层按单位各自从 1 开始编。

    **取数是一次性的，不是一个科目一次。**
    科目表每个年度各有一份、跨年区间还要按编码对上（下面 id_to_row 那段），
    逐个科目调 ``detail_ledger`` 会把「查科目表」「查分录」「算期初」
    各做 N 遍 —— 一本明细账动辄几百个末级科目，那是几百次全账扫描。
    这里把科目表、分录、期初各取一次，再按科目切片。

    行本身由 ``_detail_account_rows`` 生成 —— 与屏幕上那本明细账
    **同一份实现**，余额方向、逐月合计、累计的口径不会分叉。
    """
    if from_period.ord > to_period.ord:
        raise HTTPException(400, "起始期间不能晚于结束期间")
    if not groups:
        return []

    # 这一册要印的全部末级科目（各单位合起来，各单位之间不重叠）。
    leaves: list[Subject] = []
    for group in groups:
        leaves.extend(group.get("leaves") or [])
    if not leaves:
        return []

    eng = eng or LedgerEngine.for_ledger(db, book_id, include_unposted)
    periods = eng.periods()
    range_periods = [p for p in periods if from_period.ord <= p.ord <= to_period.ord]
    if not range_periods:
        raise HTTPException(400, "所选期间范围内没有会计期间")
    full_year = (range_periods[0].year == range_periods[-1].year
                 and range_periods[0].month == 1 and range_periods[-1].month == 12)

    first_period_by_year: dict[int, Period] = {}
    for period in periods:
        first_period_by_year.setdefault(period.year, period)

    # ---- 各年度的科目表按**编码**对上 ---------------------------------
    #
    # 科目表每个年度各有一份（迁移 0032）：2026 年的分录挂在 2026 年的
    # 科目 id 上，2027 年的挂在 2027 年的 id 上，拿错年份的行去查就是
    # 一段凭空少掉的数。所以按编码把各年度的行对上，再把分录折算到
    # **止期**那一行（展示与汇总都走止期那张树）。
    years_in_range = sorted({p.year for p in range_periods})
    by_code: dict[str, dict[int, Subject]] = {}
    for y in years_in_range:
        for s in subject_tree(db, book_id, y):
            by_code.setdefault(s.code, {})[y] = s

    id_to_row: dict[int, int] = {}       # 任意年度的科目行 -> 止期那一行
    opening_id_of: dict[int, int] = {}   # 止期那一行 -> 取期初用的那一行
    query_ids: set[int] = set()
    first_year = years_in_range[0]
    for node in leaves:
        per_year = by_code.get(node.code) or {}
        row = per_year.get(to_period.year) or node
        for s in per_year.values():
            id_to_row[s.id] = row.id
            query_ids.add(s.id)
        if first_year in per_year:
            opening_id_of[row.id] = per_year[first_year].id

    leaf_ids = sorted({id_to_row.get(s.id, s.id) for s in leaves})

    # ---- 期初：引擎一次算完（共用实例，账簿聚合只跑一遍）--------------
    eng.compute(sorted(query_ids), to_period.ord)
    opening_by_leaf = {
        sid: eng.opening(opening_id_of.get(sid, sid), from_period.ord)
        for sid in leaf_ids
    }

    # ---- 分录：一次查完，再按科目切片 ---------------------------------
    earliest_ytd_ord = min(first_period_by_year[p.year].ord for p in range_periods)
    monthly_moves: dict[tuple[int, int], list[Decimal]] = defaultdict(lambda: [ZERO, ZERO])
    entries_by_leaf_period: dict[tuple[int, int], list[tuple[Entry, Voucher]]] = defaultdict(list)
    if query_ids:
        stmt = (
            _entry_stmt(book_id, include_unposted)
            .where(Entry.subject_id.in_(sorted(query_ids)))
            .where(Entry.period_ord >= earliest_ytd_ord, Entry.period_ord <= to_period.ord)
            .order_by(Entry.period_ord, Voucher.voucher_date, Voucher.voucher_no, Entry.line_no)
        )
        for entry, voucher, _subj in db.execute(stmt).all():
            debit = entry.amount if entry.direction == "DEBIT" else ZERO
            credit = entry.amount if entry.direction == "CREDIT" else ZERO
            entry_id = id_to_row.get(entry.subject_id, entry.subject_id)
            totals = monthly_moves[(entry_id, entry.period_ord)]
            totals[0] += debit
            totals[1] += credit
            if entry.period_ord >= from_period.ord:
                entries_by_leaf_period[(entry_id, entry.period_ord)].append((entry, voucher))

    # ---- 累计：每年 1 月归零，逐月累加 -------------------------------
    ytd_moves: dict[tuple[int, int], tuple[Decimal, Decimal]] = {}
    ytd_running: dict[int, list[Decimal]] = {}
    current_year = None
    for period in periods:
        if period.ord < earliest_ytd_ord or period.ord > to_period.ord:
            continue
        if period.year != current_year:
            current_year = period.year
            ytd_running = {sid: [ZERO, ZERO] for sid in leaf_ids}
        for sid in leaf_ids:
            month = monthly_moves[(sid, period.ord)]
            ytd_running[sid][0] += month[0]
            ytd_running[sid][1] += month[1]
            if period.ord >= from_period.ord:
                ytd_moves[(sid, period.ord)] = tuple(ytd_running[sid])

    # ---- 逐末级科目出行 ----------------------------------------------
    rows_by_leaf: dict[int, list[dict]] = {}
    for node in leaves:
        sid = id_to_row.get(node.id, node.id)
        rows_by_leaf[sid] = _detail_account_rows(
            node,
            opening=opening_by_leaf.get(sid, ZERO),
            range_periods=range_periods,
            entries_by_period={
                p.ord: entries_by_leaf_period.get((sid, p.ord), [])
                for p in range_periods
            },
            month_moves={
                p.ord: tuple(monthly_moves[(sid, p.ord)]) for p in range_periods
            },
            ytd_moves={
                p.ord: ytd_moves.get((sid, p.ord), (ZERO, ZERO)) for p in range_periods
            },
            is_leaf=True,
            from_period=from_period,
            full_year=full_year,
        )

    # ---- 按册页单位成册 ----------------------------------------------
    #
    # 一个单位 = 一个一级科目（综合的则把它下面几个末级科目排成几块）。
    # 纸上**没有「科目」列**（用友八列），所以综合明细账里每一块的首行
    # 要在摘要列写明是哪个下级科目，否则那几十行看不出来属于谁。
    books: list[dict] = []
    for group in groups:
        rows: list[dict] = []
        for leaf in group.get("leaves") or []:
            leaf_rows = rows_by_leaf.get(id_to_row.get(leaf.id, leaf.id), [])
            if not leaf_rows:
                continue
            if group.get("comprehensive"):
                leaf_rows = list(leaf_rows)
                leaf_rows[0]["summary"] = f"{leaf.code} {leaf.name} 期初余额"
            rows.extend(leaf_rows)
        root = group["subject"]
        books.append({"code": root.code, "name": root.name, "rows": rows})
    return books


# --------------------------------------------------------------------- 总账（用友式）

#: 总账的行型。用友的「总分类账」只有这四种行，没有别的：
#: 期初一行、每月两行（本月合计 / 累计）、期末一行。
KIND_OPENING = "OPENING"            # 上年结转
KIND_MONTH_TOTAL = "MONTH_TOTAL"    # 本月合计
KIND_YTD_TOTAL = "YTD_TOTAL"        # 累计
KIND_CLOSING = "CLOSING"            # 结转下年


def subject_code_order(subject: Subject) -> tuple:
    """册页单位按**科目编码数字**升序的排序键。

    科目表本身的顺序（``subject_tree`` 的 ``category, code``）是按会计科目表
    的**分类**排的：资产 → 所有者权益 → 负债 → 损益。照那个顺序打出来的册子，
    「本年利润(4103)」会排在「应付账款(2202)」前面 —— 翻账的人是**按编码
    找**的，这个目录看着是乱的（用户 2026-10-22）。

    册页单位是**一册账的目录**，目录按编码排：编码就是这一行账在纸上的地址。

    比较用**数字**而不是字符串：编码是数字串，按字符串比会得出「22」排在
    「2」前面这种反直觉的结果。非纯数字的编码（自定义科目）排在数字编码
    之后，按字典序。

    **只用于册页单位（一级科目）这一层**：综合明细账里下级科目的顺序仍是
    科目表顺序 —— 前端那个「子科目范围」下拉也是按科目表顺序列的，两边
    必须一致，否则用户在下拉里选的「起始/结束」在后端会被判成倒置。
    """
    code = str(subject.code or "")
    if code.isdigit():
        return (0, int(code), code)
    return (1, 0, code)


def general_ledger_subjects(db: Session, book_id: int, year: int) -> list[Subject]:
    """账套的**一级科目**（按科目编码数字升序）—— 一册总账的科目顺序。

    用友的「总账」是**总分类账**：一个一级科目一份，把它下面所有明细科目
    的分录汇总进去。所以总账的单位是**一级科目**，不是「一行一个科目」。

    以前的总账是「一行一个科目」的余额表形态 —— 那张表就是科目余额表，
    两处重复。现在总账改成用友形态，余额表形态由 ``/ledger/balance`` 承担。

    **顺序按科目编码**（见 ``subject_code_order``），不是科目表的分类顺序。
    """
    subjects = [s for s in subject_tree(db, book_id, year) if s.level == 1]
    return sorted(subjects, key=subject_code_order)


def general_ledger(
    db: Session,
    book_id: int,
    subject: Subject,
    from_period: Period,
    to_period: Period,
    include_unposted: bool = False,
    *,
    eng: LedgerEngine | None = None,
) -> dict:
    """总账（用友式）：**一个一级科目一份**，逐月展开的台账。

    行结构与用友的「总分类账」逐格对齐::

        上年结转     期初余额 + 方向
        01 本月合计  当月借方 / 贷方 + 月末余额 + 方向
        01 累计      累计借方 / 贷方
        ...
        12 本月合计
        12 本年累计
        结转下年     期末余额 + 方向

    **「累计」的口径是区间累计**（从区间首期起累加），所以
    「期初 + 累计借 − 累计贷 = 期末」在任何区间都恒成立。
    区间恰好是一个完整会计年度时，它就是用友的「本年累计」，两者逐格相等；
    区间不是整年时，标签退回「累计」，期初/期末也退回「期初余额」「期末余额」
    —— 写「上年结转」而实际不是上年末，是拿标签骗人。

    **取数按「期间所属年度」分段。** 科目表每个年度各有一份，跨年区间里
    2026 年的分录挂在 2026 年的科目 id 上、2027 年的挂在 2027 年的 id 上，
    拿错年份的行去算就是一段凭空少掉的数。所以每一段用它自己那年的那一行，
    段与段之间靠「上一年年末 = 下一年年初」这条结转关系接上。

    ``eng`` 可以传进来共用：**打一册总账**（账套全部一级科目）是逐个科目
    调本函数的，共用一个引擎实例，账簿聚合只跑一次；不传就自己建一个。
    """
    if from_period.ord > to_period.ord:
        raise HTTPException(400, "起始期间不能晚于结束期间")

    eng = eng or LedgerEngine.for_ledger(db, book_id, include_unposted)
    periods = eng.periods()
    range_periods = [p for p in periods if from_period.ord <= p.ord <= to_period.ord]
    if not range_periods:
        raise HTTPException(400, "所选期间范围内没有会计期间")

    # 区间涉及的每一年的科目表里，**同编码**的那一行。
    years = sorted({p.year for p in range_periods})
    by_year: dict[int, Subject] = {}
    for y in years:
        for s in subject_tree(db, book_id, y):
            if s.code == subject.code:
                by_year[y] = s
                break
    if not by_year:
        raise HTTPException(404, "科目不存在")

    first, last = range_periods[0], range_periods[-1]

    # **先把引擎热到区间最后一期。**
    #
    # 引擎的记忆化只在「已查到的上界 ≥ 本次上界」时才盖得住
    # （``LedgerEngine._pick_donor``）：从第一期开始逐期问的话，每一期都要
    # 重新发一次全账聚合 —— 12 个月就是 12 次。倒过来先问最后一期，
    # 后面每一期都是从那一份结果里切片，**不发 SQL**。
    warm = by_year.get(last.year) or by_year[years[-1]]
    eng.figures(warm.id, last.ord)

    # 逐期取数。``figures`` 取的是该科目在那一期的期初 / 当期发生额 / 期末，
    # 发生额**已由引擎上卷到各级父科目**（_cum_movements），所以一级科目
    # 那一行拿到的就是「它加上它所有下级」的合计 —— 正是用友总账要的口径。
    seg: dict[int, dict] = {}
    for p in range_periods:
        node = by_year.get(p.year)
        if node is None:
            continue
        f = eng.figures(node.id, p.ord)
        seg[p.ord] = {
            "opening": f.opening,
            "debit": f.period_dr,
            "credit": f.period_cr,
            "closing": f.closing,
        }

    full_year = (first.year == last.year and first.month == 1 and last.month == 12)
    opening_label = "上年结转" if full_year else "期初余额"
    closing_label = "结转下年" if full_year else "期末余额"

    def _blank(kind: str, period: Period, summary: str, **extra) -> dict:
        row = {
            "kind": kind,
            "period": period.code,
            "month": "", "day": "", "voucher_no": "",
            "summary": summary,
            "debit": None, "credit": None,
            "direction": "", "balance": None,
        }
        row.update(extra)
        return row

    rows: list[dict] = []

    # 期初行
    side, amount = display_side(subject, seg[first.ord]["opening"])
    rows.append(_blank(KIND_OPENING, first, opening_label,
                       direction=side, balance=amount))

    # 逐月两行
    cum_dr = cum_cr = ZERO
    for p in range_periods:
        s = seg.get(p.ord)
        if s is None:
            continue
        cum_dr += s["debit"]
        cum_cr += s["credit"]
        side, amount = display_side(subject, s["closing"])
        rows.append(_blank(KIND_MONTH_TOTAL, p, "本月合计",
                           month=f"{p.month:02d}",
                           debit=s["debit"], credit=s["credit"],
                           direction=side, balance=amount))
        # 年末那一期的累计行改用「本年累计」——用友的固定写法。
        #
        # 「累　　计」中间是**两个全角空格**（U+3000），不是普通空格：
        # 用友的标签一律按四字宽排版，普通空格排出来只有半字宽，字距对不上，
        # 与样张并排看就是两个字的距离。标签写在这里而不是各个渲染器里，
        # 是为了让打印件、Excel 与屏幕**逐字同一份**。
        cum_summary = "本年累计" if (full_year and p.month == 12) else "累　　计"
        rows.append(_blank(KIND_YTD_TOTAL, p, cum_summary,
                           month=f"{p.month:02d}",
                           debit=cum_dr, credit=cum_cr))

    # 期末行
    side, amount = display_side(subject, seg[last.ord]["closing"])
    rows.append(_blank(KIND_CLOSING, last, closing_label,
                       direction=side, balance=amount))

    return {
        "subject": {"id": subject.id, "code": subject.code, "name": subject.name,
                    "level": subject.level},
        "from_period": first.code,
        "to_period": last.code,
        "full_year": full_year,
        "rows": rows,
    }


# --------------------------------------------------------------------- 科目余额表 / 试算平衡


#: 科目余额表的「看哪些科目」三档。跟页面工具栏上的单选一一对应。
BALANCE_SUBJECT_SCOPES = ("top", "leaf", "all")


def filter_balance_rows(rows: list[dict], subject_scope: str = "all") -> list[dict]:
    """按科目范围过滤余额表行。三档：top=仅一级 / leaf=仅明细 / all=全部。

    **打印和导出必须跟屏幕同一个口径。** 屏幕上是「仅一级科目」、打出来却是
    1253 行明细，用户不会以为导出坏了，只会把纸上那一份当成对的 —— 余额表
    正是交给客户和审计的纸面数。页面过滤是纯前端的，导出走的是这里，两边
    各写一套迟早对不上，所以取值只有这一个函数。

    过滤**不动 check**：试算平衡那条 check 是按明细级算的（只累计叶子，
    否则父级汇总行会重复计数），而一级科目行本身就是下级的上卷 —— 屏幕上
    看到的加总与档位无关，打印/导出当然也不该随档位变。

    未知档位一律当 ``all``：这是查询参数，值错了宁可多给几行，也不能给一份
    少行的账 —— 缺行的账看不出问题，全行的账用户自己会发现多了什么。
    """
    if subject_scope == "top":
        return [r for r in rows if r["level"] == 1]
    if subject_scope == "leaf":
        return [r for r in rows if r["is_leaf"]]
    return list(rows)


def balance_sheet_of_accounts(
    db: Session,
    book_id: int,
    subjects: list[Subject],
    period: Period,
    include_unposted: bool = False,
    from_period: Period | None = None,
) -> tuple[list[dict], dict]:
    """科目余额表。返回 (行, 试算平衡校验结果)。

    试算平衡：
        Σ 借方发生额 == Σ 贷方发生额
        Σ 借方余额    == Σ 贷方余额

    **给 from_period 就是查一段连续区间**（止期仍是 ``period``）：

        期初余额 = 起始期的**上一期期末**
        发生额   = [起始期, 止期] 区间合计
        期末余额 = 期初 + 区间借方 − 区间贷方

    两处细节都不能省，否则区间账一查就错：

    * 区间发生额**不能用「本年累计相减」**。``dr_cum/cr_cum`` 是本年累计，
      区间跨过年界（2026-12 → 2027-02）时相减会得到乱七八糟的数，
      所以直接用 ``range_movements`` 逐期累加。
    * 区间期初**不能退回取止期自己的期初**。2026-10-13 审计 P0-03 记过这个
      缺陷：期初 + 区间发生额 = 期末 在行内恒成立，看起来永远自洽，
      只有与起始期上月的期末交叉核对才看得出来，而导出的 Excel 正是
      交给客户的纸面数。起始期是账套首期时引擎返回「首期之前」那张图
      （期初=首期期初、零发生额），closing 相同，两条路径语义一致。

    行结构与总账完全一致，``general_ledger`` 直接委托到这里 ——
    同一个口径只允许有一份实现。

    **区间倒置的闸门在这里，不在每个路由里。** 倒过来的一段区间不会让查询
    报错，只会算出一份「看起来自洽」的表：``range_movements`` 逐期按
    ``from_ord <= ord <= to_ord`` 过滤，倒置区间一条都留不下 → 发生额全 0；
    期初取的是起始期的上一期期末，期末 = 期初 + 0 − 0，于是每一行期初都等于
    期末、试算也平。用户看到的是一张有模有样、数字却全错的账 —— 而余额表
    正是交给客户和审计的纸面数（见 ``filter_balance_rows`` 的注释）。

    以前这道闸只有 ``/ledger/balance`` 的路由里有，``/ledger/general`` 只校验了
    起始期间属不属于本账套、没查 ord，两者却委托的是**同一个**函数：同一段
    倒置区间，余额表报 400、总账悄悄给出一份全零的错账。闸门下沉到共享实现
    之后，两张表、以及所有别的调用方都拿到同一条规则。
    """
    if from_period is not None and from_period.ord > period.ord:
        # 抛 HTTPException 而不是 ValueError：queries.py 本来就是 HTTP 耦合的
        # （``detail_ledger`` 的区间校验、``subject_of_book`` 的 404 都在这儿抛
        # HTTPException），而且本函数的调用方里有一批在 ``api/printing.py``：
        # 那个文件的 ``_render_scope``（总账打印/预览）只接 ExportTooLarge，
        # 裸 ValueError 会以 500 落给用户 —— 参数写错却回「服务器错误」，
        # 比返回一份错账还让人无从下手。
        raise HTTPException(400, "起始期间不能晚于结束期间")

    eng = LedgerEngine.for_ledger(db, book_id, include_unposted)
    is_range = from_period is not None and from_period.id != period.id

    # **行按「编码」归并**（引擎那一层做的事，理由见
    # ``LedgerEngine.merge_by_code``）：科目表每个年度各有一份，
    # 跨年区间里同一个编码在两个年度是**两行不同的记录、两个 id**，
    # 不归并的话区间发生额会凭空少掉另一年那半截。
    #
    # 只有一个年度时归并组里只有一行，与逐科目算逐字等价 ——
    # 口径只有一份实现，不会出现「单年一种算法、跨年另一种算法」。
    merged = eng.merge_by_code(subjects, period.ord,
                               from_period.ord if is_range else None)

    rows = []
    total_open_dr = total_open_cr = total_dr = total_cr = total_close_dr = total_close_cr = ZERO
    # 本年累计（ytd）也要在叶子级累计一次。``dr_cum`` / ``cr_cum`` 是**本年**
    # 累计发生额（每年 1 月归零），与「本期」那一列不是一回事 —— 期初/期末/
    # 本期三个维度都从 check 里取，唯独本年不在，合计行的「本年借方/贷方」
    # 就只能回退去表体里自己加，于是它随「科目范围」档位变（审计 P1-4）。
    total_ytd_dr = total_ytd_cr = ZERO

    for mf in merged:
        s = mf.subject
        o_signed = mf.opening
        m_dr, m_cr = mf.period_dr, mf.period_cr
        c_signed = mf.closing
        o_side, o_amt = display_side(s, o_signed)
        c_side, c_amt = display_side(s, c_signed)
        rows.append(
            {
                "subject_id": s.id,
                "parent_id": s.parent_id,
                "code": s.code,
                "name": s.name,
                "level": s.level,
                "category": s.category,
                "is_leaf": s.is_leaf,
                "opening_direction": o_side,
                "opening": o_amt,
                # 字段名沿用「本期」，区间口径下它装的是**区间**发生额。
                # 界面按当前口径改列名（本期发生额 / 区间借方），
                # 字段名不动是为了让总账、余额表共用同一份行结构。
                "period_debit": m_dr,
                "period_credit": m_cr,
                "ytd_debit": mf.ytd_dr,
                "ytd_credit": mf.ytd_cr,
                "direction": c_side,
                "closing": c_amt,
            }
        )
        # 只累计明细级科目，否则父级汇总行会重复计数
        if s.is_leaf:
            total_open_dr += o_signed if o_signed > 0 else ZERO
            total_open_cr += -o_signed if o_signed < 0 else ZERO
            total_dr += m_dr
            total_cr += m_cr
            total_ytd_dr += mf.ytd_dr
            total_ytd_cr += mf.ytd_cr
            total_close_dr += c_signed if c_signed > 0 else ZERO
            total_close_cr += -c_signed if c_signed < 0 else ZERO

    # 试算合计的**八个**维度都在这里了：期初借/贷、本期借/贷、本年借/贷、
    # 期末借/贷。合计行（Excel 导出、HTML 打印）**只认这份 check** ——
    # 它是叶子级、与档位无关的唯一一份实现。少了哪一维，哪一维就会退回去
    # 「按表体自己加」，于是合计随「科目范围」档位变（审计 P1-4 / 工程 P1-1）。
    check = {
        "period_balanced": total_dr == total_cr,
        "closing_balanced": total_close_dr == total_close_cr,
        "opening_balanced": total_open_dr == total_open_cr,
        "total_debit": total_dr,
        "total_credit": total_cr,
        "total_ytd_debit": total_ytd_dr,
        "total_ytd_credit": total_ytd_cr,
        "total_opening_debit": total_open_dr,
        "total_opening_credit": total_open_cr,
        "total_closing_debit": total_close_dr,
        "total_closing_credit": total_close_cr,
    }
    return rows, check


# --------------------------------------------------------------------- 凭证录入页的科目余额


def voucher_subject_balances(
    db: Session,
    book_id: int,
    period: Period,
    exclude_voucher_id: int | None = None,
    include_unposted: bool = True,
) -> dict[int, str]:
    """凭证录入页辅助记账用的「除本张以外」科目余额：subject_id -> 期末余额(借正贷负)。

    口径与科目余额表**完全同源**（同一个 ``Figures.closing``），差别只有三处，
    每处都是为了「正在录入的这一张」不干扰自己：

    - ``exclude_voucher_id``：剔除正在编辑的那张凭证。它会在 0.7 秒后自动保存，
      算进去的话余额会跟着手上这张一起动 —— 你往 1001 里填 500，余额就 +500，
      看着像「我刚存的凭证已经进账了」，其实是自己在跟自己说话。
    - ``include_unposted=True``：辅助记账要看的是**这个人眼下记到哪儿了**。
      默认排除未记账的话，月底集中记账前余额全是空的，这个提示就没意义了。
    - 只回金额不回方向：调用方要的是「一个数」，符号本身已经说明落在哪一边
      （正=借方、负=贷方）。再拆成「方向 + 绝对值」是给表格看的，
      塞进一个格子里只会让人多读一遍。

    上级科目一并算出来（引擎本来就上卷）：凭证只挂在末级上，但排凭证时
    「1002 银行存款」这种非末级也可能临时选上去看一眼余额。
    """
    eng = LedgerEngine.for_ledger(db, book_id, include_unposted,
                                   exclude_voucher_id=exclude_voucher_id)
    fmap = eng.compute(up_to_ord=period.ord)
    return {sid: str(f.closing) for sid, f in fmap.items()}
