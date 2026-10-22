"""凭证服务：新增 / 修改 / 删除 / 记账 / 反记账 / 查询 / 批量。"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Integer, Numeric, Select, and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.accounting import organize as svc_organize
from app.accounting.audit_helper import log, voucher_snapshot
from app.accounting.util import constraint_name, find_period, money, period_label
from app.models import Book, Entry, Period, Subject, SysUser, Voucher

# 凭证号在期间内唯一。撞号时数据库只会说「违反唯一约束」，看到的人
# 不知道该改哪个字段，所以把它翻成人话时要认得出**是哪一条**约束。
_NO_UNIQUE = "uq_voucher_period_no"


def _is_no_collision(exc: IntegrityError) -> bool:
    """这个 IntegrityError 是凭证号撞车吗？

    必须认约束而不是「凡 IntegrityError 都当撞号」：别的唯一约束
    （外键指向不存在、字段非空）撞上时，报「凭证号重复」是**假话**，
    用户会照着改一个根本没问题的字段。取约束名的工具在 util 里，
    与 organize.py 那一侧共用同一份判据。
    """
    if constraint_name(exc) == _NO_UNIQUE:
        return True
    # 少数驱动/包装下拿不到 diag，退回原文匹配 —— 宁可漏认成
    # 「别的错误」也不要误报成撞号（漏认的后果是 500，不是数据错）。
    return not constraint_name(exc) and _NO_UNIQUE in str(getattr(exc, "orig", exc))



# --------------------------------------------------------------------- 编号


# NO_PATTERN 的前缀组约束，逐字照抄（organize.py:39）。前缀不合这个形状时，
# **任何**凭证号都拆不出这个前缀，于是答案与库里有哪些号无关，恒为 001。
_PREFIX_SHAPE = re.compile(r"^[^\d]{1,4}$")


def _prefix_seq_expr(prefix: str):
    """「前缀 + 纯数字序号」整体的序号列；不匹配的行给 NULL。

    等价于应用层 ``split_no(no)`` 在 ``prefix == 前缀`` 那一支的行为：
    ``split_no`` 先 ``strip()`` 再要求整体匹配 ``^{prefix}(\\d+)$``，所以正则
    两头留 ``\\s*``、中间**不留** ——「记 005」里那个空格在 Python 侧会被吃进
    前缀组（前缀变成「记 」，不等于「记」）因而落选，这里同样落选。

    用 ``substring(no, pattern)`` 而不是 ``substring(no, 固定偏移)``：偏移量
    依赖「凭证字恰好几个汉字」这个假设，当年就是这么错的（见下面 docstring）。
    PostgreSQL 的两参 ``substring(text, text)`` 在正则含括号子表达式时返回
    **第一个括号子表达式**，于是这里拿到的就是纯数字那段，不需要在 Python
    侧再抠一次。
    """
    pattern = rf"^\s*{re.escape(prefix)}(\d+)\s*$"
    return func.cast(func.substring(Voucher.voucher_no, pattern), Numeric)


def next_voucher_no(db: Session, period: Period, prefix: str = "记") -> str:
    """期间内顺序编号。凭证号允许回收复用，因此取「当前最大值 + 1」即可。

    原来这行是 ``max(substring(voucher_no, 3))``，两个假设都不成立：

    **① 固定偏移 3。** ``substring(x, 3)`` 假设凭证字恰好 2 个汉字，
    从第 3 个字符起截。而 ``organize.NO_PATTERN`` 允许 1~4 个非数字字符
    作前缀，导入功能也允许用户自带凭证号。实测（修复前）：

        库里只有 银009   → 发出 记010（截断位置错了，序号对但前缀被吞）
        库里只有 记账凭证009 → 发出 记001

    **② max() 作用在字符串上。** PostgreSQL 对 text 的 max 是字典序，
    ``"099" > "1000"``。所以超过 999 张就回绕。实测（修复前）：

        库里只有 记100   → 发出 记001  ← 字典序 "00" 赢过 "100"
        库里只有 记199   → 发出 记100
        库里只有 记999   → 发出 记100

    两个缺陷叠在一起，探针 13 组里 10 组结果是错的。

    第一版修复改对了方向（必须按**整数**比），但落成了「把该期间全部凭证号
    拉回 Python 求 max」。单张创建是 4 次 SELECT，可批量导入在一个事务里
    逐张 ``create_voucher``（``import_max_vouchers`` 允许 5000 张），
    于是 N 张累计读回约 N(N-1)/2 行。

    现在把「按前缀分组取该前缀最大序号」下推成 SQL 聚合：**正确性决策
    （按整数比）原样保留，退回字典序 max 的路被正则 + cast 堵死**，
    而回传的只有一行。

    与原实现逐条等价的地方：

    * 序号 0（如「记0」）仍可能进 ``max``，但原实现把 ``n <= 0`` 过滤掉后
      缺省值就是 0，两边对「只存在记0」与「记0 + 记005」都给 0 / 5 ——
      max 的候选里放不放 0 不改变结果。
    * 前缀组只有 1~4 个非数字字符，且序号必须 ``\\d+``（至少一位），这两条
      约束由正则承担，与 ``NO_PATTERN`` 同源；前缀本身不合这个形状时直接
      短路成 001，省掉一次注定为空的查询。
    * cast 用 Numeric 而不是 Integer：Python 的 ``int()`` 不溢出，
      Integer 到 2147483647 就报错，用户自带「记99999999999999」不该让
      发号整个炸掉。

    剩下的代价：PG 侧仍要对本期每一行算一次正则（没有匹配表达式的索引），
    单次发号是 O(本期凭证数)。真要把它也压成 O(1)，得在 ``period`` 上维护
    一个发号计数器列 —— 那要动模型和迁移，不在本文件范围内。
    """
    if not _PREFIX_SHAPE.match(prefix):
        return f"{prefix}{1:0{svc_organize.NO_WIDTH}d}"
    top = db.scalar(
        select(func.coalesce(func.max(_prefix_seq_expr(prefix)), 0)).where(
            Voucher.period_id == period.id
        )
    )
    # 补零位数只有 organize.NO_WIDTH 一个数说了算，别在这儿另写一份。
    return f"{prefix}{int(top or 0) + 1:0{svc_organize.NO_WIDTH}d}"


# --------------------------------------------------------------------- 插入位


def insert_slot(db: Session, anchor: Voucher, position: str = "before") -> dict:
    """插入凭证要占的那个号，以及谁要顺位。

    ``position="before"``（**插入凭证** = 向前插入）：新凭证占参照凭证自己的号，
    参照及以后的号整体顺延 +1。

        记0001..记0005，打开记0003 插入 —— 新凭证拿记0003，
        原记0003..记0005 变成记0004..记0006。

    ``position="after"``（**增加凭证** = 向后插入，用户 2026-10-19 定的口径）：
    新凭证拿参照号 **+1**，只把比它大的往后顺延，参照自己原地不动。

        记0001..记0005，打开记0003 增加 —— 新凭证拿记0004，
        原记0004..记0005 变成记0005..记0006。

    顺延按**号**算，不按日期：凭证号才是这个期间里的顺序，日期排出来的次序
    交给「整理凭证」管。

    这里只算不改。改号和建凭证必须落在同一个事务里：中途放弃一张空凭证，
    后面那叠号就该原封不动 —— 空凭证占着号、把别的凭证顶走，才是真事故。
    """
    if position not in ("before", "after"):
        raise ValueError("插入位置只能是 before 或 after")
    # 参照凭证自己的号先归一：库里有历史遗留的「记-0003」时，顺延出来的号
    # 也必须是「记0004」，不能一半新形态一半旧形态。比较用的序号来自归一后的号，
    # 所以下面每张参与比较的凭证也要归一 —— 否则「记-」和「记」是两个不相等的
    # 前缀，那几张根本不会被顺延，会当场撞 uq_voucher_period_no。
    canon_anchor = svc_organize.normalize_no(anchor.voucher_no)
    prefix, seq = svc_organize.split_no(canon_anchor or anchor.voucher_no)
    if seq == 0:
        raise ValueError(
            f"凭证号 {anchor.voucher_no} 不是「凭证字 + 序号」，插入凭证要照着它顺位，请先整理凭证"
        )
    if position == "after":
        seq += 1
    rows = list(db.scalars(
        select(Voucher).where(Voucher.period_id == anchor.period_id)
    ).all())
    moved = []
    for v in rows:
        canon = svc_organize.normalize_no(v.voucher_no)
        p, s = svc_organize.split_no(canon or v.voucher_no)
        # before 顺延「参照及其以后」；after 顺延「>= 新号」，而新号已经
        # 比参照大 1，所以参照自己（s = 原序号）不满足 s >= seq，天然不动。
        if p == prefix and s >= seq:
            moved.append({"id": v.id, "from": v.voucher_no,
                          "to": f"{prefix}{s + 1:0{svc_organize.NO_WIDTH}d}"})
    moved.sort(key=lambda m: m["from"])
    return {"prefix": prefix, "seq": seq,
            "no": f"{prefix}{seq:0{svc_organize.NO_WIDTH}d}",
            "anchor_no": anchor.voucher_no, "position": position, "moved": moved}


def _apply_shift(db: Session, anchor: Voucher, position: str = "before") -> list[dict]:
    """把参照凭证及其后面的号整体 +1。两阶段改号，避开即时唯一约束。

    直接把 记006 改成 记007 会当场撞上原来那张记007 —— ``uq_voucher_period_no``
    是即时约束，不像平衡检查那样延迟到提交。所以先整体挪到 ∥<id> 临时号再写最终号，
    跟整理凭证用的是同一套路子。
    """
    plan = insert_slot(db, anchor, position)
    moved = plan["moved"]
    if not moved:
        return moved

    targets = []
    for m in moved:
        v = db.get(Voucher, m["id"])
        targets.append((v, m["to"]))
    for v, _new_no in targets:
        v.voucher_no = f"{svc_organize.PARK_PREFIX}{v.id}"
    db.flush()
    for v, new_no in targets:
        v.voucher_no = new_no
    db.flush()
    return moved


def _log_renames(db: Session, moved: list[dict], user, book_id: int,
                 prefix: str) -> None:
    """「谁让了位」：**一张凭证一条审计**。

    以前是把所有顺延凭证的 id 拼成 ``"24,25,26"`` 塞进 ``entity_id``，
    而 ``audit_log.entity_id`` 只有 ``varchar(64)``。

    **顺延超过十张就超长**：2025-01 那个月在第 9 张（记0009）上拆分，
    后面 18 张凭证的号要整体顺延 —— 四个数位的 id 拼起来 89 个字符，
    PostgreSQL 报 22001
    （value too long for type character varying），整个事务回滚，
    界面把它翻译成「提交的内容超出了字段允许的长度」——
    看着像用户提交的问题，其实是日志表的一个字段放不下。
    （「插入凭证」那处的 RENAME 审计是同一种写法，同样中过招。）

    改成**一张一条**：
      · ``entity_id`` 回到全系统「一条审计 = 一个实体」的约定，永远放得下；
      · 顺延了几张就留几条痕，谁改了号一目了然；
      · 每条自帶 before/after（改号前后的号），界面点「查看快照」能看明白。
    """
    for m in moved:
        log(
            db,
            action="RENAME",
            entity="voucher",
            entity_id=m["id"],
            user=user,
            book_id=book_id,
            detail=f"{prefix}：{m['from']}→{m['to']}",
            before={"id": m["id"], "voucher_no": m["from"]},
            after={"id": m["id"], "voucher_no": m["to"]},
        )


# --------------------------------------------------------------------- 校验


def side_totals(entries: list[dict]) -> tuple[Decimal, Decimal]:
    """凭证两栏的合计：**红字算到对面去**。

    贷方红字（负数）本质是借方，借方红字本质是贷方 —— 这是红字的本意，
    不是「贷方减了 300」。所以每栏只加正数：

        借方合计 = 借方正数 + 贷方红字（绝对值）
        贷方合计 = 贷方正数 + 借方红字（绝对值）

    跟「带符号的两边相等」完全等价（移项即可），所以拿它判平也对；
    但它是**给人看**的那个数：两栏都摆得下正数，一眼比得出大小。
    """
    dr = cr = Decimal("0")
    for e in entries:
        amt = money(e["amount"])
        if amt > 0:
            if e["direction"] == "DEBIT":
                dr += amt
            else:
                cr += amt
        elif amt < 0:
            if e["direction"] == "DEBIT":
                cr += -amt
            else:
                dr += -amt
    return dr, cr


# 系统自己生成的凭证：结转损益、年度结转。
# 它们搬的是**已经存在**的余额，不是「给这个科目记一笔新账」，
# 所以不受「停用科目不能新记」的闸门约束（审计 P2-08）。
#
# **这个标记必须由服务端自己给。** 客户端曾经能在 ``VoucherIn.source`` 里
# 提交 CARRY_FORWARD，于是「停用科目可豁免」这道闸对任何有写权限的人都开着，
# 而 ``periods._drop_carry_forward`` 又会不看状态地删掉本期全部 CARRY_FORWARD
# 凭证 —— 自己造的凭证、记上账、再点一次重新结转就没了。现在写权限来自
# ``create_voucher(internal=True)``，客户端那份 ``Literal`` 里已经没有它。
SYSTEM_SOURCES = ("CARRY_FORWARD",)


def _validate_entries(db: Session, book_id: int, entries: list[dict],
                      year: int,
                      allow_inactive: bool = False,
                      legacy_non_leaf: Counter | None = None) -> int:
    """校验分录。返回**本次被豁免的历史非末级分录条数**（审计 P2-21）。

    末级科目这道关口是硬的：分录挂在上级科目上，明细账、科目余额表都按末级出账，
    这笔分录会在账上凭空消失。前端不给选、导入会拦、服务端再拦一遍，
    才轮得到「账上不可能出现挂在上级科目上的分录」这句话。

    ``legacy_non_leaf`` 是唯一一条口子，而且它不是「校验放宽」，
    是「**原样保留历史**」：会计早于末级校验就在凭证上挂了非末级科目，
    后来那个科目被加了子节点 —— 从此这张凭证**一个格子都改不了**，
    连只改附单据数都存不下去（400）。这不是校验太严，是校验把历史锁死了。

    口子的边界必须 fail-closed：调用方交来的是「这张凭证原本有哪些分录」的
    逐条计数（科目 + 方向 + 金额），本次提交里每一条非末级分录都要**逐条**
    从这份计数里扣一条对得上的。也就是：

      · 新增一条非末级分录   → 扣不掉 → 拒绝（**新错不许进**）
      · 改金额/改科目        → 扣不掉 → 拒绝（历史不许被加工）
      · 老分录原样提交        → 扣得掉 → 放行
      · 删一条再加两条一样的 → 第二条扣不掉 → 拒绝（计数不是布尔开关）

    所以放宽的只有「保持原样」这一种情形，而它甚至不会被写进库里
    （写进去的还是原来那些分录）。
    """
    if not entries:
        raise ValueError("凭证至少需要一条分录")
    dr = sum(money(e["amount"]) for e in entries if e["direction"] == "DEBIT")
    cr = sum(money(e["amount"]) for e in entries if e["direction"] == "CREDIT")
    if dr != cr:
        raise ValueError(f"借贷不平衡：借方 {dr} / 贷方 {cr}")
    # 负数是**红字冲销**，一张红字凭证借贷两边都是负的（-300 / -300），
    # 照样是平的、照样该存 —— 冲掉一张记错的凭证正是记账的日常。
    # **正负相抵**（借 111 / 借 -111）同理：红字把 -111 搬去了对面，两栏一样大，
    # 按「借贷合计相等」就是平的（2026-10-12 用户明确要求这么判），也该存。
    #
    # 只挡**一条金额都没填**的：每条都是 0，那不是「记了 0 块」，是没填。
    # 它存进去，报表上凭空多出一张凭证，还查不出是哪张干的。
    if all(money(e["amount"]) == 0 for e in entries):
        raise ValueError("凭证金额不能是 0")

    subject_ids = {e["subject_id"] for e in entries}
    stmt = select(Subject.id, Subject.code, Subject.name, Subject.is_leaf).where(
        Subject.book_id == book_id,
        Subject.year == year,
        Subject.id.in_(subject_ids),
    )
    if not allow_inactive:
        stmt = stmt.where(Subject.active.is_(True))
    rows = db.execute(stmt).all()
    found = {r.id for r in rows}
    missing = subject_ids - found
    if missing:
        raise ValueError(f"科目不存在或已停用: {sorted(missing)}")

    # 末级科目这道关口放在服务端，不能只指望前端面板不给选：
    # 接口能被直接调、导入能绕、老的行早就挂上去了 —— 前端拦一遍，这里再拦一遍，
    # 才轮得到「账上不可能出现挂在上级科目上的分录」这句话。
    # 挂错了不是显示难看：明细账、科目余额表都按末级出账，这笔分录会在账上凭空消失。
    by_id = {r.id: r for r in rows}
    non_leaf: list[str] = []
    grandfathered = 0
    for e in entries:
        r = by_id.get(e["subject_id"])
        if r is None or r.is_leaf:
            continue
        key = (e["subject_id"], e["direction"], money(e["amount"]))
        if legacy_non_leaf is not None and legacy_non_leaf.get(key, 0) > 0:
            legacy_non_leaf[key] -= 1     # 逐条一一对应，不是布尔开关
            grandfathered += 1
            continue
        non_leaf.append(f"{r.code} {r.name}")
    if non_leaf:
        raise ValueError(f"不是末级科目，请改到末级科目：{'、'.join(sorted(set(non_leaf)))}")
    return grandfathered


# --------------------------------------------------------------------- 写操作


def validate_new_voucher(db: Session, *, book_id: int, voucher_date: date,
                         entries: list[dict], period_id: int | None = None,
                         insert_after_id: int | None = None, internal: bool = False):
    """无写入地校验新凭证，返回期间、标准化分录和插入锚点。"""
    period = db.get(Period, period_id) if period_id else find_period(db, book_id, voucher_date)
    if period is None:
        raise ValueError("会计期间不存在")
    # **期间必须属于本账套**（审查报告 P2-2）。
    #
    # ``db.get(Period, period_id)`` 是**按主键取**，不带任何账套过滤 ——
    # 拿别家账套的期间 id 进来，会得到一个「属于别的账套」的期间对象，
    # 而 ``period.ord`` 是**账套内**的相对序号：于是这张凭证被记到
    # 「本账套的某一期」上，而挂着别家的期间标签。
    #
    # 这条校验与 ``api/vouchers.py``、``api/reports.py``、``api/printing.py``
    # 里的写法是**同一句话** —— 本仓里「期间必须属于本账套」这条规则早就存在，
    # 漏掉的只是这第四处。
    if period.book_id != book_id:
        raise ValueError("会计期间不属于本账套")
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能新增凭证")

    # 插入凭证：先认下参照凭证。顺延跟建凭证在同一个事务里做完，
    # 半路退出（一个格子都没填）就不会动别人的号。
    anchor: Voucher | None = None
    if insert_after_id is not None:
        anchor = db.get(Voucher, insert_after_id)
        if anchor is None or anchor.book_id != book_id:
            raise ValueError("插入位置的参照凭证不存在")
        if anchor.period_id != period.id:
            where = db.get(Period, anchor.period_id)
            raise ValueError(
                f"只能插在同一个会计期间里：参照凭证 {anchor.voucher_no} 在 "
                f"{where.code if where else '别的期间'}，这张在 {period.code}"
            )

    payload = [
        {
            "subject_id": int(e["subject_id"]),
            "direction": e["direction"],
            "amount": money(e["amount"]),
            "summary": e.get("summary", "") or "",
            "aux_ref": e.get("aux_ref") or {},
        }
        for e in entries
    ]
    # allow_inactive 只认 internal —— 见函数 docstring。原来是
    # ``source in SYSTEM_SOURCES``，而 source 由客户端提交。
    _validate_entries(db, book_id, payload, period.year, allow_inactive=internal)

    return period, payload, anchor


def create_voucher(
    db: Session,
    *,
    book_id: int,
    user: SysUser,
    voucher_date: date,
    entries: list[dict],
    remark: str = "",
    attachments: int = 0,
    source: str = "MANUAL",
    voucher_no: str | None = None,
    period_id: int | None = None,
    insert_after_id: int | None = None,
    insert_position: str = "before",
    commit: bool = True,
    internal: bool = False,
) -> Voucher:
    """建一张凭证。

    ``internal=True`` 是「这是系统自己生成的凭证」（结转损益、年度结转）的
    **唯一**正规声明，也是 ``allow_inactive`` 唯一的来源 —— 它过去是
    ``source in SYSTEM_SOURCES``，而 source 曾经由客户端提交，等于把这道
    「停用科目也能记账」的豁免开给了所有人。现在豁免跟着调用方走：
    客户端能构造的 source（MANUAL/IMPORT/INVOICE）拿不到它，
    ``SYSTEM_SOURCES`` 只由 ``app/accounting/periods.py`` 那两个生成方使用。

    刻意**不**在这里对 ``source in SYSTEM_SOURCES`` 再抛一次错：内部代码
    （测试夹具 ``make_voucher`` 等）也会造 CARRY_FORWARD 凭证来验证查询口径，
    而那条路上真正要守的不变量已经守住了 ——
    ① 豁免拿不到，② ``_drop_carry_forward`` 只删签名对得上的系统凭证，
    伪造一张出来既没有特权、也不会被悄悄删掉。
    """
    period, payload, anchor = validate_new_voucher(
        db, book_id=book_id, voucher_date=voucher_date, entries=entries,
        period_id=period_id, insert_after_id=insert_after_id, internal=internal)

    # 上号的三个来源：插入位（占参照的号）、自动发号、以及**导入时表里带的号**。
    # 三者都要过 require_no 归一 + 校验：用户 2026-10-19 定的是「凭证号一律
    # 自动生成」，但导入是另一个入口（Excel 里带什么号就带什么号），不归一
    # 的话「记-0001」会和自动发的「记0002」混在一个期间里，把发号正则打瞎。
    moved: list[dict] = []
    moving_ids: set[int] = set()
    if anchor is not None:
        slot = insert_slot(db, anchor, insert_position)
        moved = slot["moved"]
        moving_ids = {m["id"] for m in moved}
        no = svc_organize.require_no(voucher_no) or slot["no"]
    else:
        no = svc_organize.require_no(voucher_no) or next_voucher_no(db, period)

    # 页面上会预填下一个号（/vouchers/next），两个人同时开一张就会撞号。
    # 撞了就报人话 —— 数据库只会说「违反唯一约束 uq_voucher_period_no」，
    # 看到的人不知道该改哪个字段。
    #
    # 必须在顺位**之前**查一次：号被顶走是这一笔唯一不可逆的副作用，
    # 先顺位再发现撞号，事务虽然会回滚，可这一层代码不该依赖调用方记得回滚。
    # 顺位会把参照凭证的旧号腾出来，所以占用检查要把「待顺位的那几张」排除掉。
    taken = db.scalar(select(Voucher.id).where(
        Voucher.period_id == period.id, Voucher.voucher_no == no))
    if taken is not None and taken not in moving_ids:
        raise ValueError(f"凭证号 {no} 在 {period.code} 已经有一张了，请改一个号")

    if moved:
        moved = _apply_shift(db, anchor, insert_position)

    voucher = Voucher(
        book_id=book_id,
        period_id=period.id,
        voucher_no=no,
        voucher_date=voucher_date,
        status="UNPOSTED",
        source=source,
        remark=remark,
        attachments=attachments,
        created_by=user.username,
    )
    # 撞号在数据库上是唯一约束，在这里是 ValueError。
    #
    # 上面那次「占用检查」和 ``next_voucher_no`` 的「取全表求 max」都没有锁，
    # 两个人同时开一张必然双双查到「没被占」。DB 侧有 ``uq_voucher_period_no``
    # 兜底，所以**不会产生脏数据**，问题只在于撞号抛的是 IntegrityError：
    # 导入侧 ``except ValueError`` 接不住（``importer.commit``），
    # 一整批导入就变成 HTTP 500，用户看到的是「服务器错误」而不是
    # 「凭证号重复」—— 而后者只要换一个号就能解决。
    #
    # savepoint 而不是裸 flush：IntegrityError 会把整个事务标记为失败，
    # 而这一段可能在调用方（批量导入）已经写了几张凭证之后才撞上，
    # 一次并发不该把那几张一起废掉。
    try:
        with db.begin_nested():
            db.add(voucher)
            db.flush()
    except IntegrityError as exc:
        if not _is_no_collision(exc):
            raise
        raise ValueError(
            f"凭证号 {no} 在 {period.code} 已经有一张了（可能刚被另一条记录占用），"
            "请刷新后换一个号重试") from exc

    for i, e in enumerate(payload, start=1):
        db.add(Entry(voucher_id=voucher.id, line_no=i, **e))

    db.flush()
    if moved:
        # 号被顶走这件事必须留痕：事后有人问「记007 怎么变成这笔了」，
        # 操作日志里能一句话说清是老几号顺位过来的。
        _log_renames(db, moved, user, book_id, "插入凭证顺位")
    log(
        db,
        action="CREATE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=book_id,
        detail=f"新增凭证 {voucher.voucher_no}",
        # 分录是拿外键建的，backref 不会回填 voucher.entries —— 不把
        # payload 递进去，下面这行就会为了拿「刚写进去的这些分录」再查一次库。
        after=voucher_snapshot(
            voucher, entries=[dict(e, line_no=i) for i, e in enumerate(payload, start=1)]
        ),
    )
    if commit:
        db.commit()
        db.refresh(voucher)
    return voucher


def update_voucher(
    db: Session, voucher: Voucher, user: SysUser, payload: dict
) -> Voucher:
    before = voucher_snapshot(voucher)
    period = db.get(Period, voucher.period_id)
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能修改凭证")
    # 已记账 = 已进账簿，数字已经被总账/报表引用。改它必须先退回复记账状态，
    # 否则「报表里的数和凭证里写的数」会对不上，且没人知道差在哪一步。
    #
    # 提示里**不能写「请在凭证列表里反记账」**：凭证列表工具栏上的
    # 「记账 / 反记账」早就撤了（结算只在期末结账那一处，见 ARCHITECTURE §4），
    # 按这句话去列表页翻的人只会找不到那个按钮。退回复记账的入口现在只有
    # 「期末结账 → 反结账」—— 它会把区间内凭证一并退回未记账。
    if voucher.status == "POSTED":
        raise ValueError(
            f"凭证 {voucher.voucher_no} 已记账，不能修改。"
            f"请到「期末结账」对本期执行「反结账」后再改。")

    # ``insert_after`` 同属「以前静默丢弃」的那一类（P2-2）。它与 voucher_no
    # 不同：改号是业务上允许的（凭证号可回收复用），而「插在谁后面、把后面
    # 整体顺位」是**建凭证**独有的动作，对一张已存在的凭证说这句话没有意义 ——
    # 照着办要动别的凭证的号，而那张凭证属于另一条业务规则。
    #
    # 与其又一次「返回成功但什么都没做」，不如明确拒绝并说清为什么。
    if payload.get("insert_after"):
        raise ValueError(
            "insert_after 只在新增凭证时有效（「插在谁后面、后面的号顺位」）；"
            "修改已存在的凭证请直接填 voucher_no。")

    if "voucher_date" in payload and payload["voucher_date"]:
        new_date = payload["voucher_date"]
        new_period = find_period(db, voucher.book_id, new_date)
        voucher.voucher_date = new_date
        voucher.period_id = new_period.id

    # 凭证号不再允许人工改（用户 2026-10-19 定的：新增 / 插入 / 修改三处
    # 一律只读，只能用系统自动生成的号）。
    #
    # 这里原本是 P2-2 那个「静默丢弃」的修复 —— 客户端 PUT 一个新号会拿到 200、
    # 号却没变，所以当年人把它**实现**掉了。但实现出来的是一把没有护栏的刀：
    # 号段是期间的顺序，手改一个号既可能撞唯一约束，也可能把整段号打乱。
    # 生产上就是这么出的事 —— 优境 9 月那张 18 条分录的工资凭证，凭证号被人
    # 在这个页面上改成 "wochakan"，凭证列表四种排序全部 500（排序键要对凭证号
    # 做 CAST AS INTEGER），整月打不开，排查花了半小时才定位到一行脏数据。
    #
    # **明确拒绝**而不是静默忽略：静默忽略会让人以为改成功了（这正是 P2-2
    # 当初要消灭的那种"返回成功却什么都没做"）。要改号请走「整理凭证」，
    # 它会整体重排并留下审计。
    new_no = svc_organize.normalize_no(payload.get("voucher_no"))
    if new_no and new_no != voucher.voucher_no:
        raise ValueError(
            f"凭证号不能手工修改（{voucher.voucher_no} → {new_no}）。"
            "凭证号由系统按期间自动生成；要调整号段请用「整理凭证」重排。"
        )

    if "entries" in payload:
        entries = [
            {
                "subject_id": int(e["subject_id"]),
                "direction": e["direction"],
                "amount": money(e["amount"]),
                "summary": e.get("summary", "") or "",
                "aux_ref": e.get("aux_ref") or {},
            }
            for e in payload["entries"]
        ]
        # P2-21：这张凭证**原本**有哪些分录，必须在清空之前逐条记下来。
        # 它是「历史非末级分录原样保留」那条口子的唯一凭据 ——
        # 校验时每条非末级分录都要从这里扣一条对得上的，对不上的照样拒。
        #
        # 症状：科目被加了子节点之后，那些早于末级校验的老凭证**再也存不下去** ——
        # 前端只改附单据数也一样 400，因为 ``update_voucher`` 无条件调用校验。
        legacy = Counter(
            (e.subject_id, e.direction, money(e.amount)) for e in voucher.entries
        )
        grandfathered = _validate_entries(db, voucher.book_id, entries,
                                          period.year, legacy_non_leaf=legacy)
        voucher.entries.clear()
        db.flush()
        for i, e in enumerate(entries, start=1):
            voucher.entries.append(Entry(voucher_id=voucher.id, line_no=i, **e))
        # 改动内容后回到未记账，避免「已记账但内容变了」的脏状态
        voucher.status = "UNPOSTED"
        voucher.posted_at = None
        voucher.posted_by = None
    else:
        grandfathered = 0

    if "remark" in payload:
        voucher.remark = payload["remark"] or ""
    if "attachments" in payload:
        voucher.attachments = int(payload["attachments"] or 0)

    db.flush()
    log(
        db,
        action="UPDATE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=voucher.book_id,
        # 走了「历史非末级分录原样保留」那条口子时，日志里必须留一笔：
        # 这张凭证上从此会长期挂着非末级分录，事后有人查「为什么它还能改」，
        # 答案只能在这条日志里，不能靠翻代码重新推一遍。
        detail=f"修改凭证 {voucher.voucher_no}"
               + (f"（保留 {grandfathered} 条历史非末级分录，本次未改动）"
                  if grandfathered else ""),
        before=before,
        after=voucher_snapshot(voucher),
    )
    db.commit()
    db.refresh(voucher)
    return voucher


def delete_voucher(db: Session, voucher: Voucher, user: SysUser,
                   commit: bool = True) -> None:
    """删除凭证。因为编号可复用，必须把内容快照写进审计日志。

    ``commit=False`` 让调用方把多步操作合成一个事务（批量删除用它）。
    批量删到第 5 张失败时，前 4 张不该已经落在库里 ——
    用户收到的是 400「删失败」，而账上少了 4 张，且没人告诉他少了哪几张。
    """
    if voucher.status == "POSTED":
        raise ValueError(
            f"凭证 {voucher.voucher_no} 已记账，不能删除。"
            f"请到「期末结账」对本期执行「反结账」后再删。")
    snapshot = voucher_snapshot(voucher)
    log(
        db,
        action="DELETE",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=voucher.book_id,
        detail=f"删除凭证 {voucher.voucher_no}",
        before=snapshot,
    )
    db.delete(voucher)
    if commit:
        db.commit()


# --------------------------------------------------------------------- 拆分凭证
#
# 把一张凭证在**分录边界**处切成两张：前半段留在原凭证，后半段另起一张新凭证。
#
# 三条硬约束（拆分的可行域就是它们划出来的）：
#
#   1. **两张各自必须借贷平衡**。整张平不代表切一刀之后还平 ——
#      「借 管理费用500 / 借 管理费用800 / 贷 银行存款1300」在第 1 行后切开，
#      前半张只有借方 500。库里那条 ``trg_entry_balance`` 是**延迟到提交时**
#      才判的约束触发器，所以不平的两张在提交那一刻会被数据库挡下；
#      这里提前挡，是为了给用户一句人话而不是一条 check_violation。
#      **不平就不给拆**，并把两边的差额说清楚。
#
#   2. **一个事务**。「原凭证删掉后半段」与「新凭证建起后半段」必须同生共死 ——
#      分两次提交的话，中间那一刻原凭证是残的（可能不平），新凭证还不存在。
#
#   3. **编号沿用「增加凭证」那套语义**（``insert_slot(position="after")``）：
#      **原凭证继承它自己的号**（一个字符都不动 —— 它可能已经打印、已经被引用过），
#      新凭证往后拿一个号（= 原号 +1），后面同期的号整体顺延 +1。
#      顺延是「整体 +1」而不是「删一个补一个」，所以期间号段既不缺号也不重号，
#      ``organize.assert_tidy`` 仍然放行记账与结账。


def _entry_payload(e: Entry) -> dict:
    """Entry → 校验 / 建凭证那种 dict。"""
    return {
        "subject_id": int(e.subject_id),
        "direction": e.direction,
        "amount": money(e.amount),
        "summary": e.summary or "",
        "aux_ref": e.aux_ref or {},
    }


def _split_entries(voucher: Voucher) -> list[Entry]:
    """这张凭证的分录，按行号排好。"""
    return sorted(voucher.entries, key=lambda e: e.line_no)


def _assert_contiguous_line_no(entries: list[Entry]) -> None:
    """分录行号必须是 1..n 连续，否则「在第几条之后切」这件事没有确定答案。

    正常情况下它一定连续（``create_voucher`` 与 ``update_voucher`` 都是从 1
    重排的），所以这条是**兜底**：真的不连续时（历史脏数据）宁可拒绝，
    也不要猜一个切点出来 —— 猜错的后果是拆出来的两张张冠李戴。
    """
    nos = [e.line_no for e in entries]
    if nos != list(range(1, len(nos) + 1)):
        raise ValueError(
            f"这张凭证的分录行号不连续（{'、'.join(str(n) for n in nos)}），拆不开。"
            "请先修改一次这张凭证（保存后行号会重排），再拆。"
        )


def _half_totals(entries: list[Entry]) -> dict:
    """一半分录的合计与判平结果。

    **判平用带符号和，展示用 ``side_totals``** —— 两套口径，结论永远一致：
    设 ``dr_side - cr_side = (Σ借正 + Σ贷负) - (Σ贷正 + Σ借负)``，
    移项就是 ``dr_signed - cr_signed``，所以「平不平」两种算法同解，
    差的只是红字凭证上摆出来的数（界面那一栏要的是正数）。

    判平口径必须与 ``_validate_entries`` 一致（它也是带符号和），
    否则会出现「页面说平、服务端说不平」。
    """
    rows = [{"direction": e.direction, "amount": e.amount} for e in entries]
    dr_side, cr_side = side_totals(rows)
    dr_signed = sum((money(e.amount) for e in entries if e.direction == "DEBIT"), Decimal("0"))
    cr_signed = sum((money(e.amount) for e in entries if e.direction == "CREDIT"), Decimal("0"))
    return {
        "lines": len(entries),
        # **过一遍 money()**：``side_totals`` 的零值是 ``Decimal("0")``，
        # 直接 str() 出来是「0」而不是「0.00」—— 页面上摆的是「0.00」，
        # 这里摆「0」，同一个数两种写法，看着像两个数。
        "debit": str(money(dr_side)),
        "credit": str(money(cr_side)),
        "balanced": dr_signed == cr_signed,
        # 差额 = 借 - 贷。正数是借方多，负数是贷方多。
        "diff": str(money(dr_signed - cr_signed)),
    }


def _side_phrase(totals: dict) -> str:
    """一半的账：借 X / 贷 Y（借方多 Z）。

    **方向要说出来**，不能只给一个数 —— 只写「差额 500」的话，
    人还得回头数一遍才知道该往哪一栏补。
    """
    diff = Decimal(totals["diff"])
    base = f"借 {totals['debit']} / 贷 {totals['credit']}"
    if diff > 0:
        return f"{base}（借方多 {abs(diff)}）"
    if diff < 0:
        return f"{base}（贷方多 {abs(diff)}）"
    return f"{base}（平）"


def _split_message(at_line: int, first: dict, second: dict) -> str:
    """一个候选分界处的说法。平与不平都说清楚。"""
    head = f"在第 {at_line} 行后拆分"
    if first["balanced"] and second["balanced"]:
        return f"{head}：前半张 {_side_phrase(first)}；后半张 {_side_phrase(second)}（两张都平）"
    return (f"{head}不成立：前半张 {_side_phrase(first)}；后半张 {_side_phrase(second)}。"
            "请换一个分界处。")


def _legacy_counter(entries: list[Entry]) -> Counter:
    """这半分录里「历史非末级分录」的逐条计数。

    与 ``update_voucher`` 里那份计数同一把尺（科目 + 方向 + 金额），
    但**按半张分别算** —— 祖父条款（审计 P2-21）的凭据是
    「**这一半**原本有哪些分录」，拿整张的计数去套两半，
    会让另一半凭空多出一条豁免。
    """
    return Counter((e.subject_id, e.direction, money(e.amount)) for e in entries)


def _reject_split(voucher: Voucher) -> None:
    """两张与并发无关的硬闸：已记账、结转损益。

    **结转损益为什么不能拆**：``periods._drop_carry_forward`` 在
    「重新结转损益」时按 ``source == "CARRY_FORWARD"`` **加签名**删本期
    结转凭证。拆一张出来，那张新凭证的 source 仍是 CARRY_FORWARD、
    签名却对不上了 —— 重新结转时它既不会被删、也就不再是系统凭证，
    账上会留一张半结转的孤儿。
    """
    if voucher.status == "POSTED":
        raise ValueError(
            f"凭证 {voucher.voucher_no} 已记账，不能拆分。"
            "请到「期末结账」对本期执行「反结账」后再拆。")
    if voucher.source == "CARRY_FORWARD":
        raise ValueError(
            "结转损益凭证由系统生成，不能拆分。请先到「期末结账」重新结转损益。")


def split_preview(db: Session, voucher: Voucher) -> dict:
    """拆分的预览：一次给出**所有**候选分界处（只算不改）。

    与「整理凭证」的 preview / apply 两阶段同一个道理：拆分会改掉
    后面同期的凭证号，而纸质凭证可能已经打印装订了 ——
    先看清「切在哪、两张各自平不平、拆完各拿哪个号」，再确认。

    一次返回全部候选，而不是让页面为每个边界发一次请求 ——
    候选数 = 分录数 - 1，一张几十行的凭证就是几十个请求。
    """
    entries = _split_entries(voucher)
    candidates = []
    for k in range(1, len(entries)):
        t1 = _half_totals(entries[:k])
        t2 = _half_totals(entries[k:])
        candidates.append({
            "at_line": k,
            "first": t1,
            "second": t2,
            "valid": bool(t1["balanced"] and t2["balanced"]),
            "message": _split_message(k, t1, t2),
        })
    # **原凭证继承它自己的号**，新凭证往后拿一个（与「增加凭证」同一套语义）。
    slot = insert_slot(db, voucher, "after")
    return {
        "voucher_id": voucher.id,
        "voucher_no": voucher.voucher_no,
        "entry_count": len(entries),
        "candidates": candidates,
        # 新凭证拿的号（= 原号 +1，后面同期的号整体顺延）
        "created_no": slot["no"],
        # 原凭证的号 —— 它不动
        "original_no": voucher.voucher_no,
        "moved": slot["moved"],
    }


def split_voucher(db: Session, voucher: Voucher, user: SysUser,
                  at_line: int) -> dict:
    """把一张凭证在第 ``at_line`` 条分录之后拆成两张。

    返回 ``{"original": 原凭证, "created": 新凭证, "moved": 顺延清单}``。
    原凭证的 **id 和号都不变**（只少了后半段分录），新凭证往后拿一个号，
    后面同期的号整体顺延 +1。
    """
    # ---- 闸门 0：与并发无关的那两条（先挡掉，省一次锁）
    _reject_split(voucher)

    period = db.get(Period, voucher.period_id)
    if period is None:
        raise ValueError("会计期间不存在")

    # ---- 锁：期间行 → 这张凭证行
    #
    # 期间行与 ``organize.apply_organize`` 用**同一把锁、同一个顺序**
    # （期间粒度、按 ord 升序）：同期间的「拆分 / 插入 / 整理」因此互斥，
    # 两个并发拆分别算出同一个号的事被挡在门外。
    #
    # 这张凭证行也要锁：它挡的是**并发改这一张**（``update_voucher`` 不走
    # 期间锁）。锁到手后重新读一遍分录 —— 拆的是**当下**这一张，
    # 不是进入函数时看到的那一份。
    db.execute(
        select(Period.id).where(Period.id == period.id).with_for_update()
    ).all()
    db.refresh(period)
    db.execute(
        select(Voucher.id).where(Voucher.id == voucher.id).with_for_update()
    ).all()
    db.refresh(voucher)
    db.expire(voucher, ["entries"])

    # ---- 闸门 1：以锁后重读的状态为准
    _reject_split(voucher)
    if period.locked:
        raise ValueError(f"{period.code} 已结账，不能拆分凭证")

    entries = _split_entries(voucher)
    n = len(entries)
    if n < 2:
        raise ValueError(f"这张凭证只有 {n} 条分录，拆不开")
    if at_line < 1 or at_line > n - 1:
        raise ValueError(f"分界处必须在第 1 行到第 {n - 1} 行之间（当前 {n} 行）")
    _assert_contiguous_line_no(entries)

    first = entries[:at_line]
    second = entries[at_line:]

    t1 = _half_totals(first)
    t2 = _half_totals(second)
    if not (t1["balanced"] and t2["balanced"]):
        raise ValueError(_split_message(at_line, t1, t2))

    # 分录校验：末级科目 / 停用科目 / 零金额。
    # **两半各自用自己的 legacy 计数** —— 见 ``_legacy_counter``。
    for half in (first, second):
        _validate_entries(
            db, voucher.book_id,
            [_entry_payload(e) for e in half],
            period.year,
            legacy_non_leaf=_legacy_counter(half),
        )

    # ---- 编号：**原凭证继承它自己的号**，新凭证往后拿一个
    #
    # 与「增加凭证」（position="after"）完全同一套语义与代码路径 ——
    # ``insert_slot`` 算谁拿哪个号、谁顺延，``_apply_shift`` 两阶段改号
    # （先写 ∥<id> 再写最终号），避开 uq_voucher_period_no 的即时唯一约束。
    #
    # 原凭证的号**一个字符都不动** —— 它可能已经打印、已经被引用过；
    # 动它的是**后面**那几张（整体 +1，给新凭证腾出中间那个号）。
    canon = svc_organize.normalize_no(voucher.voucher_no)
    _prefix, seq = svc_organize.split_no(canon or voucher.voucher_no)
    if seq == 0:
        raise ValueError(
            f"凭证号 {voucher.voucher_no} 不是「凭证字 + 序号」，"
            "拆分要照着它顺位，请先整理凭证"
        )
    slot = insert_slot(db, voucher, "after")
    no = slot["no"]
    moving_ids = {m["id"] for m in slot["moved"]}

    # 占用检查：新号此刻可能被**后面某张**占着（它马上要让位），
    # 所以要把「待顺位的那几张」排除掉 —— 与 ``create_voucher`` 同一套写法。
    taken = db.scalar(select(Voucher.id).where(
        Voucher.period_id == period.id, Voucher.voucher_no == no))
    if taken is not None and taken not in moving_ids:
        raise ValueError(f"凭证号 {no} 在 {period.code} 已经有一张了，请改一个号")

    # 拆分前的完整快照 —— 必须在顺延**之前**取：顺延会改掉后面那几张的号，
    # 而「拆之前那张凭证长什么样」正是事后追溯时要对的那一份。
    before_snapshot = voucher_snapshot(voucher)
    old_no = voucher.voucher_no

    # ---- 顺延（两阶段改号）。**原凭证不在其中** —— 它的号不动。
    moved = _apply_shift(db, voucher, "after")

    # ---- 原凭证只留前半段。
    # 前半段的 line_no 本来就是 1..at_line（连续，上面查过了），不用重排；
    # 后半段的行从关系里摘掉 → delete-orphan 级联删行。
    for e in second:
        voucher.entries.remove(e)
    db.flush()

    # ---- 新凭证：往后拿那个号；日期与摘要沿用；**附单据数留在原凭证**（不重复计数）。
    created = Voucher(
        book_id=voucher.book_id,
        period_id=voucher.period_id,
        voucher_no=no,
        voucher_date=voucher.voucher_date,
        status="UNPOSTED",
        source=voucher.source,
        remark=voucher.remark,
        attachments=0,
        created_by=user.username,
    )
    db.add(created)
    db.flush()
    payload: list[dict] = []
    for i, e in enumerate(second, start=1):
        row = _entry_payload(e)
        payload.append(dict(row, line_no=i))
        db.add(Entry(voucher_id=created.id, line_no=i, **row))
    db.flush()

    # ---- 审计三条：拆了什么、拆出来的是什么、谁让了位。
    # 凭证号可回收复用，所以「拆之前那张长什么样」必须完整留在日志里。
    log(
        db,
        action="SPLIT",
        entity="voucher",
        entity_id=voucher.id,
        user=user,
        book_id=voucher.book_id,
        detail=f"拆分凭证 {old_no}：保留前 {at_line} 行，"
               f"其余 {len(second)} 行拆成新凭证 {no}",
        before=before_snapshot,
        after=voucher_snapshot(voucher),
    )
    log(
        db,
        action="CREATE",
        entity="voucher",
        entity_id=created.id,
        user=user,
        book_id=voucher.book_id,
        detail=f"由 {old_no} 拆分而来（带走后 {len(second)} 行）",
        after=voucher_snapshot(created, entries=payload),
    )
    if moved:
        _log_renames(db, moved, user, voucher.book_id, "拆分凭证顺位")
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if not _is_no_collision(exc):
            raise
        raise ValueError(
            f"凭证号 {no} 在 {period.code} 已经有一张了（可能刚被另一条记录占用），"
            "请刷新后重试") from exc
    db.refresh(voucher)
    return {"original": voucher, "created": created, "moved": moved}


# --------------------------------------------------------------------- 记账


def _resolve_vouchers(db: Session, book_id: int, voucher_ids: list[int]) -> list[Voucher]:
    """把请求的 ID 列表解析成**确实属于 book_id**的凭证集合。

    为什么必须带 book_id 一起查：原来只有 ``Voucher.id.in_(ids)``，
    路由层拿到的 book 压根没往下传 —— 于是拿着 A 账套的接口就能把
    B 账套的凭证状态改掉（审计 P1-01，动态复现 HTTP 200）。
    凭证状态直接决定它算不算进余额表、总账和报表，这是跨账套数据完整性破口。

    为什么整体拒绝而不是「跳过越界的、成功的照样成功」：
    部分成功最坏 —— 用户以为 3 张都记上了，实际 2 张、而且报错被淹在
    「已记账 1 张」这种成功计数里。这里改成**一个不对就全批拒绝**。

    另外顺带解决 P1-08：原来查不到就直接访问 ``vouchers[0]``，
    传不存在的 ID → IndexError → HTTP 500。500 会掩盖真实原因
    （是不存在？是无权？还是服务端坏了？），也不符合 API 契约。

    ``selectinload(Voucher.entries)``：批量记账按 ``v.entries`` 逐张求借贷合计
    （``post_vouchers``），裸 select 会让每张各发一条 ``SELECT ... WHERE
    voucher_id=?`` —— 一次批量记账 = N 次往返。与凭证列表查询是同一个坑，
    那边早就补过了（``query_vouchers`` 里的 selectinload，200 次往返的注释
    还记着）。预载对不读分录的调用方（``unpost_vouchers``）只是多传一份
    数据，语义不变：这里返回的还是同一批凭证对象，分录还是各自的那几条。
    """
    wanted = sorted(set(voucher_ids))
    rows = list(db.scalars(
        select(Voucher)
        .options(selectinload(Voucher.entries))
        .where(Voucher.id.in_(wanted), Voucher.book_id == book_id)
    ).all())
    found = {v.id for v in rows}
    missing = [i for i in wanted if i not in found]
    if missing:
        raise ValueError(
            f"凭证不存在或不属于当前账套，已整批拒绝：{', '.join(str(i) for i in missing[:10])}"
            + ("…" if len(missing) > 10 else ""))
    return rows


def post_vouchers(db: Session, book_id: int, voucher_ids: list[int], user: SysUser,
                  commit: bool = True) -> int:
    """批量记账。``book_id`` 是**必填**的账套归属约束，不是可选参数。

    ``commit=False`` 让调用方把多步操作合成一个事务。
    """
    if not voucher_ids:
        return 0
    vouchers = _resolve_vouchers(db, book_id, voucher_ids)
    period_ids = {v.period_id for v in vouchers}
    rows = db.execute(select(Period).where(Period.id.in_(period_ids))).scalars().all() if period_ids else []

    locked = sorted(p.code for p in rows if p.locked)
    if locked:
        raise ValueError(f"会计期间 {', '.join(locked)} 已结账，不能记账")

    # 号段有洞、或者号序跟日期对不上：这个月就还不能记账。记了账这些凭证就锁死，
    # 而纸质的已经打印装订，月底对不上账只能整月作废重来。
    svc_organize.assert_tidy(db, rows)

    # **只有上一期已结账，本期才允许记账。**
    # 上一期没封账，本期凭证再去改上期已经变动的数字，账就对不回来了 ——
    # 这正是「先结账、再记下个月」这条规矩要挡住的事。
    # 账套的第一个期间没有上一期，不受此限制。
    prev_ords = {p.ord - 1 for p in rows if not p.locked}
    prev_rows = (
        db.execute(
            select(Period).where(
                Period.book_id.in_({p.book_id for p in rows}),
                Period.ord.in_(prev_ords),
            )
        ).scalars().all()
        if prev_ords
        else []
    )
    prev_map = {(p.book_id, p.ord): p for p in prev_rows}
    pending = {}
    for p in rows:
        if p.locked:
            continue
        prev = prev_map.get((p.book_id, p.ord - 1))
        if prev is not None and not prev.locked:
            pending[p.code] = prev.code
    if pending:
        detail = "；".join(f"{code} 需先结账 {prev}" for code, prev in sorted(pending.items()))
        raise ValueError(f"上一期尚未结账，不能记账：{detail}")

    count = 0
    for v in vouchers:
        if v.status == "POSTED":
            continue
        dr = sum(money(e.amount) for e in v.entries if e.direction == "DEBIT")
        cr = sum(money(e.amount) for e in v.entries if e.direction == "CREDIT")
        if dr != cr:
            raise ValueError(f"凭证 {v.voucher_no} 借贷不平衡，不能记账")
        v.status = "POSTED"
        v.posted_at = datetime.now(timezone.utc)
        v.posted_by = user.username
        count += 1
    db.flush()
    # book_id 用请求带来的，不取 vouchers[0].book_id：批次里的凭证经过
    # _resolve_vouchers 已保证同属一个账套，但审计记录的归属不该依赖
    # 「碰巧第一张是谁」这种实现细节。
    log(db, action="POST", entity="voucher", entity_id="batch", user=user,
        book_id=book_id, detail=f"记账 {count} 张凭证", after={"ids": voucher_ids})
    if commit:
        db.commit()
    return count


def unpost_vouchers(db: Session, book_id: int, voucher_ids: list[int], user: SysUser,
                    commit: bool = True) -> int:
    """批量反记账。归属约束与 post_vouchers 同一条，不能一边松一边紧。"""
    if not voucher_ids:
        return 0
    vouchers = _resolve_vouchers(db, book_id, voucher_ids)
    period_ids = {v.period_id for v in vouchers}
    locked = db.execute(
        select(Period.year, Period.month).where(Period.id.in_(period_ids), Period.locked.is_(True))
    ).all()
    if locked:
        codes = [period_label(y, m) for y, m in locked]
        raise ValueError(f"会计期间 {', '.join(codes)} 已结账，不能反记账")

    count = 0
    for v in vouchers:
        if v.status == "UNPOSTED":
            continue
        v.status = "UNPOSTED"
        v.posted_at = None
        v.posted_by = None
        count += 1
    db.flush()
    log(db, action="UNPOST", entity="voucher", entity_id="batch", user=user,
        book_id=book_id, detail=f"反记账 {count} 张凭证", before={"ids": voucher_ids})
    if commit:
        db.commit()
    return count


# --------------------------------------------------------------------- 查询


# 列表排序档位（用户 2026-10-13 定的）：默认「凭证号倒序」，
# 表头可在「凭证号顺序 / 凭证号倒序 / 日期顺序 / 日期倒序」之间切换。
#
# 凭证号排序**必须先按期间**：``uq_voucher_period_no`` 是期间内唯一，
# 记001 与 记012 分属两个月，跨月比大小没有意义。所以排序键是
# (期间 ord, 号段数字)，日期排序则直接比 voucher_date。
#
# 号段数字要从字符串里抠出来再**按整数**比 —— 直接按 voucher_no 字符串排，
# "记100" < "记099"（字典序），跟 next_voucher_no 踩过的 max() 同一个坑。
# 用 PostgreSQL 的 regexp 抽出尾部数字再 cast 成 int。
SORTS = ("no_asc", "no_desc", "date_asc", "date_desc")
DEFAULT_SORT = "no_desc"


def _no_seq():
    r"""凭证号里的序号，按整数排序用。解析不出序号时给 -1，让它排最前。

    原来这里是 ``regexp_replace(no, '^[^\d]*(\d+)$', '\\1')``，看着有
    ``coalesce`` 兜底，实际上**兜底是死的**：PostgreSQL 的 ``regexp_replace``
    在**无匹配时返回原字符串**，不是 NULL。所以对 "wochakan" 这种一个数字
    都没有的号，它原样返回 "wochakan"，紧接着 ``CAST(... AS INTEGER)`` 抛
    ``invalid input syntax for type integer`` —— 外层的 coalesce 永远等不到
    NULL。2026-10-19 生产事故：优境 9 月有一张凭证号被人工改成 "wochakan"，
    凭证列表的**四种排序全部 500**（四种都带这个排序键），整月打不开。

    ``substring(no from '(\d+)$')`` 才是这里该用的：它无匹配时返回 NULL，
    ``cast`` 得到 NULL，``coalesce`` 终于能把它收成 -1。于是脏号只是排到
    最前面（而且 create_voucher 已经在入口拦住了新脏号），不再拖垮整个列表。
    """
    digits = func.substring(Voucher.voucher_no, r"(\d+)$")
    return func.coalesce(func.cast(digits, Integer), -1)


def _apply_sort(stmt: Select, sort: str) -> Select:
    """按档位排序。未知档位退回默认，不给 400 —— 排序是显示偏好，不是数据错误。"""
    if sort == "no_asc":
        return stmt.order_by(Period.ord.asc(), _no_seq().asc(), Voucher.id.asc())
    if sort == "no_desc":
        return stmt.order_by(Period.ord.desc(), _no_seq().desc(), Voucher.id.desc())
    if sort == "date_asc":
        return stmt.order_by(Voucher.voucher_date.asc(), _no_seq().asc(), Voucher.id.asc())
    # date_desc / 未知值
    return stmt.order_by(Voucher.voucher_date.desc(), _no_seq().desc(), Voucher.id.desc())


def query_vouchers(
    db: Session,
    book_id: int,
    *,
    from_period: Period | None = None,
    to_period: Period | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    source: str | None = None,
    keyword: str | None = None,
    min_amount: Decimal | None = None,
    max_amount: Decimal | None = None,
    subject_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
    sort: str = "no_desc",
) -> tuple[list[Voucher], int]:
    stmt = select(Voucher).where(Voucher.book_id == book_id)

    if from_period:
        stmt = stmt.where(
            Voucher.period_id.in_(
                select(Period.id).where(Period.book_id == book_id, Period.ord >= from_period.ord)
            )
        )
    if to_period:
        stmt = stmt.where(
            Voucher.period_id.in_(
                select(Period.id).where(Period.book_id == book_id, Period.ord <= to_period.ord)
            )
        )
    if date_from:
        stmt = stmt.where(Voucher.voucher_date >= date_from)
    if date_to:
        stmt = stmt.where(Voucher.voucher_date <= date_to)
    if status:
        stmt = stmt.where(Voucher.status == status)
    if source:
        stmt = stmt.where(Voucher.source == source)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Voucher.voucher_no.ilike(like),
                Voucher.remark.ilike(like),
                Voucher.id.in_(
                    select(Entry.voucher_id).where(Entry.summary.ilike(like))
                ),
            )
        )
    if subject_id:
        # **这里刻意只按 subject_id 过滤，不补 book_id。**
        #
        # 迁移 0011 的 docstring 建议补上 ``Entry.book_id == book_id``，
        # 好让这条查询复用 ``ix_entry_subject_ord``（book_id, subject_id,
        # period_ord）的前导列、从而不再需要 ``ix_entry_subject_voucher``。
        # 那个建议**实测是错的**（见 0011 docstring 里补记的对照数据）：
        # 外层是「账套下全部凭证」，内层这个子查询被当半连接探测 15,000 次。
        #   * ``ix_entry_subject_voucher``（subject_id, voucher_id）
        #     是 **Index Only Scan**，探测一次命中一条记录页，且能整体
        #     物化成哈希半连接 → 17.3 ms
        #   * ``ix_entry_subject_ord`` 是普通 **Index Scan**，每次探测都要
        #     多走一次索引项比较（loops=15000） → **395.4 ms（慢 23 倍）**
        #
        # 「能复用别的索引」不等于「用那个索引更快」—— 半连接探测次数
        # 才是这里的主导成本。
        stmt = stmt.where(Voucher.id.in_(select(Entry.voucher_id).where(Entry.subject_id == subject_id)))
    if min_amount is not None or max_amount is not None:
        # 「金额」筛的是**某一条分录**的金额，不是整张凭证的合计 ——
        # 只要有一行落在区间里，这张凭证就该被捞出来。
        # 所以这里不做求和，直接拿单条 entry 的 amount 比。
        amt = select(Entry.voucher_id).where(Entry.book_id == book_id)
        if min_amount is not None:
            amt = amt.where(Entry.amount >= money(min_amount))
        if max_amount is not None:
            amt = amt.where(Entry.amount <= money(max_amount))
        stmt = stmt.where(Voucher.id.in_(amt))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    # 凭证号排序要按期间先后（记001 与 记012 分属不同月，跨月比大小无意义），
    # 所以 join Period 拿到 ord。join 放在 count **之后**：count 只关心命中
    # 多少行，join 一个 1:1 的 period 不会改变结果，却会让 count 的子查询
    # 多带一张表 —— 没必要。
    stmt = stmt.join(Period, Voucher.period_id == Period.id)
    stmt = (
        _apply_sort(stmt, sort)
        .offset((page - 1) * page_size)
        .limit(page_size)
        # 分录一次性捞出来（审计 P3-12）。这里原来是
        # ``for v in rows: db.refresh(v)`` —— 一个 200 行的页就是 200 次往返，
        # 而 ``refresh`` 刷的还是**凭证自己那一行**，分录仍然在序列化时逐条懒加载，
        # 等于 200 次白跑 + 200 次懒加载。``selectinload`` 把它压成 2 条语句。
        #
        # 为什么当初要 refresh：怀疑 identity map 里是旧值。但这个路由是纯读的，
        # 前面既没有 commit 也没有别的事务写入会话（``expire_on_commit`` 只在
        # 提交时生效），refresh 保护不到任何真实场景。
        .options(selectinload(Voucher.entries))
    )
    rows = list(db.scalars(stmt).unique().all())
    return rows, total
