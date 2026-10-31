"""账套删除。

**两条路径，都是隐形的**：

1. 管理员收回一个账套的**最后一份授权** → 这个账套一个授权用户都没有了
   → 随这次授权变更一并删掉（见 :func:`purge_unowned_books`）。
2. 账套只剩**一个**授权用户时，这个人把账套改名成「删除」→ 删掉这本账
   （见 ``api/books.py::_delete_by_rename``）。

界面上**没有**「删除账套」按钮，也没有任何文案提示第二条路径 ——
机制不外露，只在审计日志里留痕（``DELETE_BOOK``）。

----
为什么删一本账要这么讲究
------------------------

``book`` 的子表外键**几乎全是** ``ON DELETE CASCADE``，看上去「删一行 book
就够」。实际不是，有两类东西会挡住它：

**一、外键不肯级联的**（必须显式先清，否则 DELETE book 直接报错）：

===========================  ==============  ==========================
引用                          delete_rule     什么时候撞上
===========================  ==============  ==========================
``invoice_config.*_subject_id`` → subject    RESTRICT   配过发票科目
``invoice_rule.subject_id``     → subject    RESTRICT   配过发票规则
``period_opening.subject_id``   → subject    RESTRICT   录过期初数
``entry.subject_id``            → subject    NO ACTION  有分录
``voucher.period_id``           → period     NO ACTION  有凭证
``report_result.period_id``     → period     NO ACTION  跑过报表
===========================  ==============  ==========================

**二、触发器直接抛错的**（账上有数据时必踩）：

* ``fn_voucher_delete_guard`` / ``fn_entry_lock_guard``：
  会计期间**已结账**时，删凭证、删分录都报「会计期间已结账，禁止删除凭证」。
* ``fn_posted_voucher_guard``：**已记账**的凭证不许删
  （「凭证 % 已记账，不能修改或删除；如需改动请先反记账」）。

这两道是**账务护栏**，它们拦的是「有人偷偷改账」。删整本账不是改账，
但护栏看不见「整本账要没了」这件事 —— 所以这里先把锁解开、把已记账的
凭证反记账，**再**删。这三步都在同一个事务里，删不掉就整体回滚，
账面不会留下「解了锁没删掉」的半截状态。

解开的锁、反记账的凭证，都是**即将被删掉的行**上的锁与状态：
这笔账马上就消失了，谈不上「有人改了已结账的账」。

**三、子表顺序不能乱**：``data_version`` 的触发器
（``fn_bump_book_data_version``）在删子表时会 ``UPDATE book SET data_version
= data_version + 1``。先删 book 再删子表，这条 UPDATE 打在一行已经不存在的
账套上（不报错，但版本号就丢了）。所以**子表一律先删，book 最后删**。

**四、外键不肯级联的，宿主自己动手清**（2026-10-31 补）：插件表是**插件自己
建的**，它们的 ``book_id`` 外键**不一定**写了 ``ON DELETE CASCADE`` ——
写了就是 CASCADE，没写就是 PG 默认的 NO ACTION。宿主的插件契约只要求
「账套表必须有 ``book_id`` 外键指向 ``book.id``」（见
``app/plugins/migrations.py::validate_tables``），**没有要求它带级联**。

实测（2026-10-31 生产）：三个插件的建表语句都写成了
``sa.ForeignKey("book.id")``，库里落成 NO ACTION。于是 ``DELETE FROM book``
被插件表挡住，报 PostgreSQL ``23503``（外键冲突）—— 而
``app/main.py`` 对 23503 只回一句脱敏的中文
「引用了不存在的关联数据，请检查所选科目/账套是否仍然有效。」，
**一个字的线索都不给**。生产上八本账（有插件数据的那些）全都删不掉。

所以宿主**不假设**插件表会级联：删 book 之前，先问数据库
（``pg_constraint``）「谁引用了 ``book.id``、而引用规则不是 CASCADE」，
把这些表里的本账套行**按外键依赖顺序显式删掉**，再删 book。
**库里的外键拓扑是唯一判据** —— 插件叫什么名字、装没装、什么版本，
宿主一概不猜，猜错就是一本永远删不掉的账。

**五、审计要先写**：``audit_log.book_id`` 是 ``ON DELETE SET NULL`` ——
账套删掉之后，那条「账套被删了」的日志仍然留得住（book_id 变 NULL，
事实全在 detail / before 里）。但顺序不能反：先删账套再写日志，
INSERT 时会撞外键（账套已经不存在了）。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.orm import Session

from app.accounting.audit_helper import log
from app.models import (Book, Entry, InvoiceConfig, InvoiceRule, Period, PeriodOpening,
                        ReportResult, Subject, UserBook, Voucher)

#: 保留名。账套改名叫它 = 删掉这本账（见模块说明）。
DELETION_NAME = "删除"

#: 触发来源，只进审计日志，不进任何界面。
TRIGGER_REVOKE = "REVOKE_LAST_GRANT"      # 路径一：管理员收回最后一份授权
TRIGGER_RENAME = "RENAME_TO_DELETION"      # 路径二：改名成「删除」


def is_deletion_name(name: Optional[str]) -> bool:
    """这个账套名是不是「删除」。前后空白不算数。"""
    return (name or "").strip() == DELETION_NAME


def _counts(db: Session, book_id: int) -> dict:
    """这本账里各有多少行 —— 删掉之后就查不到了，删之前先记下来。"""

    def n(model) -> int:
        stmt = select(func.count()).select_from(model).where(model.book_id == book_id)
        return int(db.scalar(stmt) or 0)

    return {
        "subjects": n(Subject),
        "periods": n(Period),
        "vouchers": n(Voucher),
        "entries": n(Entry),
        "openings": n(PeriodOpening),
        "grants": n(UserBook),
    }


# ---------------------------------------------------------------------------
# 「引用 book.id、却不跟着账套一起走」的外键
# ---------------------------------------------------------------------------
#
# PG 的外键删除动作记在 ``pg_constraint.confdeltype`` 里：
#
# ======  ==========================  ===================================
# 字母    含义                        删掉被引用的行时会怎样
# ======  ==========================  ===================================
# 'c'     CASCADE                     引用行跟着一起删
# 'n'     SET NULL                   引用行的列置空
# 'a'     NO ACTION（**PG 的默认**）  还有引用行 → **报错 23503**
# 'r'     RESTRICT                    同上，且**立即**检查
# 'd'     SET DEFAULT                 引用列写回默认值
# ======  ==========================  ===================================
#
# 插件表最容易踩的就是 'a'：建表时写 ``sa.ForeignKey("book.id")`` 而**忘了
# 写 ondelete** —— Alembic 忠实地照抄，于是库里就是 NO ACTION。
# 宿主先前把插件表当成「会级联」处理（见模块说明），于是这类账套永远删不掉。

#: 会挡住 ``DELETE FROM book`` 的两种引用规则。
_FK_BLOCKING_RULES = ("a", "r")

#: 找出「引用 book.id 且不级联」的表。
#:
#: 表名与列名**全部来自 pg_catalog**，不是用户输入。
_BLOCKING_CHILDREN_SQL = text("""
    SELECT child.relname AS table_name,
           array_agg(att.attname ORDER BY u.ord) AS columns
    FROM pg_constraint con
    JOIN pg_class child ON child.oid = con.conrelid
    JOIN pg_namespace ns ON ns.oid = child.relnamespace
    JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord) ON true
    JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = u.attnum
    WHERE con.contype = 'f'
      AND con.confrelid = 'public.book'::regclass
      AND con.confdeltype IN :rules
      AND ns.nspname = 'public'
    GROUP BY child.relname, con.conname
    ORDER BY child.relname, con.conname
""").bindparams(bindparam("rules", expanding=True))

#: 这些表**之间**有没有也用 NO ACTION 的外键（决定谁得先删）。
_CHILD_EDGES_SQL = text("""
    SELECT child.relname AS child_table,
           parent.relname AS parent_table
    FROM pg_constraint con
    JOIN pg_class child ON child.oid = con.conrelid
    JOIN pg_class parent ON parent.oid = con.confrelid
    WHERE con.contype = 'f'
      AND con.confdeltype IN :rules
      AND child.relname IN :tables
      AND parent.relname IN :tables
""").bindparams(bindparam("rules", expanding=True),
                  bindparam("tables", expanding=True))


def _blocking_children(db: Session) -> list[tuple[str, list[str]]]:
    """引用 ``book.id``、而引用规则**不是** CASCADE / SET NULL 的表。

    返回 ``[(表名, [列名...]), ...]``。一张表可能有多条这样的外键
    （比如同时按 ``book_id`` 和别的列引用），一张表只出现一次。

    **为什么用 pg_catalog 而不是写死一张表清单**：插件表是插件自己建的，
    宿主不可能知道它们叫什么、装没装、什么版本 —— 写死清单的那一刻，
    清单就已经落后了。库里的外键拓扑是**唯一**能说准「谁会挡住删除」
    的地方。
    """
    rows = db.execute(_BLOCKING_CHILDREN_SQL, {"rules": _FK_BLOCKING_RULES}).mappings().all()
    merged: dict[str, list[str]] = {}
    for row in rows:
        cols = list(row["columns"] or [])
        if not cols:
            continue
        # 一条外键指向 book(id)，目标列只有一个，所以子列也只能有一个。
        # 真出现多列（不可能，但万一库被手工改过）就取第一列并留个底。
        merged.setdefault(row["table_name"], cols)
    return sorted(merged.items())


def _delete_order(names: list[str], edges: list[tuple[str, str]]) -> list[str]:
    """这些表**之间**的删除顺序：先删子表，再删父表。

    ``edges`` 是 ``(子表, 父表)``：子表有行引用着父表的行，所以父表不能在
    子表还有行的时候被清空。带 CASCADE 的父子关系**不在** edges 里 ——
    那种由 PG 自己处理，顺序无所谓。

    有环就说明这套表结构本身互相卡着（NO ACTION 的环），此时无解，
    只能明确报错而不是删一半。
    """
    prereq: dict[str, set[str]] = {n: set() for n in names}
    for child, parent in edges:
        if child != parent and child in prereq and parent in prereq:
            prereq[parent].add(child)
    order: list[str] = []
    done: set[str] = set()
    while True:
        ready = [n for n in sorted(prereq) if n not in done and not prereq[n]]
        if not ready:
            break
        pick = ready[0]
        done.add(pick)
        order.append(pick)
        for other in prereq:
            prereq[other].discard(pick)
    if len(order) != len(names):
        stuck = sorted(set(names) - set(order))
        raise RuntimeError(f"账套子表之间存在循环外键，无法确定删除顺序：{stuck}")
    return order


def _quote(db: Session, identifier: str) -> str:
    """把标识符按 PG 的规则加引号。"""
    return db.get_bind().dialect.identifier_preparer.quote(identifier)


def _child_row_counts(db: Session, book_id: int,
                      children: list[tuple[str, list[str]]]) -> dict[str, int]:
    """这些表里、属于本账套的各有多少行 —— 删掉就查不到了，删之前先记下。"""
    counts: dict[str, int] = {}
    for name, cols in children:
        quoted_table = _quote(db, name)
        where = " AND ".join(f"{_quote(db, col)} = :book_id" for col in cols)
        row = db.execute(
            text(f"SELECT count(*) FROM {quoted_table} WHERE {where}"),
            {"book_id": book_id},
        ).scalar()
        counts[name] = int(row or 0)
    return counts


def _clear_blocking_children(db: Session, book_id: int,
                             children: list[tuple[str, list[str]]]) -> dict[str, int]:
    """把「引用 book.id 却不级联」的表里、属于本账套的行删掉。

    返回 ``{表名: 删掉的行数}``。

    **为什么宿主可以直接删插件表的数据**：插件契约里，
    ``scope="book"`` 的表**按定义**就跟着账套生死
    （``app/plugins/manifest.py``：账套表必须有 ``book_id`` 指向
    ``book.id``；``global`` 表则**不许**含账套数据）。
    宿主删账套，插件的账套数据就该一起走 —— 否则这本账就永远删不掉。
    """
    if not children:
        return {}
    names = [name for name, _ in children]
    edges = [
        (row["child_table"], row["parent_table"])
        for row in db.execute(_CHILD_EDGES_SQL,
                              {"rules": _FK_BLOCKING_RULES, "tables": names}).mappings()
    ]
    columns = dict(children)
    removed: dict[str, int] = {}
    for name in _delete_order(names, edges):
        cols = columns[name]
        quoted_table = _quote(db, name)
        where = " AND ".join(f"{_quote(db, col)} = :book_id" for col in cols)
        result = db.execute(
            text(f"DELETE FROM {quoted_table} WHERE {where}"), {"book_id": book_id}
        )
        removed[name] = int(result.rowcount or 0)
    return removed


def delete_book(db: Session, book: Book, actor, *, trigger: str) -> dict:
    """物理删掉一本账，连同它全部业务数据。**调用方负责 commit。**

    返回的是**纯字典**，不是 ORM 对象：账套行已经没了，
    之后再读 ``book`` 的任何属性都会变成 ``ObjectDeletedError``。
    """
    book_id = book.id
    company_name = book.company_name
    name = book.name
    # 先探明「谁会挡住删除」—— 审计日志在后面才写，
    # 所以这些行数必须**现在**数好，才能进快照。
    blocking = _blocking_children(db)
    snapshot = {
        "id": book_id,
        "company_name": company_name,
        "name": name,
        "start_year": book.start_year,
        "start_month": book.start_month,
        **_counts(db, book_id),
    }
    if blocking:
        snapshot["children"] = _child_row_counts(db, book_id, blocking)

    # 审计**先写、先 flush**：audit_log.book_id 是 SET NULL，
    # 账套删掉之后这条日志仍留得住（book_id 变 NULL，事实全在 before 里）。
    # 反过来（先删账套再写日志）会在 INSERT 时撞外键。
    log(db, action="DELETE_BOOK", entity="book", entity_id=book_id, user=actor,
        book_id=book_id,
        detail=f"删除账套 {company_name} · {name}",
        before=snapshot)
    db.flush()

    # ---- 1. 拆掉会直接抛错的三道护栏 ---------------------------------------
    # 结账锁：period.locked=true 时，删凭证 / 删分录都被
    # fn_voucher_delete_guard / fn_entry_lock_guard 挡下。
    # 整本账都要没了，先把锁解开 —— 解的是**即将被删掉**的那些期间。
    db.execute(update(Period).where(Period.book_id == book_id).values(locked=False))
    # 已记账：POSTED 凭证不许删（fn_posted_voucher_guard）。
    # 「已记账 → 未记账」是库里允许的合法跳转，所以这里走的是正路。
    db.execute(
        update(Voucher)
        .where(Voucher.book_id == book_id, Voucher.status == "POSTED")
        .values(status="UNPOSTED")
    )

    # ---- 2. 按外键顺序显式清子表 -------------------------------------------
    # 先清指向 subject 的 RESTRICT，再清指向 period 的 NO ACTION，
    # 最后清 entry（它自己指向 subject，NO ACTION）。
    db.execute(delete(InvoiceConfig).where(InvoiceConfig.book_id == book_id))
    db.execute(delete(InvoiceRule).where(InvoiceRule.book_id == book_id))
    db.execute(delete(PeriodOpening).where(PeriodOpening.book_id == book_id))
    db.execute(delete(Entry).where(Entry.book_id == book_id))
    db.execute(delete(Voucher).where(Voucher.book_id == book_id))
    db.execute(delete(ReportResult).where(ReportResult.book_id == book_id))

    # ---- 3. 清掉「引用 book.id 却不级联」的行 --------------------------------
    # 插件表就是典型（见模块说明第四节）。不先清，DELETE book 直接撞 23503。
    _clear_blocking_children(db, book_id, blocking)

    # ---- 4. 删账套本身 ------------------------------------------------------
    # 其余子表（subject / period / book_year / sys_user_book / print_template /
    # report_template / invoice_batch / invoice_record / import_batch）
    # 由 CASCADE 带走；audit_log 是 SET NULL，日志留下。
    db.execute(
        delete(Book).where(Book.id == book_id).execution_options(synchronize_session=False)
    )
    # 把这个对象从会话里摘出去：它已经是库里的空气，
    # 留在会话里只会在 commit 时触发一次「刷新一个不存在的行」。
    db.expunge(book)
    return {"id": book_id, "company_name": company_name, "name": name,
            "trigger": trigger, "before": snapshot}


def purge_unowned_books(
    db: Session,
    actor,
    book_ids: Iterable[int],
    *,
    trigger: str = TRIGGER_REVOKE,
) -> list[dict]:
    """这些账套里，凡是现在**一个授权用户都没有**的，删掉。

    「没有授权用户」= ``sys_user_book`` 里这个账套一行都没有 ——
    没人能打开它，也没人能删它，只能烂在库里。

    **调用方负责 commit**（与授权变更在同一个事务里：
    授权改了一半、账套删不掉时，整体回滚）。

    返回被删掉的账套（纯字典列表）。
    """
    out: list[dict] = []
    for book_id in dict.fromkeys(int(b) for b in book_ids):
        # 锁住账套行：并发授权要往 sys_user_book 插一行，那条插入得先对账套行
        # 拿 KEY SHARE 锁 —— 锁住之后别人插不进来，
        # 「刚查完没人授权」到「删掉」之间不会冒出半个授权。
        exists = db.execute(
            select(Book.id).where(Book.id == book_id).with_for_update()
        ).scalar()
        if exists is None:
            continue          # 账套已经没了
        if db.scalar(
            select(func.count()).select_from(UserBook).where(UserBook.book_id == book_id)
        ):
            continue          # 还有人有权，不动
        book = db.get(Book, book_id)
        if book is None:
            continue
        out.append(delete_book(db, book, actor, trigger=trigger))
    return out
