"""科目表。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import SubjectIn
from app.core.db import get_db
from app.core.deps import get_my_book, require_book_write
from app.models import Book, Entry, PeriodOpening, Subject, UserBook

router = APIRouter(prefix="/api/books/{book_id}/subjects", tags=["科目"])


def _tree(rows: list[Subject]) -> list[dict]:
    by_parent: dict[int | None, list[Subject]] = {}
    for s in rows:
        by_parent.setdefault(s.parent_id, []).append(s)
    for lst in by_parent.values():
        lst.sort(key=lambda x: (x.sort_no, x.code))

    def build(parent):
        return [
            {
                "id": s.id, "code": s.code, "name": s.name, "level": s.level,
                "category": s.category, "direction": s.direction, "is_leaf": s.is_leaf,
                "carry_forward": s.carry_forward, "active": s.active,
                "qty_calc": s.qty_calc, "aux_calc": s.aux_calc, "fx_calc": s.fx_calc,
                "children": build(s.id) if s.id in by_parent else [],
            }
            for s in by_parent.get(parent, [])
        ]

    return build(None)


@router.get("")
def list_subjects(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                  include_inactive: bool = Query(False)):
    """include_inactive=true 时连停用科目一起返回：编辑老凭证时，
    老凭证上挂的那个科目可能已经停用，不返回的话格子里就空了。"""
    stmt = select(Subject).where(Subject.book_id == book.id)
    if not include_inactive:
        stmt = stmt.where(Subject.active.is_(True))
    rows = list(db.scalars(stmt.order_by(Subject.code)).all())
    return _tree(rows)


@router.get("/flat")
def flat_subjects(book: Book = Depends(get_my_book), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Subject).where(Subject.book_id == book.id, Subject.active.is_(True),
                               Subject.is_leaf.is_(True)).order_by(Subject.code)
    ).all()
    return [{"id": s.id, "code": s.code, "name": s.name, "direction": s.direction,
             "level": s.level, "category": s.category, "is_leaf": s.is_leaf,
             "carry_forward": s.carry_forward,
             "qty_calc": s.qty_calc, "aux_calc": s.aux_calc, "fx_calc": s.fx_calc} for s in rows]


@router.get("/frequent")
def frequent_subjects(book: Book = Depends(get_my_book), db: Session = Depends(get_db),
                      limit: int = Query(20, ge=1, le=100)):
    """最常用的末级科目：按这张账里已录入的分录数排。

    凭证录入时同一个科目一天要按几十次，摆在第一屏比让人去搜快得多。
    柠檬云这一栏叫「AI推荐」，我们没有模型，就用真实使用频次顶上 ——
    它比任何推荐算法都更贴近这个人、这套账自己的记账习惯。
    """
    rows = db.execute(
        select(Entry.subject_id, func.count().label("n"))
        .join(Subject, Subject.id == Entry.subject_id)
        .where(Entry.book_id == book.id, Subject.active.is_(True))
        .group_by(Entry.subject_id)
        .order_by(func.count().desc(), Entry.subject_id)
        .limit(limit)
    ).all()
    ids = [r.subject_id for r in rows]
    if not ids:
        return []
    subs = db.scalars(select(Subject).where(Subject.id.in_(ids))).all()
    by_id = {s.id: s for s in subs}
    out = []
    for r in rows:
        s = by_id.get(r.subject_id)
        if s is None or not s.active or not s.is_leaf:
            continue   # 只推荐能真正记账的末级科目
        out.append({"id": s.id, "code": s.code, "name": s.name, "category": s.category,
                    "direction": s.direction, "is_leaf": s.is_leaf, "used": r.n})
    return out


@router.post("", status_code=201)
def create_subject(payload: SubjectIn, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write)):
    exists = db.scalar(select(Subject).where(Subject.book_id == book.id, Subject.code == payload.code))
    if exists:
        raise HTTPException(400, f"科目编码已存在: {payload.code}")

    parent = _check_parent(db, book, payload.parent_id, self_id=None)

    s = Subject(
        book_id=book.id, code=payload.code, name=payload.name, category=payload.category,
        direction=payload.direction, parent_id=payload.parent_id,
        level=(parent.level + 1) if parent else 1,
        is_leaf=payload.is_leaf, carry_forward=payload.carry_forward,
        qty_calc=payload.qty_calc, aux_calc=payload.aux_calc, fx_calc=payload.fx_calc,
        sort_no=payload.sort_no, active=True, ext_attrs={},
    )
    db.add(s)
    # 新建子科目后，父节点就不再是叶子了。is_leaf 是「有没有下级」的冗余字段，
    # 它错了会让期初录入拒掉本该能录的科目（或者反过来允许录到汇总级上），
    # 两头都是账面错误。更新路径有 _sync_leaf_flags，新建路径原先漏了。
    _sync_leaf_flags(db, None, payload.parent_id)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "code": s.code, "name": s.name}


def _descendant_ids(db: Session, subject_id: int) -> set[int]:
    """某科目下所有后代的 id（含各级子孙）。

    用来做两件事：环路检测（不能把父节点设成自己的后代）、
    以及改父后维护 is_leaf。不一次性全查是因为科目树通常只有几百条，
    但也没必要每次请求都把整棵树捞出来。
    """
    out: set[int] = set()
    frontier = [subject_id]
    while frontier:
        rows = list(db.scalars(
            select(Subject.id).where(Subject.parent_id.in_(frontier))
        ).all())
        rows = [r for r in rows if r not in out]
        if not rows:
            break
        out.update(rows)
        frontier = rows
    return out


def _check_parent(db: Session, book: Book, parent_id: int | None, self_id: int | None) -> Subject | None:
    """校验要挂的父节点：必须属于本账套，且不能构成环。

    新建科目时有这条校验（P1-06：更新路径**没有**），于��拿着另一个账套的
    科目 id 来改父节点，HTTP 200 —— 该科目随即从本账套的科目树里消失，
    变成 UI 上不可见、库里还在、还被分录引用的孤儿节点。
    对使用者来说这是最难排查的一类问题：看不见但还在参与计算。
    """
    if not parent_id:
        return None
    if self_id is not None and parent_id == self_id:
        raise HTTPException(400, "上级科目不能是它自己")
    parent = db.get(Subject, parent_id)
    if parent is None or parent.book_id != book.id:
        raise HTTPException(400, "上级科目不存在")
    if self_id is not None and parent_id in _descendant_ids(db, self_id):
        raise HTTPException(400, "上级科目不能是它自己的下级（会形成循环）")
    return parent


def _sync_leaf_flags(db: Session, old_parent_id: int | None, new_parent_id: int | None) -> None:
    """改父之后，把新旧两个父节点的 is_leaf 重新算一遍。

    is_leaf 是「有没有下级」的冗余字段。它错了，期初录入会拒掉本该能录的科目，
    或者反过来允许在汇总级科目上录期初 —— 两头都是账面错误。
    """
    for pid in {old_parent_id, new_parent_id}:
        if not pid:
            continue
        # 会话是 autoflush=False（见 app/core/db.py），刚改的 parent_id 还没落库，
        # 下面的查询看不见它 —— ��把「有新下级」误判成 False，父节点就不更新。
        db.flush()
        has_child = db.scalar(
            select(Subject.id).where(Subject.parent_id == pid).limit(1)
        ) is not None
        parent = db.get(Subject, pid)
        if parent is not None and parent.is_leaf != (not has_child):
            parent.is_leaf = not has_child


@router.patch("/{subject_id}")
def update_subject(subject_id: int, payload: SubjectIn, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write)):
    s = db.get(Subject, subject_id)
    if s is None or s.book_id != book.id:
        raise HTTPException(404, "科目不存在")
    used = db.scalar(select(Entry).where(Entry.subject_id == subject_id).limit(1))
    if used and (s.code != payload.code or s.direction != payload.direction or s.category != payload.category):
        raise HTTPException(400, "科目已被凭证使用，不能修改编码 / 类别 / 余额方向")

    # P1-06：新建时有父科目校验，更新路径原来直接赋值。补上，
    # 连带环路检测与 level 跟随。
    if payload.parent_id != s.parent_id:
        parent = _check_parent(db, book, payload.parent_id, self_id=subject_id)
        old_parent_id = s.parent_id
        s.parent_id = payload.parent_id
        s.level = (parent.level + 1) if parent else 1
        _sync_leaf_flags(db, old_parent_id, payload.parent_id)

    for field in ("name", "category", "direction", "carry_forward", "is_leaf", "sort_no",
                  "qty_calc", "aux_calc", "fx_calc"):
        setattr(s, field, getattr(payload, field))
    # 停用是「不让再选它」，不是「删掉它」：老凭证还指着它，账还得算得出来。
    # 有分录的科目删不掉（下面 400），停用才是那条正路。
    if payload.active is not None:
        s.active = payload.active
    db.commit()
    db.refresh(s)
    return {"id": s.id, "code": s.code, "name": s.name}


@router.delete("/{subject_id}")
def delete_subject(subject_id: int, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write)):
    s = db.get(Subject, subject_id)
    if s is None or s.book_id != book.id:
        raise HTTPException(404, "科目不存在")
    if db.scalar(select(Entry).where(Entry.subject_id == subject_id).limit(1)):
        raise HTTPException(400, "科目已被凭证使用，不能删除；可改为停用")
    if db.scalar(select(Subject).where(Subject.parent_id == subject_id).limit(1)):
        raise HTTPException(400, "存在下级科目，不能删除")
    # P1-05：期初引用也要挡。PeriodOpening.subject_id 上挂着
    # ON DELETE CASCADE —— 原来只查了 Entry 和下级科目，没查期初，
    # 于是「只被期初引用、没有凭证引用」的科目一删，期初记录随之消失，
    # 余额凭空漂移（审计实测：2 条期初删成 1 条，余额 0 → -100）。
    #
    # 年度结转自动生成的期初**大量处于这个状态**（有余额、未记账），
    # 所以这不是边角情况。报错口径与「被凭证使用」保持一致：
    # 都是「可改为停用」—— 停用不影响历史统计。
    n_openings = db.scalar(
        select(func.count()).select_from(PeriodOpening).where(
            PeriodOpening.subject_id == subject_id)
    ) or 0
    if n_openings:
        raise HTTPException(
            400, f"该科目有 {n_openings} 期期初余额，不能删除；可改为停用"
                 "（停用不影响历史统计与年度结转）")
    db.delete(s)
    db.commit()
    return {"ok": True}
