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

两种模式，改的是**两件不同的事**
--------------------------------
* ``--id N``（默认）：**换人** —— 把第 N 行提为系统基线、顶掉同名的旧基线。
  **内容一个字节都不动**：被提升的那一行只改归属标记（``user_id`` /
  ``forked_from_id`` 归位），版式由它自己带着走。
* ``--sync-content``：**同步内容** —— 把**仓库里那份出厂 content**（迁移 0022
  内嵌的那一段 JSON）写进库里的「基础模板」基线行，归属不动。

为什么需要第二个模式：出厂 content 住在迁移里（迁移必须自包含，不能 import
应用代码），但**已经在跑的库不会被再种一次** —— 迁移的 upgrade 按
(scope, name, book_id IS NULL) 判存在，存量库里已有「基础模板」就整条跳过。
于是只改迁移的话，**新库是新参数、老库是旧参数**，两套环境各说各话。
用户 2026-10-30 批注：「把本地开发库的『基础摸板』最新的参数，更新进仓库，
作为基线，替换现在的 tpl=2 的旧参数」—— 仓库改完之后，**本地库与生产库
用同一个脚本拉齐**，三处是同一份。

用法
----
    python scripts/set_voucher_baseline.py --id 8            # 换人：看看会做什么（默认）
    python scripts/set_voucher_baseline.py --id 8 --apply    # 换人：真的执行
    python scripts/set_voucher_baseline.py --sync-content     # 同步内容：看看会做什么
    python scripts/set_voucher_baseline.py --sync-content --apply   # 同步内容：真的执行

不写 ``--id`` 时，先在 dry-run 里看看它认出了哪一行，再决定要不要执行。
两种模式都是**幂等、默认 dry-run、``--apply`` 才写库、写审计日志**。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.accounting.audit_helper import log  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.core.schema_version import assert_schema_ready  # noqa: E402
from app.models.printing import PrintTemplate  # noqa: E402
from app.models.system import SysUser  # noqa: E402
from app.printing.template import validate_template  # noqa: E402

SCOPE = "VOUCHER"

#: 这条范围里，**仓库持有出厂内容**的那条基线。
#:
#: 「凭证纸专用」不在这里：它是这一档的默认打印版式，用户 2026-10-29 明确说过
#: 它不参与基线换人，它的内容也从来没跟着改过。
BASELINE_NAME = "基础模板"

#: 出厂内容住在**迁移 0022** 里 —— 迁移必须自包含（不能 import 应用代码，
#: 否则将来应用代码一改，老迁移就跑不动了），所以「仓库里的基线参数」这份内容
#: 的家就是那条迁移。脚本从那儿读，于是**新建的库（迁移种进去的）与已经在跑的
#: 库（本脚本同步的）读的是同一份**：改版式只改那一处，两边不会各说各话。
MIGRATION_PATH = (Path(__file__).resolve().parents[1] / "migrations" / "versions"
                  / "0022_voucher_baseline_templates.py")


def factory_content(name: str = BASELINE_NAME) -> dict:
    """仓库里持有的那份出厂 content（迁移 0022 里内嵌的那一段 JSON）。

    **只读那一个常量**，不执行迁移的任何函数 —— 它跑在 alembic 的上下文里，
    这里没有 ``op`` 可用，也不需要。
    """
    spec = importlib.util.spec_from_file_location("_cwq_baseline_0022", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"读不到迁移文件：{MIGRATION_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for entry in mod.BASELINE:
        if entry["name"] == name:
            return entry["content"]
    raise SystemExit(f"迁移 0022 里没有基线《{name}》")


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


def backup_dir() -> Path:
    """基线改版式之前，备份落在哪。

    **跟着这台机器的既有惯例走**，别新造一个地方：

    * 仓库里（本地开发）：``<仓库>/.run/`` —— 2026-10-29 那次基线换人的备份
      就在那儿（``.run/print_template_backup_*.json``）；
    * 生产机上：``/root/cwq-baseline-backups/`` —— 那次换人的备份也在那儿。

    认不出来就退回生产那套（服务器上没有 ``.run``，仓库里有）。可以用环境变量
    ``CWQ_BASELINE_BACKUP_DIR`` 显式指定。
    """
    override = os.environ.get("CWQ_BASELINE_BACKUP_DIR")
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / ".run").is_dir():
        return repo_root / ".run"
    return Path("/root/cwq-baseline-backups")


def write_backup(db) -> Path:
    """把这一范围的全部模板导出一份 JSON —— 改坏了照着还原。"""
    rows = db.scalars(
        select(PrintTemplate).where(PrintTemplate.scope == SCOPE)
        .order_by(PrintTemplate.id)
    ).all()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / f"print_template_{stamp}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([
        {
            "id": t.id, "name": t.name, "scope": t.scope, "book_id": t.book_id,
            "user_id": t.user_id, "forked_from_id": t.forked_from_id,
            "is_default": t.is_default, "remark": t.remark, "content": t.content,
        }
        for t in rows
    ], ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _flat(d, prefix: str = "") -> dict:
    """把模板 content 摊平成「路径 → 值」，便于逐项比对。

    **列表按项展开**（``elements[capital].text``），不整段当成一个值 ——
    整段比的话，``elements`` 这一条路径会打印出六千字符的 JSON，
    人眼在里面找那一处改动是找不到的。
    项的标识取 ``id`` / ``key``（模板里的元素与列都有），没有就退回下标。
    """
    out: dict = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(_flat(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(d, list):
        for i, item in enumerate(d):
            if isinstance(item, dict):
                ident = item.get("id") or item.get("key")
                label = f"[{ident}]" if ident else f"[{i}]"
                out.update(_flat(item, f"{prefix}{label}"))
            else:
                out[f"{prefix}[{i}]"] = item
    else:
        out[prefix] = d
    return out


def _diff(old: dict | None, new: dict | None) -> list[tuple[str, object, object]]:
    """两份 content 的逐项差异（路径, 旧值, 新值）。"""
    fo, fn = _flat(old or {}), _flat(new or {})
    rows = []
    for k in sorted(set(fo) | set(fn)):
        a, b = fo.get(k, "（缺）"), fn.get(k, "（缺）")
        if a != b:
            rows.append((k, a, b))
    return rows


def _baseline_row(db, name: str) -> PrintTemplate | None:
    """这一范围里**已经是基线**的那一行（两个归属都空）。"""
    return db.scalar(
        select(PrintTemplate).where(
            PrintTemplate.scope == SCOPE,
            PrintTemplate.book_id.is_(None),
            PrintTemplate.user_id.is_(None),
            PrintTemplate.name == name,
        )
    )


def sync_content(db, apply: bool) -> int:
    """把**仓库里那份出厂 content**同步进库里的「基础模板」基线行。

    与 ``main()``（换人：谁当基线）是两件事：

    * ``main()`` 改的是**归属标记**（哪一行是系统基线），内容一个字节不动；
    * 这里改的是**内容**（基线行长什么样），归属不动。

    为什么要它：出厂 content 住在迁移 0022 里（迁移必须自包含），但**已经在跑
    的库**不会被再种一次 —— 迁移的 upgrade 按 (scope, name, book_id IS NULL)
    判存在，存量库里已有「基础模板」就整条跳过。于是只改迁移的话，**新库是新
    参数、老库是旧参数**，两套环境各说各话。这个脚本把老库也拉齐。

    幂等、默认 dry-run、``--apply`` 才写库、写审计日志 —— 与同目录那份
    ``set_ledger_baseline.py`` 同一套规矩。
    """
    print("=== 凭证范围：同步「基础模板」基线内容 ===")
    print("模式：", "执行" if apply else "dry-run（加 --apply 才写库）")
    print(f"内容来源：{MIGRATION_PATH}")

    content = factory_content()
    # 写之前先过一遍模板校验：仓库里这份要是自己都不合法，写进去等于把一份
    # 坏版式发给所有账套 —— 那比不同步更糟。
    validate_template(SCOPE, content)

    row = _baseline_row(db, BASELINE_NAME)
    if row is None:
        print(f"  库里没有《{BASELINE_NAME}》这条系统基线行 —— 本脚本只改已有行。")
        print("  （新建库由迁移 0022 种进去；已有库却没这一行，说明被删过，"
              "先查清楚是谁删的再补。）")
        return 1

    print(f"  目标行 #{row.id}《{row.name}》"
          f"（book_id={row.book_id}, user_id={row.user_id}）")
    rows = _diff(row.content, content)
    if not rows:
        print("  库里已经是仓库那份 —— 无需改动（幂等）。")
        return 0

    print(f"  有 {len(rows)} 处不同：")
    for path, a, b in rows:
        sa = json.dumps(a, ensure_ascii=False)
        sb = json.dumps(b, ensure_ascii=False)
        if len(sa) > 90:
            sa = sa[:87] + "…"
        if len(sb) > 90:
            sb = sb[:87] + "…"
        print(f"    {path}\n        库里: {sa}\n        仓库: {sb}")

    if not apply:
        print("  （dry-run：没有写库）")
        return 0

    # 改之前先留一份底：基线版式改错了，照着这份 JSON 就能还原。
    backup = write_backup(db)
    print(f"  改前已备份：{backup}")

    before = dict(row.content or {})
    actor = db.scalars(select(SysUser).order_by(SysUser.id)).first()
    row.content = content
    row.paper = str(content.get("paper") or "A4")
    db.flush()
    log(db, action="UPDATE_PRINT_TEMPLATE", entity="print_template",
        entity_id=row.id, user=actor,
        detail="把「基础模板」基线的内容同步成仓库里那份出厂内容",
        before=before, after=content)
    # 这一条审计日志必须跟着版式改动**一起落库**：session 是 autoflush=False
    # （``app/core/db.py``），不显式 flush 它就只挂在会话里 —— 读的人看不到。
    db.flush()

    print(f"  已写入：#{row.id}《{row.name}》的内容 = 仓库那份")
    return 0


def sync_main(apply: bool) -> int:
    """``sync_content`` 的命令行外壳：自己开 session、自己提交。"""
    assert_schema_ready()
    db = SessionLocal()
    try:
        rc = sync_content(db, apply)
        if rc == 0 and apply:
            db.commit()
            print("\n已提交。")
        else:
            db.rollback()
            if rc == 0:
                print("\n（dry-run：已回滚，库未改动）")
        return rc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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

    if "--sync-content" in sys.argv:
        return sync_main(apply)

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
