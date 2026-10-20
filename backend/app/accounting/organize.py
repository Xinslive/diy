"""整理凭证：重新排序、重新编号。

整理只做两件事，方式由用户在页面上选：

1. ``RENUM``  —— 保持当前顺序，把号段压紧成 001..N（补断号）。
2. ``SORT_DATE`` —— 先按日期升序排，再压紧编号。

设计取舍
--------
* **两阶段返回**：``plan_organize`` 只算不改，``apply_organize`` 才落库。
  财务改号影响已经打印出来的纸质凭证，必须先看清差异再确认。
* **临时号避让**：``uq_voucher_period_no`` 是即时唯一约束，直接改号会在交换
  （记001 <-> 记002）时撞库。因此先整体写到临时号并 flush，再写最终号。
* **系统凭证一起排，但永远垫底**：结转损益（source=CARRY_FORWARD）由月末任务
  生成，跟手工凭证抢号会让期末那几张看起来像随手记的。所以排序键把
  CARRY_FORWARD 放在最高优先级（排在最后），编号也就落在最后两个号。
* **按期间分组**：整理范围可以是单个期间，也可以是整个账套（自动按期间分组），
  不会把凭证号排到别的期间去。
* **体检不占一个入口**：``check_period`` 单独提供，给凭证列表页做顶部提醒、
  也给记账/结账当闸门，不再作为一种「整理方式」让用户去选。
* **已记账的不动**：已记账凭证占着的号原样保留，剩下的凭证只在空出来的号里
  重新发号 —— 否则改到一半撞上「这张已经记账了」，账就死在一半。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.accounting.audit_helper import log, voucher_snapshot
from app.accounting.util import constraint_name, period_label
from app.models import Entry, Period, Subject, SysUser, Voucher

# 凭证号形如「记0001」，前缀是 1~2 个汉字，序号是数字（补零到 NO_WIDTH 位）
NO_PATTERN = re.compile(r"^(?P<prefix>[^\d]{1,4})(?P<seq>\d+)$")
# **序号补零到几位，全仓只有这一个数说了算**（2026-10-16 由三位改回四位）。
#
#   发号 / 插入顺延 / 整理重编 / 体检的断号清单 / 一次性补零脚本，
#   五处各自写死过三位的格式串，改一处就留下两三种宽度的号混在同一个列表里。
#   位数是**排版**，不是账：发号、断号体检、整理全都只按整数序号比
#   （split_no），所以改宽窄不影响大小关系，也就不会撞上唯一约束。
#
#   三位（记008）和四位（记0008）在列表里并排时，序号那几列没法上下对齐，
#   扫一眼数不出号断在哪 —— 四位给「一个月要记几百张」留出余量。
NO_WIDTH = 4
PARK_PREFIX = "∥"  # ∥ 临时占位前缀，保证永不与真实凭证号冲突

# ------------------------------------------------------------------ 凭证号归一
#
# 用户 2026-10-19 定的形态是「记0001」——凭证字后面**不带任何分隔符**。
# 而导入的用友序时账是「记-0001」，导入环节原先原样入库，于是同一个期间里
# 两种形态并存。后果不是"看着不整齐"，是**发号失明**：
# ``vouchers._prefix_seq_expr`` 的正则是 ``^记(\d+)$``，要求凭证字后紧跟数字，
# 带横杠的那几张一条都不匹配 → ``next_voucher_no`` 的 max 得 NULL → 兜底成 0
# → 在一个已经记到 0004 的期间里重新发出「记0001」。实测（迁移前优境 9 月）：
# ``/vouchers/next`` 返回 记0001，存下去就成了同期间里第二个「序号 1」。
#
# 所以分隔符必须在**入口**剥掉，而不是在读取时兼容。这个函数幂等：
# 记-0001 和 记0001 都归一成 记0001，反复调用不会变形。
#
# 前缀长度刻意不设上限（``NO_PATTERN`` 限 4 个字是给**解析**用的，
# 这里只需要切出「非数字部分 + 尾部数字」）：宁可放过一个长凭证字，
# 也不要因为前缀多一个字就把原本能导进来的表整批判失败。
_NO_SPLIT = re.compile(r"^(?P<prefix>[^\d]+)(?P<seq>\d+)$")
_NO_SEPARATOR = re.compile(r"[-_‐‑‒–—−－·•.、/\\|]+$")


def normalize_no(voucher_no: str | None) -> str | None:
    """归一成「凭证字 + 补零序号」。空或解析不出序号时返回 ``None``（不抛）。

    调用方分两种：入口校验用 ``require_no``（解析不出要报错给人看），
    读存量数据用本函数（解析不出就当它没有序号，交给各自的兜底）。
    """
    s = (voucher_no or "").strip()
    if not s:
        return None
    m = _NO_SPLIT.match(s)
    if not m:
        return None
    prefix = _NO_SEPARATOR.sub("", m.group("prefix"))
    if not prefix:
        return None
    return f"{prefix}{int(m.group('seq')):0{NO_WIDTH}d}"


def require_no(voucher_no: str | None, *, field: str = "凭证号") -> str | None:
    """入参校验。空值放行（那表示"走自动发号"），非空必须能归一。

    没有这道闸时，任何字符串都能落库，而凭证列表的排序键要对凭证号做
    ``CAST(... AS INTEGER)`` —— 一个脏号就能让**整个期间的凭证列表 500**
    （2026-10-19 生产事故：人工把凭证号改成 "wochakan"，四种排序全部报错）。
    """
    s = (voucher_no or "").strip()
    if not s:
        return None
    out = normalize_no(s)
    if out is None:
        raise ValueError(
            f"{field}「{s}」不是「凭证字 + 序号」，"
            f"请写成 记0001 这样：凭证字后直接跟数字，中间不要符号"
        )
    return out
# 与 accounting/vouchers.py 同一份判据：撞号要认约束，不能「凡 IntegrityError
# 都当撞号」（util.constraint_name 的注释里写了为什么）。
_NO_UNIQUE = "uq_voucher_period_no"

STRATEGIES = ("RENUM", "SORT_DATE")


@dataclass
class Change:
    """一张凭证的改号前后对照。"""

    voucher_id: int
    old_no: str
    new_no: str
    voucher_date: str
    status: str
    source: str
    # 是不是期末结转损益（见 is_carry_forward）。**构造时就定下来**，
    # 不能等到给页面的那个排序里再去查凭证 —— 那时手上只有一个 Change，
    # 摘要和分录都拿不到，只剩 source，而导入进来的结转凭证 source 是 IMPORT。
    # 结果就是「号排对了、页面显示的顺序不对」，两套口径打架。
    tail: bool = False

    @property
    def changed(self) -> bool:
        return self.old_no != self.new_no


@dataclass
class PeriodPlan:
    period_id: int
    label: str
    changes: list[Change] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "period_id": self.period_id,
            "period": self.label,
            "changes": [
                {
                    "voucher_id": c.voucher_id,
                    "old_no": c.old_no,
                    "new_no": c.new_no,
                    "voucher_date": c.voucher_date,
                    "status": c.status,
                    "source": c.source,
                    "changed": c.changed,
                }
                for c in self.changes
            ],
            "gaps": self.gaps,
            "changed_count": sum(1 for c in self.changes if c.changed),
        }


# --------------------------------------------------------------------- 解析


def split_no(voucher_no: str) -> tuple[str, int]:
    """拆出 (前缀, 序号)。解析不了时返回 (原串, 0) 以便原样保留。"""
    m = NO_PATTERN.match((voucher_no or "").strip())
    if not m:
        return (voucher_no or "").strip(), 0
    return m.group("prefix"), int(m.group("seq"))


def _seq_of(voucher_no: str) -> int:
    _prefix, seq = split_no(voucher_no)
    return seq


# --------------------------------------------------------------------- 收 / 付 / 转账

# 「现金类」科目：1001 库存现金、1002 银行存款（含 1002xx 明细级）、
# 1012 其他货币资金（第三方支付平台备付金等）。**按编码前缀判定**。
#
# 为什么不是给 Subject 加一个 is_cash 标志位：明细级（100201/100202）
# 和父级（1002）是不同的行，前缀匹配一次命中整棵子树；标志位则要么每个
# 账套逐个勾，要么靠「父级为现金 ⇒ 子级也为现金」的级联推导 —— 而后者
# 在用户新建子科目时又得补一道回填。前缀是会计准则里已经定死的分类，
# 拿来当判据既不用加列、也不会漏。
CASH_SUBJECT_PREFIXES = ("1001", "1002", "1012")

RECEIPT = 0   # 收：钱进账（现金/银行存款净增加），或确认了收入
PAYMENT = 1   # 付：钱出账（现金/银行存款净减少），或确认了费用
TRANSFER = 2  # 转账：现金进出相抵（提现、银行转户），或收支两边打平


def cash_flow_kind(entries, income_of=None) -> int:
    """从分录判定这张凭证是收、付还是转账。

    判据是**现金类科目的净额**（借方合计 − 贷方合计），不引入凭证字、
    不落库、不改表：

    * 净额 > 0（钱净进账）→ 收 RECEIPT
    * 净额 < 0（钱净出账）→ 付 PAYMENT
    * 净额 = 0 且碰过现金 → 转账 TRANSFER（内部划转）
    * 压根没碰现金 → 转 _kind_by_profit 按损益方向定

    **为什么没碰现金的不能一律当转账。** 挂应收确认管理费、计提折旧、
    停车费支出这些凭证一笔现金都不碰，业务性质却清清楚楚是收入或支出。
    星悦荟商管 2026 年 9 月记-0045「管理费2026-09-25-2026-12-24/周淑琴」
    就是借应收账款、贷主营业务收入 —— 债权进来了，钱就在路上。把它和
    「借应收账款 / 贷其他应付款」这种纯往来划拨混在同一段，对账时就找不着它了。

    **为什么用净额而不是「看现金类出现在哪一边」。** 一张凭证里只要有一笔
    内部划转（提现、银行转户、微信↔银行），原来那条「借贷两边都有就算转账」
    的规则就会把它整张打成转账 —— 而业务上它明明是一笔收付。真实账里
    最常见的是这种（启明物业 13 张、星悦荟商管 39 张）::

        借 财务费用 186   贷 银行存款 186     ← 手续费，付
        借 银行存款 980   贷 投资收益 980     ← 利息，收

    摘要写的是「银行手续费」，业务性质是**收到利息**，却因为夹了一笔现金类
    对外支付而被归到转账段，钱按银行流水对账时的阅读顺序就断了。
    用户 2026-10-16 定的口径是：**同时符合收和转的，认定为收。**

    净额这个判据正好把这句话落成代码，而且不需要为「转」单开一个分支：

    * 手续费 + 利息 → 980 − 186 = +794 → 收 ✓（同属收和转，认定为收）
    * 纯对倒（借 库存现金 500 / 贷 银行存款 500）→ 净额 0 → 转账 ✓
    * 混合收款里夹一笔内部划转（微信 → 银行）→ 对倒部分自己抵掉，
      净额只剩真正的收付 ✓
    * 「折旧 + 停车费收入 + 在途↔尚宝 对倒」这种打包凭证 → 现金净额 0
      → 转账 ✓

    净额同时天然处理了「只在一侧出现」的情况：只有借方时净额必然 > 0，
    只有贷方时必然 < 0，不必再单独数 has_debit / has_credit。
    内部对抵的那部分正是「转账」的成分，两笔现金分录一借一贷就把它们
    抵消掉了 —— 留在净额里的只有真正的钱进出。
    """
    net = Decimal(0)
    has_cash = False
    for e in entries:
        code = getattr(getattr(e, "subject", None), "code", "") or ""
        if not code.startswith(CASH_SUBJECT_PREFIXES):
            continue
        has_cash = True
        amount = Decimal(e.amount or 0)
        net += amount if e.direction == "DEBIT" else -amount

    if net > 0:
        return RECEIPT
    if net < 0:
        return PAYMENT
    if has_cash:
        # 碰过现金但进出相抵：内部划转（提现、银行转户、在途↔到账）。
        # 这跟「压根没碰现金」是两回事，不该按损益去猜它的性质。
        return TRANSFER
    return _kind_by_profit(entries, income_of)


def _kind_by_profit(entries, income_of) -> int:
    """**完全不涉及现金**的凭证：按损益方向定收付（2026-10-16 用户定的口径）。

    挂应收确认管理费收入（借 应收账款 / 贷 主营业务收入 + 贷 预收账款）、折旧、
    停车费支出这些凭证一笔现金都不碰，按上一版「不涉及现金就是转账」会全被扫进
    转账段 —— 而记-0045 那张「管理费…/周淑琴」业务性质明明是收入。

    **确认收入就是钱（债权）进来，确认费用就是钱（负债）出去**，所以：

    * 收入方金额 > 费用方金额 → 收
    * 费用方金额 > 收入方金额 → 付
    * 两边相等（成本分摊一类的对冲）或压根没有损益科目 → 转账

    「收入方 / 费用方」是按**科目类型**分的组，不是按借贷方向：一张挂应收确认
    管理费的凭证，收入方就是「主营业务收入」那一条（哪怕它是贷方），而不是
    「贷方的所有科目」。

    ``income_of`` 是「这个科目是不是收入方」的判定函数，由 ``plan_organize``
    按父级链建好后传进来 —— 明细科目的名字是「157」「503」这种编号，
    光看分录自己的科目名什么也判不出来。传 None 就一律回转账（老的保守行为）。
    """
    if income_of is None:
        return TRANSFER

    income = expense = Decimal(0)
    for e in entries:
        subject = getattr(e, "subject", None)
        if subject is None:
            continue
        if getattr(subject, "category", "") not in ("PROFIT", "COST"):
            continue
        # 按**科目类型**分组，不看借贷方向。方向在这里不参与判断：收入类科目的
        # 金额整体算收入方、费用类整体算费用方，「收入方为主」这句话问的是
        # 这张凭证主要在确认收入还是在确认成本费用，不是问钱从哪边走。
        # 资产/负债科目（应收账款、预收账款、累计折旧）两边都不进 —— 它们只是
        # 收入或费用的对手方，本身不代表任何方向。
        amount = Decimal(e.amount or 0)
        if income_of(subject):
            income += amount
        else:
            expense += amount

    if income > expense:
        return RECEIPT
    if expense > income:
        return PAYMENT
    return TRANSFER


#: 回溯顶层时最多走几跳。正常科目树三层就到根（用友 4-2-2 就是三层），
#: 留到 16 跳是给「导入的历史科目表层级更深」留余量，同时也是脏数据里 parent
#: 指回自己形成的环的保险丝。
_ROOT_WALK_GUARD = 16

#: 顶层科目名里带这些字眼的，判成「收入方」。收入类科目是「钱进来」，
#: 其余损益类（主营业务成本 / 税金及附加 / 销售费用 / 营业外支出 …）都算费用方 ——
#: 所以这是**白名单**而不是黑名单：漏掉一个新造的收入科目，最坏是把它算成费用，
#: 而不是把一堆成本费用误当成收入。
INCOME_NAME_HINTS = ("收入", "收益", "补偿")


def build_income_lookup(db, book_id: int):
    """建「科目 id → 是不是收入方」的判定函数。

    **为什么要回溯到顶层。** 星悦荟那类明细科目的名字就是「157」「503」
    （单元编号），名字里没有任何信息；「是不是收入」这件事挂在顶层科目上 ——
    5001 主营业务收入物业管理费 / 5004 停车费支出 / 5005 停车费收入。
    按名字判只能对顶层判，所以每个科目都顺着 parent_id 一路走到根。

    一次查完整个账套的科目（星悦荟 1390 个），在 Python 里走链，避免每条分录
    回数据库 —— 整理是个「点一次、等一会儿」的动作，这种延迟最容易被误读成卡死。
    """
    rows = list(db.scalars(select(Subject).where(Subject.book_id == book_id)).all())
    parent = {s.id: s.parent_id for s in rows}
    names = {s.id: s.name for s in rows}
    root: dict[int, str] = {}

    def root_name(sid: int) -> str:
        """这个科目挂在哪棵树的根上，根的名字是什么。

        走过的**每一环**都写进 root 缓存 —— 星悦荟 1390 个科目绝大多数挂在二层
        以下，不缓存就是每个科目各走一遍链（O(科目数×深度)）。
        """
        if sid in root:
            return root[sid]
        chain: list[int] = []
        cur, name = sid, ""
        for _ in range(_ROOT_WALK_GUARD):
            if cur in root:
                name = root[cur]
                break
            chain.append(cur)
            nxt = parent.get(cur)
            if not nxt:
                name = names.get(cur, "")
                break
            cur = nxt
        else:
            # 走满保险丝还没到根：多半是 parent 指回了自己。就地截断，把最后一环
            # 当根 —— 判据退化成只看自己这层的名字，也好过让一条坏科目把整个整理
            # 卡死在这里。
            name = names.get(chain[-1], "") if chain else ""
        for node in chain:
            root[node] = name
        return name

    def income_of(subject) -> bool:
        name = root_name(subject.id) or subject.name or ""
        return any(h in name for h in INCOME_NAME_HINTS)

    return income_of


# 结转损益凭证的识别标记（用户 2026-10-19 给的：摘要写着「期间损益结转」）。
#
# 只写「结转」两个字太宽 —— 手工凭证里「结转上月的余额」这类摘要会被误判，
# 把一张正常凭证扔到号段末尾。命中最长的那个短语，边界最窄。
_CARRY_FORWARD_MARKERS = ("期间损益结转", "结转损益")


def is_carry_forward(voucher: Voucher) -> bool:
    """这张是不是期末结转损益的凭证。

    原来只认 ``source == "CARRY_FORWARD"``，也就是只认**系统**生成的那些。
    但用户是把别的账套导进来的：那些结转凭证 ``source`` 是 ``IMPORT``
    （实测生产库里 CARRY_FORWARD 凭证数为 0，优境 9 个月每月一张的结转凭证
    全是导入的），于是 tail 恒为 0，整理时结转凭证被排进号段中间，
    一眼认不出期末那几张 —— 用户 2026-10-19 要求它们一律垫底。

    所以补一条按摘要认的口径。``source`` 仍然是首选判据：它是系统给的标记，
    比文本可靠；文本只是给导入的那批兜底。
    """
    if voucher.source == "CARRY_FORWARD":
        return True
    if any(m in (voucher.remark or "") for m in _CARRY_FORWARD_MARKERS):
        return True
    return any(m in (e.summary or "") for e in voucher.entries
               for m in _CARRY_FORWARD_MARKERS)


def _sort_key(voucher: Voucher, strategy: str, income_of=None):
    """排序键。最高一位永远是「是不是结转损益」：1 排最后。

    月末的结转凭证是系统生成的，收入类、成本费用类各一张，落在最后两个号上，
    一眼就能在号段末尾认出期末那几张，不必去翻摘要。导入进来的结转凭证同样
    适用（见 ``is_carry_forward``）。
    """
    tail = 1 if is_carry_forward(voucher) else 0
    if strategy == "SORT_DATE":
        # 同一天内按「先收后付再转账」排（用户 2026-10-13 定的口径）：
        # 这一天钱先怎么进来、再怎么出去、最后才是内部划转 —— 这正是
        # 银行流水对账时的阅读顺序。三类之内仍按录入顺序（id）兜底，
        # 保证排序稳定、可复现。
        kind = cash_flow_kind(voucher.entries, income_of)
        return (tail, voucher.voucher_date.toordinal(), kind, voucher.id)
    # RENUM 保持**当前号段**顺序，不是 id 顺序。
    #
    # 原来这里是 ``voucher.id``。正常录入时 id 与号同增，两者一致；但
    # 「插入凭证」让老凭证保留 id、号却 +1，新凭证拿中间的号而 id 最大，
    # 于是 id 顺序 ≠ 号段顺序。RENUM 再按 id 排，会把刚插到中间的那张
    # 甩到末尾 —— 而 RENUM 的全部意义就是「不重排」。实测：插入后体检是
    # 干净的（插入的凭证与参照凭证同一天，时序检查判它没错），RENUM 放行，
    # 执行完当月从 clean 变成「时序不对 1 处」。按号排才等于「当前顺序」。
    return (tail, _seq_of(voucher.voucher_no), voucher.id)


# --------------------------------------------------------------------- 体检


class _VoucherRow(NamedTuple):
    """体检真正用到的三个字段。

    以前这里跑的是 ``select(Voucher)``：结账时 ``assert_tidy`` 会对**整条
    期间链**逐期调用，把每期的凭证全建成 ORM 对象并塞进 identity map，
    而体检只用到凭证号、日期、状态这三个字段。十年账套结一次 12 月就是
    几万张白建的 Voucher。
    """
    no: str
    voucher_date: date
    status: str


def _time_order_issues(by_seq: list[tuple[_VoucherRow, str, int]]) -> list[dict]:
    """按凭证号排一遍，看日期是不是也一路往后走。

    记账是按时间顺序记的，凭证号就是时间顺序的编号。号大的那张日期反而更早，
    说明这个月是「先记了后面的，又回头补了前面的」—— 号段乱了，纸质凭证按号
    装订也对不上，月底不能就这么封账。
    """
    issues: list[dict] = []
    prev: _VoucherRow | None = None
    for v, _prefix, _seq in by_seq:
        if prev is not None and v.voucher_date < prev.voucher_date:
            issues.append({
                "voucher_no": v.no,
                "voucher_date": v.voucher_date.isoformat(),
                "prev_no": prev.no,
                "prev_date": prev.voucher_date.isoformat(),
            })
        prev = v
    return issues


def _analyze_rows(rows: list[_VoucherRow]) -> dict:
    """从「凭证号 / 日期 / 状态」的集合算出断号、重号、解析不出的号、时序。

    **纯函数，不查库。** ``check_period`` 查完库调它；
    ``irreparable_gaps`` 拿「删掉之后」那份假想集合也调它 ——
    **同一份号段语义只有这一处实现**，两边算出来的断号必然一致
    （用户看到的「断在 记0002」和体检报出来的「断号 1 个：记0002」必须是同一个）。
    """
    parsed = [(v, *split_no(v.no)) for v in rows]
    with_seq = [(v, p, s) for v, p, s in parsed if s > 0]

    prefixes = {p for _, p, _ in with_seq}
    gaps: list[str] = []
    dup: list[str] = []
    unparsed: list[str] = []

    for v, p, s in parsed:
        if s == 0:
            unparsed.append(v.no)

    if prefixes:
        _, prefix, top_seq = max(with_seq, key=lambda t: t[2])
        used = {s for _v, p, s in with_seq if p == prefix}
        missing = [n for n in range(1, top_seq + 1) if n not in used]
        gaps = [f"{prefix}{n:0{NO_WIDTH}d}" for n in missing]
        seen: set[str] = set()
        for v, p, s in with_seq:
            if p != prefix:
                continue
            if v.no in seen:
                dup.append(v.no)
            seen.add(v.no)

    by_seq = sorted(with_seq, key=lambda t: t[2])
    out_of_order = _time_order_issues(by_seq)
    posted = [v.no for v in rows if v.status == "POSTED"]

    return {
        "total": len(rows),
        "prefixes": sorted(prefixes),
        "gaps": gaps,
        "duplicates": dup,
        "unparsed": unparsed,
        "time_out_of_order": out_of_order,
        "posted": posted,
    }


def check_period(db: Session, period: Period) -> dict:
    """一个期间的「整理就绪」体检：断号 + 时序。全程只读。

    这两项任一不干净，这个月就不该记账、也不该结账：号段有洞或者号序跟日期
    对不上，纸质凭证按号装订就跟系统对不上，月底封了账再想改就只能作废重记。

    结转损益算不算断号/时序？算。它照样占着号，缺了就是缺了。
    """
    rows = [
        _VoucherRow(*r) for r in db.execute(
            select(Voucher.voucher_no, Voucher.voucher_date, Voucher.status)
            .where(Voucher.period_id == period.id).order_by(Voucher.id)
        ).all()
    ]
    a = _analyze_rows(rows)

    gaps = a["gaps"]
    dup = a["duplicates"]
    unparsed = a["unparsed"]
    out_of_order = a["time_out_of_order"]
    prefixes = a["prefixes"]

    blocking: list[str] = []
    if gaps:
        blocking.append(f"断号 {len(gaps)} 个：{'、'.join(gaps)}")
    if dup:
        blocking.append(f"凭证号重复：{'、'.join(sorted(set(dup)))}")
    if unparsed:
        blocking.append(f"凭证号不是「凭证字+数字」：{'、'.join(unparsed)}")
    if len(prefixes) > 1:
        blocking.append(f"混用了多个凭证字：{'、'.join(sorted(prefixes))}")
    if out_of_order:
        blocking.append(
            f"时序不对 {len(out_of_order)} 处："
            + "、".join(f"{i['prev_no']}({i['prev_date']}) → {i['voucher_no']}({i['voucher_date']})"
                        for i in out_of_order[:3])
            + ("…" if len(out_of_order) > 3 else "")
        )

    return {
        "period_id": period.id,
        "period": period_label(period.year, period.month),
        "total": a["total"],
        "prefixes": a["prefixes"],
        "gaps": a["gaps"],
        "duplicates": a["duplicates"],
        "unparsed": a["unparsed"],
        "time_out_of_order": a["time_out_of_order"],
        "posted": a["posted"],
        "blocking": blocking,
        "clean": not blocking,
    }


# --------------------------------------------------------------------- 计划


def plan_organize(
    db: Session,
    *,
    book_id: int,
    period_id: int | None = None,
    strategy: str = "SORT_DATE",
) -> list[PeriodPlan]:
    """算出整理方案但不落库。``period_id`` 为空表示整个账套。

    结转损益不再有「参不参与」的开关：它一定参与，而且一定排在最后。
    """
    if strategy not in STRATEGIES:
        raise ValueError(f"未知的整理方式：{strategy}")

    # 「完全没碰现金」的凭证要按损益方向定收付，得知道哪个科目是收入方。这件事
    # 对整批凭证是同一份答案，所以只查一次科目表，之后每张凭证都在内存里判。
    # RENUM 不重排，用不上，白查一轮 1390 个科目不划算。
    income_of = build_income_lookup(db, book_id) if strategy == "SORT_DATE" else None

    period_stmt = select(Period).where(Period.book_id == book_id).order_by(Period.ord)
    if period_id is not None:
        period_stmt = period_stmt.where(Period.id == period_id)
    periods = list(db.scalars(period_stmt).all())
    if not periods:
        raise ValueError("没有可整理的会计期间")

    plans: list[PeriodPlan] = []
    for period in periods:
        rep = check_period(db, period)
        plan = PeriodPlan(period_id=period.id, label=period_label(period.year, period.month))
        plan.gaps = rep["gaps"]

        # 「按当前顺序重新编号」的前提是当前顺序本身就对。
        # 时序不对还照着当前顺序压紧号，等于把乱的号段原样固化成整齐的乱。
        if strategy == "RENUM" and rep["time_out_of_order"]:
            raise ValueError(
                f"{rep['period']} 有 {len(rep['time_out_of_order'])} 处凭证号与日期不同序，"
                "不能「按当前顺序重新编号」；请改用「按日期升序重排并编号」先排好时序"
            )

        # entries + subject 一次取出来：_sort_key 的「先收后付再转账」要逐张看
        # 分录里现金类科目在哪个方向。靠懒加载的话这里是 N+1（每张凭证一条
        # entry 查询 + 每条 entry 一条 subject 查询），一个月几百张就是上千次
        # 往返 —— 整理是个「点一次、等一会儿」的动作，这种延迟最容易被误读
        # 成页面卡死。两级 selectinload 压成 2 条查询。
        rows = list(db.scalars(
            select(Voucher).where(Voucher.period_id == period.id)
            .options(selectinload(Voucher.entries).selectinload(Entry.subject))
        ).all())

        # 已记账的凭证不动：它们占着的号原样留着，其余凭证只在空出来的号里发号。
        # 直接把已记账的排除在外再压紧成 1..N，N 一多半就会踩到它们占的号上。
        movable = [v for v in rows if v.status != "POSTED"]
        reserved = {_seq_of(v.voucher_no) for v in rows if v.status == "POSTED"}

        prefix = _dominant_prefix(rows)
        ordered = sorted(movable,
                        key=lambda v: _sort_key(v, strategy, income_of))
        n = 0
        for v in ordered:
            n += 1
            while n in reserved:
                n += 1
            plan.changes.append(
                Change(v.id, v.voucher_no, f"{prefix}{n:0{NO_WIDTH}d}",
                       v.voucher_date.isoformat(), v.status, v.source,
                       tail=is_carry_forward(v))
            )
        for v in rows:
            if v.status == "POSTED":
                plan.changes.append(
                    Change(v.id, v.voucher_no, v.voucher_no,
                           v.voucher_date.isoformat(), v.status, v.source,
                           tail=is_carry_forward(v))
                )

        # 交给页面的顺序 = 整理完之后的号段顺序，而不是「先排可动的、最后补已记账的」。
        # 这两件事在混合场景下不是一回事：整月的凭证都记完账时，可动的那批是空的，
        # 页面拿到的就成了数据库的原始存储顺序 —— 日期是乱的、结转损益夹在中间，
        # 看着像整理排错了，其实只是列表没排。结转损益仍旧压在最末尾。
        #
        # 排的是 ``c.tail``（构造时按 is_carry_forward 定的），**不是** ``c.source``：
        # 导入进来的结转凭证 source 是 IMPORT，只认 source 的话这一处照样漏。
        plan.changes.sort(key=lambda c: (1 if c.tail else 0, _seq_of(c.new_no)))

        plans.append(plan)

    return plans


def _dominant_prefix(rows: list[Voucher]) -> str:
    """取出现次数最多的前缀，保证重编号后仍是原来的凭证字。

    计数走的是**归一后**的前缀。这样库里混着「记-0001」和「记0002」时，
    两边都算作「记」，重编号的结果是清一色的「记000X」—— 整理顺带把
    历史遗留的分隔符形态收敛掉，而不是让它继续繁殖（记- 票数占优时，
    原来这里会把「记-」当成凭证字原样保留下来）。
    """
    counts: dict[str, int] = {}
    for v in rows:
        canon = normalize_no(v.voucher_no)
        p, _ = split_no(canon) if canon else split_no(v.voucher_no)
        counts[p] = counts.get(p, 0) + 1
    if not counts:
        return "记"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0] == "记"))[0]


def irreparable_gaps(db: Session, book_id: int, period: Period,
                     deleting: set[int]) -> list[str]:
    """删掉 ``deleting``（凭证 id 集合）之后，这个期间会留下哪些
    **「整理凭证」补不上**的断号（审计 P1-3）。

    **为什么会有「补不上」的断号。** 整理只给**未记账**的凭证重新发号
    （已记账的号原样留着，见 :func:`plan_organize`），它把未记账的那些
    压紧成 1..N（跳过已记账占着的号）。于是：

      · 断号的序号 <= N 的 → 整理能填掉（空号正好被补上）；
      · 断号的序号 >  N 的 → **整理填不掉**，而且整理自己还会改 0 张。

    后者就是界面的死路：期间不能记账也不能结账（``assert_tidy`` 拦着），
    而用户按报错去点「整理凭证」，点完还是这个错。典型状态是
    「记0001（未记账） + 记0003（已记账）」—— 中间的 记0002 被删了。

    算 N 时要算准：整理发号时会**跳过**已记账占着的号，所以最终发到的
    最大序号 M = N + |已记账占着的、落在 1..N 里的号|。用 M 判「填不填得上」
    才准 —— 用 N 判会把「其实填得上」误判成死路，把人拦在门外。

    返回断号的凭证号（空列表 = 删掉之后号段仍然修得回来）。
    """
    rows = list(db.scalars(
        select(Voucher).where(
            Voucher.period_id == period.id, Voucher.book_id == book_id)
    ).all())
    keep = [v for v in rows if v.id not in deleting]
    if not keep:
        return []
    # 未记账的那些 = 整理能重新发号的（movable）
    movable = [v for v in keep if v.status != "POSTED"]
    # 已记账占着的号 = 整理要跳过的（reserved）
    reserved = {_seq_of(v.voucher_no) for v in keep if v.status == "POSTED"}
    # 整理发号后用到的最大序号：第 N 个「不在 reserved 里」的正整数
    assigned = 0
    top_assigned = 0
    while assigned < len(movable):
        top_assigned += 1
        if top_assigned not in reserved:
            assigned += 1
    # 断号用**与体检同一份**算法（``_analyze_rows``）算 —— 用户看到的
    # 「断在 记0002」和体检报出来的「断号 1 个：记0002」必须是同一个。
    analysis = _analyze_rows([
        _VoucherRow(v.voucher_no, v.voucher_date, v.status) for v in keep])
    return [g for g in analysis["gaps"] if _seq_of(g) > top_assigned]


def assert_tidy(db: Session, periods: list[Period]) -> None:
    """记账 / 结账前的闸门：这个月还没整理干净，就别让它变成不可翻的历史。

    断号、时序不对的月份一旦记了账、封了账，纸质凭证就再也对不上了 ——
    而那张纸已经打印出去、装订进账簿了。所以闸门放在这里，而不是等到月底才发现。

    **报错必须给出真正走得通的那条路**（审计 P1-3）。以前这里一律写
    「请先到「整理凭证」把号排好再来」，而有一类状态**整理凭证对它无能为力**：

        记0001（未记账） 记0003（已记账）  ← 记0002 被删了，号段留洞

    整理只给**未记账**的凭证重新发号（已记账的号原样保留，见 ``plan_organize``），
    于是 0001 仍然是 0001、0003 仍然是 0003 —— 洞还在原地，改 0 张。
    用户按报错去点「整理凭证」，点完还是这个错 —— 这就是死路。
    （界面上的「记账 / 反记账」按钮已撤，唯一能退回已记账凭证的入口是
    「期末结账 → 反结账」。）

    所以这里区分两种情况，各给各的出路：
      * 断号**能被整理补上**（空号在未记账凭证能压紧到的范围内）→ 让用户去整理；
      * 断号**整理补不上**（后面有已记账的凭证占着号）→ 说清两条真出路：
        手工补一张用这个号的凭证，或先反结账把已记账的退回未记账再整理。
    """
    bad: list[str] = []
    advice: list[str] = []
    for p in periods:
        if p.locked:
            continue
        rep = check_period(db, p)
        if not rep["blocking"]:
            continue
        bad.append(f"{rep['period']}　" + "；".join(rep["blocking"]))
        gaps = rep["gaps"]
        if not gaps:
            continue
        # 整理把未记账的凭证压紧成 1..N（跳过已记账占着的号），
        # 所以断号 g 能被补上的充要条件是 g <= N（N = 未记账凭证数）。
        unposted = rep["total"] - len(rep["posted"])
        stuck = [g for g in gaps if _seq_of(g) > unposted]
        if stuck:
            advice.append(
                f"· {rep['period']} 的断号（{'、'.join(stuck)}）**「整理凭证」补不上**："
                f"它后面有已记账的凭证占着号（整理只给未记账的凭证重新发号）。"
                f"两条出路：① 手工补一张用「{'、'.join(stuck)}」这个号的凭证"
                f"（导入也可以），把号段接上；"
                f"② 到「期末结账」对 {rep['period']} 执行「反结账」，"
                f"把已记账的凭证退回未记账，再点「整理凭证」。"
            )
        else:
            advice.append(
                f"· {rep['period']} 的断号（{'、'.join(gaps)}）："
                f"点一次「整理凭证」就能补上（它会把未记账的凭证重新发号，空号正好被填掉）。"
            )
    if bad:
        raise ValueError(
            "凭证还没整理干净，不能记账、也不能结账：\n"
            + "\n".join(bad)
            + "\n怎么补：\n"
            + "\n".join(advice)
        )


# --------------------------------------------------------------------- 执行


def apply_organize(
    db: Session,
    *,
    book_id: int,
    user: SysUser,
    period_id: int | None = None,
    strategy: str = "SORT_DATE",
) -> dict:
    """落库整理。返回每期的实际改动张数。"""
    # P2-1：**先锁，再算**。
    #
    # 原来是「先 plan_organize 算方案、再取行锁」，而 plan_organize 自己就是
    # 「读一遍算一遍」。于是锁拿到的时候方案早算完了 —— 它挡住的是写入，
    # 挡不住**读**。两个并发整理仍然会各自按自己的快照算出同一套目标号，
    # 后提交的那个照旧撞 ``uq_voucher_period_no``。
    #
    # DB 侧有唯一约束兜底，所以从来不会产生脏数据；撞出来的是一条裸的
    # IntegrityError，走全局处理器只得到 500，用户看不出是自己刚点过整理。
    # 也就是说「并发防护」只在代码注释里成立过。
    #
    # 锁的**候选集合只由 period_id 决定**（指定一期 → 那期；不指定 → 本账套
    # 全部期间），不需要看凭证就能确定，所以完全可以在算方案之前拿到。
    #
    # 锁粒度选期间而不是账套：整理是逐期发号的，锁期间就够把「同一个期间的
    # 号段」串行化，不同期间仍可并行。锁顺序按 ``ord`` 升序 —— 全项目行锁
    # 只有这两处，顺序一致才不会成环。
    #
    # 代价：不指定期间时，锁的是本账套**全部**期间，而不是「算下来有改动的
    # 那些」。那是没办法的 —— 「有改动」这件事正是要读凭证才知道的，而读
    # 必须在锁之后。整理是低频操作，多锁几期换来的是真的串行化，这笔买卖
    # 划算；而且那一档的锁仍然在同一个事务里，几毫秒就放开了。
    lock_stmt = select(Period.id).where(Period.book_id == book_id).order_by(Period.ord)
    if period_id is not None:
        lock_stmt = lock_stmt.where(Period.id == period_id)
    db.execute(lock_stmt.with_for_update()).all()   # 取结果：锁才真正落在这些行上

    plans = plan_organize(db, book_id=book_id, period_id=period_id, strategy=strategy)

    # 结账检查放在锁内：这样「读到未结账」与「发号」之间不再有窗口。
    locked = db.execute(
        select(Period.year, Period.month).where(
            Period.id.in_([p.period_id for p in plans]), Period.locked.is_(True)
        )
    ).all()
    if locked:
        raise ValueError(
            "会计期间 " + ", ".join(period_label(y, m) for y, m in locked) + " 已结账，不能整理凭证"
        )

    result: list[dict] = []
    total_changed = 0

    for plan in plans:
        before = {
            c.voucher_id: c.old_no
            for c in plan.changes
            if c.changed
        }
        if before:
            _renumber(db, plan)
            total_changed += len(before)
        result.append(plan.to_dict())

    db.flush()
    log(
        db,
        action="ORGANIZE",
        entity="voucher",
        entity_id=str(period_id or "ALL"),
        user=user,
        book_id=book_id,
        detail=f"整理凭证（{strategy}）：改号 {total_changed} 张",
        before={"old": {str(k): v for k, v in before.items()}} if before else None,
        after={"count": total_changed},
    )
    db.commit()

    return {"strategy": strategy, "total_changed": total_changed, "periods": result}


def _renumber(db: Session, plan: PeriodPlan) -> None:
    """两阶段改号，避开即时唯一约束。"""
    targets = [c for c in plan.changes if c.changed]
    if not targets:
        return

    # 阶段一：整体挪到 ∥<id>，这些值与任何真实凭证号都不冲突
    # 阶段二：写最终号，此时序列里已经没有旧号
    #
    # 两段都包在 savepoint 里（P3-11）：IntegrityError 会把整个事务标记为失败，
    # 而 ``apply_organize`` 逐期改号 —— 一个期间撞了不该把前面几期已经改好的
    # 号一起废掉。撞号（唯一约束兜底生效）翻成 ValueError；不是撞号的
    # 数据库错误原样抛出，不替它编理由。
    try:
        with db.begin_nested():
            for c in targets:
                v = db.get(Voucher, c.voucher_id)
                if v is None:
                    raise ValueError(f"凭证不存在（可能已被删除）：{c.voucher_id}")
                v.voucher_no = f"{PARK_PREFIX}{v.id}"
            db.flush()

            for c in targets:
                v = db.get(Voucher, c.voucher_id)
                v.voucher_no = c.new_no
            db.flush()
    except IntegrityError as exc:
        # savepoint 回滚了，数据库里的号没动，但会话里的对象还带着改过的值 ——
        # expire 掉，否则调用方接着读到的就是一份「内存里已经改了、库里没改」的
        # 凭证号，日志与返回值会互相打架。
        db.expire_all()
        if constraint_name(exc) != _NO_UNIQUE and _NO_UNIQUE not in str(
                getattr(exc, "orig", exc)):
            raise
        raise ValueError(
            f"改号失败：{plan.label} 的目标凭证号已被另一条记录占用"
            f"（可能刚被别的操作改过）。请刷新后重试。") from exc
