"""把总账 / 明细账的**内置默认基线**写进库里（2026-10-22）。

用户自己在模板设计器里调出了两份「新默认模板」（总账、明细账各一份），
要求把它们**设定成内置默认基线**。

基线是模板三层归属里最外面那一层（``book_id IS NULL AND user_id IS NULL``），
与凭证范围那两份（「凭证纸专用」「基础模板」）同一层 —— 口径见
``app/models/printing.py``。

为什么是**基线**而不是「账套模板」
--------------------------------
默认模板的取用顺序是「我的私有副本 → 本账套模板 → 全局基线」
（``api/printing.py::_template`` 的 ORDER BY）。要让它对**所有账套**都生效，
就得落在最外面那一层；留在账套层只是这一本账的事。

为什么**直接改库、不写迁移**
--------------------------
基线是**这台部署上的内容**，不是数据结构的变化。凭证那一对基线当初就是
这么落的（不加迁移）。加一条迁移等于把「某人调出来的版式」硬编进每一次
部署：换个环境、换个人就得改迁移；用户改一次版式，又与迁移对不上。

内容取自代码里的 ``DEFAULT_TEMPLATES`` —— **代码默认值与库里的基线行是
同一份内容**，两边不会各说各话（改版式只改一处，另一处由这个脚本同步）。

用法
----
    python scripts/set_ledger_baseline.py            # 只看会做什么（默认）
    python scripts/set_ledger_baseline.py --apply    # 真的执行
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.schema_version import assert_schema_ready  # noqa: E402
from app.models.printing import PrintTemplate  # noqa: E402
from app.printing.template import DEFAULT_TEMPLATES, validate_template  # noqa: E402

#: scope -> 基线行的名字。
#:
#: 名字是给人看的：模板下拉里那一句「{name}（A4）」。原来那两份叫
#: 「新默认模板」—— 那是在「我新建了一份模板」这个语境下起的名，提到
#: 全局基线上就不对了（两个范围同名，列在一起分不出谁是谁）。
BASELINE_NAMES = {
    "LEDGER": "总账用友式",
    "DETAIL_LEDGER": "明细账用友式",
}


def _baseline_of(db, scope: str) -> PrintTemplate | None:
    """这一范围**已经是基线**的那一行（两个归属都空）。"""
    return db.scalar(
        select(PrintTemplate).where(
            PrintTemplate.scope == scope,
            PrintTemplate.book_id.is_(None),
            PrintTemplate.user_id.is_(None),
        )
    )


def _book_rows_of(db, scope: str) -> list[PrintTemplate]:
    """这一范围里属于某个账套的模板（``book_id`` 有值、不是私有副本）。"""
    return list(db.scalars(
        select(PrintTemplate).where(
            PrintTemplate.scope == scope,
            PrintTemplate.user_id.is_(None),
            PrintTemplate.book_id.is_not(None),
        ).order_by(PrintTemplate.id)
    ).all())


def plan(db, scope: str) -> tuple[str, PrintTemplate | None, dict]:
    """算出这一范围该怎么落，返回（动作, 行, 新内容）。"""
    content = validate_template(scope, DEFAULT_TEMPLATES[scope])
    baseline = _baseline_of(db, scope)
    if baseline is not None:
        return ("更新基线行", baseline, content)
    rows = _book_rows_of(db, scope)
    if len(rows) == 1:
        # **只有一个账套层模板** → 那就是用户自己调出来的那一份，
        # 直接把它提为基线（保留这一行，不另起一行）。
        return ("账套模板提为基线", rows[0], content)
    return ("新建基线行", None, content)


def apply_scope(db, scope: str, apply: bool) -> None:
    name = BASELINE_NAMES[scope]
    action, row, content = plan(db, scope)
    print(f"\n[{scope}] {action}")
    if row is not None:
        print(f"  现有行 #{row.id}《{row.name}》"
              f"（book_id={row.book_id}, user_id={row.user_id}）")
    print(f"  目标：基线行（book_id=NULL, user_id=NULL, is_default=True）《{name}》")
    print(f"  内容：代码默认 DEFAULT_TEMPLATES[{scope!r}]（已归一化，"
          f"{len(content.get('columns') or [])} 列 / "
          f"方向 {content.get('orientation')}）")

    # 账套层里别的模板带着默认标记的话，**会盖过基线**（取用顺序是
    # 私有副本 → 账套模板 → 基线）。基线要当默认，这些标记就得清掉。
    others = [r for r in _book_rows_of(db, scope)
              if r.id != (row.id if row is not None else -1) and r.is_default]
    for other in others:
        print(f"  清掉账套模板 #{other.id}《{other.name}》的默认标记"
              f"（否则它会盖过基线）")
    if not apply:
        print("  （dry-run：没有写库）")
        return

    if row is None:
        row = PrintTemplate(book_id=None, user_id=None, scope=scope,
                            name=name, paper="A4", content=content,
                            is_default=True, remark="")
        db.add(row)
    else:
        row.book_id = None
        row.user_id = None
        row.name = name
        row.paper = "A4"
        row.content = content
        row.is_default = True
    for other in others:
        other.is_default = False
    db.flush()
    print(f"  已写入：基线行 #{row.id}《{row.name}》")


def main() -> int:
    apply = "--apply" in sys.argv
    print("=== 总账 / 明细账：内置默认基线 ===")
    print("模式：", "执行" if apply else "dry-run（加 --apply 才写库）")
    assert_schema_ready()
    db = SessionLocal()
    try:
        for scope in BASELINE_NAMES:
            apply_scope(db, scope, apply)
        if apply:
            db.commit()
            print("\n已提交。")
        else:
            db.rollback()
            print("\n（dry-run：已回滚，库未改动）")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
