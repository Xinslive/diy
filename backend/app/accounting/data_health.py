"""存量数据体检的判据：上线前在**真实库**上查一遍「账上的数是不是已经错了」。

**为什么要有这个模块（审计报告 §7 第 1 步第 11 项）。**

审计报告的路线图第 1 步第 11 项写着：

    先做一次存量数据体检：查 entry 是否已存在同一 voucher_id 下 line_no
    重复的行（§3.15 场景 B 已经能落库）。有重复则先人工清理 ——
    这一步是第 12 项的前置。

这件事**替得掉一半**：在真实库上跑一条只读查询，代码替得了；
但「哪一条重复该留、金额该怎么修正」替不了 —— 那是懂业务的人才能做的决定，
所以**清理数据是前置条件，不是「迁移顺手替人清理」**。

这个模块负责的是替得掉的那一半：**判据**。查什么、怎么算重复、
查出来怎么跟人说清楚 —— 都在这里，别处不许再写一份。

**为什么必须只有一份（审计报告 §4.5）。**

「同一张凭证里行号重复」这个判据在这条链上要出现在三处：

  · 迁移 0038 的前置检查 —— 加唯一约束**之前**先查一遍，有重复就大声失败；
  · 上线前体检脚本（``scripts/data_health_check.py``）—— 让人在**迁移之前**
    就知道，而不是等迁移炸了才看见；
  · 用例 —— 造脏数据、验判据本身。

三处各写一份查询，三份就会分叉；而分叉出来的那两份会**安静地**给出不同答案
（有的说重复、有的说干净）。这里出错的后果不是报错，是
**账上的数被记了两遍却没有任何一处提示**：§3.15 那个场景里借贷仍然平、
试算平、科目余额平、总账平、「整理就绪」体检也过 —— 全绿，但数字是错的。

所以判据只写在这里，三处都来调它。

**它只读。** 体检跑在生产库上，这个模块里没有任何一条写操作。
"""
from __future__ import annotations

import sqlalchemy as sa


#: 重复行号是怎么来的 —— 迁移的前置报错与体检脚本的输出**共用这一份**。
#:
#: 为什么共用：同一个解释写两遍，两遍就会慢慢不一样；而这段话是要给
#: **不懂技术的人**看的（运维、业务），两版说法不一致会让人怀疑到底哪版对。
DUPLICATE_HOW_IT_HAPPENED = (
    "这些重复行是并发改同一张凭证留下的（审计报告 §3.15）："
    "两个人同时改一张凭证，后一个按自己那份陈旧的分录集合「删旧插新」，"
    "而前一个已经把那几行删掉了 —— DELETE 命中 0 行，新行照样插进去，"
    "于是同一笔钱在账上记了两遍。"
)

#: 拿到重复行之后该做什么 —— 同样**只有这一份**。
DUPLICATE_HOW_TO_FIX = (
    "**加约束之前必须先清理这些重复分录**：每张凭证每个行号只留一条；"
    "金额对不上的那一条要按原始业务修正（不是随手删一行）。"
)


def duplicate_line_no(
    bind, *, book_id: int | None = None
) -> list[tuple[int, list[int], int]]:
    """找出重复的 ``(voucher_id, line_no)``。

    返回 ``[(voucher_id, 行号列表, 份数), ...]``，按 voucher_id、行号排序。

    **全仓唯一一份实现** —— 迁移 0038 的前置检查与上线前体检脚本都调它
    （理由见模块 docstring）。

    ``entry`` 表可能很大（五年账套几万行），所以用 ``GROUP BY`` 让数据库
    自己聚合，而不是把全表拉回 Python。

    ``bind`` 是 SQLAlchemy 的 ``Connection`` 或 ``Session`` —— 两者都有
    ``.execute``：迁移前置检查传连接（``db.connection()``），体检脚本传会话。

    ``book_id`` 给了就只看那个账套，不给就全表。**默认是全表**：唯一约束
    ``uq_entry_voucher_line_no`` 是建在整张表上的，判「这张表能不能加约束」
    只能按整张表看。
    """
    where = "WHERE book_id = :book_id" if book_id is not None else ""
    params = {"book_id": book_id} if book_id is not None else {}
    rows = bind.execute(sa.text(f"""
        SELECT voucher_id,
               array_agg(DISTINCT line_no ORDER BY line_no) AS lines,
               count(*) AS n
          FROM entry
          {where}
         GROUP BY voucher_id, line_no
        HAVING count(*) > 1
         ORDER BY voucher_id, line_no
    """), params).all()
    return [(int(r[0]), list(r[1]), int(r[2])) for r in rows]


def describe_duplicate(dup: tuple[int, list[int], int]) -> str:
    """把一组重复行说成人能直接照着做的话。

    ``dup`` 是 :func:`duplicate_line_no` 的一条返回。

    口径**只有这一份** —— 迁移的前置报错与体检脚本的输出都调它，
    两边点名的方式必须一致（同一张凭证、同一个行号、几份重复）。
    """
    voucher_id, lines, n = dup
    return (f"凭证 {voucher_id}（voucher_id={voucher_id}）的行号 {lines} 各有 {n} 条")
