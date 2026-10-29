"""把凭证范围的「系统基线」换成指定的那一份（2026-10-29）。

用户 2026-10-29 批注：把「合计大写」写进分录表格的合计行之后，要求**用自己改过
的那份替换掉原来的系统基线**。原文是「我要用 tpl=8 作为新的基线」，随后澄清
「是用现在的 tpl=8 替换掉 tpl=2」—— 顶掉的是**同名的旧基线**，
「凭证纸专用」（tpl=1）保持原样，它仍是那一档的默认打印版式。

基线是模板三层归属里最外面那一层（``book_id IS NULL AND user_id IS NULL``），
与总账 / 明细账那两份同一层 —— 口径见 ``app/models/printing.py``。

为什么是**基线**而不是「账套模板」
--------------------------------
默认模板的取用顺序是「我的私有副本 → 本账套模板 → 全局基线」
（``api/printing.py::_template`` 的 ORDER BY）。要让它对**所有账套**都生效，
就得落在最外面那一层；留在账套层只是这一本账的事。

为什么**直接改库、不写迁移**
--------------------------
基线是**这台部署上的内容**，不是数据结构的变化。凭证那一对基线当初就是
这么落的（不加迁移），总账 / 明细账那两份也一样
（见 ``set_ledger_baseline.py``）。加一条迁移等于把「某人调出来的版式」硬编进
每一次部署：换个环境、换个人就得改迁移；用户改一次版式，又与迁移对不上。

**内容一个字节都不动**：被提升的那一行只改归属标记（``user_id`` /
``forked_from_id`` 归位），版式由它自己带着走。顶替掉的旧基线整行删掉 ——
留着就会在模板下拉里变成两份同名，用户得先猜哪一份在用（``api/printing.py``
里那个「我 fork 过的那份基线不摆出来」的机制这时也救不了：副本已经不是私有副本了）。

用法
----
    python scripts/set_voucher_baseline.py --id 8            # 看看会做什么（默认）
    python scripts/set_voucher_baseline.py --id 8 --apply    # 真的执行

不写 ``--id`` 时，先在 dry-run 里看看它认出了哪一行，再决定要不要执行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.accounting.audit_helper import log  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.schema_version import assert_schema_ready  # noqa: E402
from app.models.printing import PrintTemplate  # noqa: E402
from app.models.system import SysUser  # noqa: E402

SCOPE = "VOUCHER"


def _is_baseline(t: PrintTemplate) -> bool:
    return t.book_id is None and t.user_id is None


def _same_named_baselines(db, name: str, keep_id: int | None) -> list[PrintTemplate]:
    """同名的**其它**基线行 —— 它们就是被顶替掉的那一份。"""
    rows = db.scalars(
        select(PrintTemplate).where(
            PrintTemplate.scope == SCOPE,
            PrintTemplate.book_id.is_(None),
            PrintTemplate.user_id.is_(None),
            PrintTemplate.name == name,
        )
    ).all()
    return [t for t in rows if keep_id is None or t.id != keep_id]


def plan(db, template_id: int | None) -> tuple[PrintTemplate | None, list[PrintTemplate]]:
    """算出该怎么落，返回（要提升的那一行, 被顶替掉的那些行）。"""
    if template_id is None:
        # 没给 id：取这一范围里**最近动过**的那一行（用户刚调过的那份）。
        row = db.scalars(
            select(PrintTemplate).where(PrintTemplate.scope == SCOPE)
            .order_by(PrintTemplate.updated_at.desc(), PrintTemplate.id.desc())
            .limit(1)
        ).first()
    else:
        row = db.get(PrintTemplate, template_id)
    if row is None:
        return None, []
    return row, _same_named_baselines(db, row.name, keep_id=row.id)


def main() -> int:
    apply = "--apply" in sys.argv
    template_id = None
    for arg in sys.argv[1:]:
        if arg.startswith("--id"):
            template_id = int(arg.split("=", 1)[1]) if "=" in arg else None
    if template_id is None and "--id" in sys.argv:
        idx = sys.argv.index("--id")
        if idx + 1 < len(sys.argv):
            template_id = int(sys.argv[idx + 1])

    print("=== 凭证范围：系统基线 ===")
    print("模式：", "执行" if apply else "dry-run（加 --apply 才写库）")
    assert_schema_ready()

    db = SessionLocal()
    try:
        row, replaced = plan(db, template_id)
        if row is None:
            print(f"  找不到模板 id={template_id}，什么都没做。")
            db.rollback()
            return 1

        print(f"  提升行 #{row.id}《{row.name}》"
              f"（book_id={row.book_id}, user_id={row.user_id}, "
              f"forked_from={row.forked_from_id}）")
        if _is_baseline(row) and not replaced:
            print("  它已经是系统基线，同名的旧基线也不在了 —— 无需改动（幂等）。")
            db.rollback()
            return 0
        for old in replaced:
            print(f"  顶替掉旧基线 #{old.id}《{old.name}》"
                  f"（整行删除：留着会变成两份同名）")
            print(f"    它的内容留个底（JSON）：{json.dumps(old.content, ensure_ascii=False)[:200]}…")
        print("  目标：基线行（book_id=NULL, user_id=NULL）《%s》" % row.name)

        if not apply:
            print("  （dry-run：没有写库）")
            db.rollback()
            print("\n（dry-run：已回滚，库未改动）")
            return 0

        before = {"user_id": row.user_id, "forked_from_id": row.forked_from_id,
                  "is_default": row.is_default}
        actor = None
        if row.user_id is not None:
            actor = db.get(SysUser, row.user_id)
        if actor is None:
            actor = db.scalars(select(SysUser).order_by(SysUser.id)).first()

        row.book_id = None
        row.user_id = None
        row.forked_from_id = None
        db.flush()
        log(db, action="UPDATE_PRINT_TEMPLATE", entity="print_template", entity_id=row.id,
            user=actor, detail="提升为系统基线（顶替原同名基线），模板内容不变",
            before=before, after={"user_id": None, "forked_from_id": None,
                                  "is_default": row.is_default})

        for old in replaced:
            db.delete(old)
            db.flush()
            log(db, action="DELETE_PRINT_TEMPLATE", entity="print_template",
                entity_id=old.id, user=actor,
                detail=f"删除打印模板 {old.name}（已由 #{row.id} 顶替）")

        db.commit()
        print(f"  已写入：基线行 #{row.id}《{row.name}》")
        print("\n已提交。")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
