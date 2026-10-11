"""科目表。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import SubjectIn
from app.core.db import get_db
from app.core.deps import get_my_book, require_book_write
from app.models import Book, Entry, Subject, UserBook

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

    parent = db.get(Subject, payload.parent_id) if payload.parent_id else None
    if payload.parent_id and (parent is None or parent.book_id != book.id):
        raise HTTPException(400, "上级科目不存在")

    s = Subject(
        book_id=book.id, code=payload.code, name=payload.name, category=payload.category,
        direction=payload.direction, parent_id=payload.parent_id,
        level=(parent.level + 1) if parent else 1,
        is_leaf=payload.is_leaf, carry_forward=payload.carry_forward,
        qty_calc=payload.qty_calc, aux_calc=payload.aux_calc, fx_calc=payload.fx_calc,
        sort_no=payload.sort_no, active=True, ext_attrs={},
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"id": s.id, "code": s.code, "name": s.name}


@router.patch("/{subject_id}")
def update_subject(subject_id: int, payload: SubjectIn, book: Book = Depends(get_my_book),
                   db: Session = Depends(get_db), _link: UserBook = Depends(require_book_write)):
    s = db.get(Subject, subject_id)
    if s is None or s.book_id != book.id:
        raise HTTPException(404, "科目不存在")
    used = db.scalar(select(Entry).where(Entry.subject_id == subject_id).limit(1))
    if used and (s.code != payload.code or s.direction != payload.direction or s.category != payload.category):
        raise HTTPException(400, "科目已被凭证使用，不能修改编码 / 类别 / 余额方向")
    for field in ("name", "category", "direction", "carry_forward", "is_leaf", "sort_no",
                  "parent_id", "qty_calc", "aux_calc", "fx_calc"):
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
    db.delete(s)
    db.commit()
    return {"ok": True}
