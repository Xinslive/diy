"""凭证「基础模板」基线：仓库、迁移、落地脚本是**同一份**（2026-10-30）。

用户 2026-10-30 批注：「把本地开发库的『基础摸板』最新的参数，更新进仓库，
作为基线，替换现在的 tpl=2 的旧参数」。

**为什么这件事要单独盯。** 出厂 content 住在迁移 0022 里（迁移必须自包含，
不能 import 应用代码），但**已经在跑的库不会被再种一次** —— 迁移的 upgrade
按 (scope, name, book_id IS NULL) 判存在，存量库里已有「基础模板」就整条跳过。
于是只改迁移的话：

* **新库**是新参数（迁移种进去的）；
* **老库**是旧参数（当年种的那一份还在原地）。

两套环境各说各话 —— 同一张纸，在新环境印是一页两张，在老环境印是一页一张。
``backend/scripts/set_voucher_baseline.py --sync-content`` 就是把老库拉齐用的。

盯的是四件事：

1. **迁移里内嵌的那份是合法的模板** —— 自己都不合法的出厂参数写进去，
   等于把一份坏版式发给所有账套；
2. **迁移里内嵌的那份 == 落地脚本读出来的那份** —— 两处各写一份的话，
   改了一处忘了另一处，新库老库又不一样；
3. **迁移种进新库的那份 == 仓库那份**（测试库就是新库：迁移刚种进去的）；
4. **那份内容确实带着用户调过的东西** —— 「换了基线」换了个寂寞，
   参数悄悄回到旧值，是最难被发现的一种回归。
"""
import importlib.util
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.printing import PrintTemplate
from app.printing.template import validate_template

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0022_voucher_baseline_templates.py"
SCRIPT = ROOT / "scripts" / "set_voucher_baseline.py"


def _load(path: Path, name: str):
    """按文件路径加载（``scripts/`` 没有 ``__init__.py``，它不是个包）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def migration():
    return _load(MIGRATION, "cwq_migration_0022")


@pytest.fixture(scope="module")
def script():
    return _load(SCRIPT, "cwq_set_voucher_baseline")


@pytest.fixture(autouse=True)
def backup_dir(tmp_path, monkeypatch):
    """备份目录指到临时目录。

    **不指的话，测试会往这台机器的真实备份目录里写文件** —— 本地是仓库的
    ``.run/``，而 CI 的检出里没有 ``.run/``，按「存在才用」的旧判据会一路
    落到生产的 ``/root/cwq-baseline-backups/``，而 CI 的 runner 不是 root，
    写不进去 —— 那正是这一版第一次上 CI 挂掉的原因。
    """
    target = tmp_path / "baseline-backups"
    monkeypatch.setenv("CWQ_BASELINE_BACKUP_DIR", str(target))
    return target


def _entry(mod, name: str) -> dict:
    for entry in mod.BASELINE:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"迁移里没有基线《{name}》")


def _seeded_baseline(db) -> PrintTemplate:
    """测试库里由迁移 0022 种下的那条「基础模板」基线行。"""
    row = db.scalar(select(PrintTemplate).where(
        PrintTemplate.scope == "VOUCHER",
        PrintTemplate.book_id.is_(None),
        PrintTemplate.user_id.is_(None),
        PrintTemplate.name == "基础模板",
    ))
    assert row is not None, "测试库里没有迁移种下的「基础模板」基线行"
    return row


# --------------------------------------------------------------------- 仓库里那一份


def test_迁移里内嵌的基线内容是合法模板(migration):
    """自己都不合法的出厂参数，不能发给所有账套。"""
    content = _entry(migration, "基础模板")["content"]
    tpl = validate_template("VOUCHER", content)
    assert tpl["paper"] == "A4"
    assert tpl["elements"], "基线一个元素都没有 —— 纸上什么都不会有"


def test_内嵌JSON原样贴着序列化器的输出(migration):
    """内嵌的那一段必须是 ``json.dumps(..., ensure_ascii=False, indent=2)``
    **原样**贴上去的 —— 手工折行会在 JSON 字符串值里折出裸换行，
    ``json.loads`` 直接报 ``Invalid control character``。

    这也是外层必须用 **raw 字符串**的原因：``\\n`` 得由 json 去解释，
    不能由 Python 先解释一遍。
    """
    raw = migration.CONTENT_PLAIN
    parsed = json.loads(raw)
    again = json.dumps(parsed, ensure_ascii=False, indent=2)
    assert raw.strip("\n") == again, "内嵌的 JSON 与序列化器的输出不一致（多半是手工折过行）"


def test_迁移与落地脚本读的是同一份(migration, script):
    """**单一出处**：新建的库（迁移种进去的）与已经在跑的库（脚本同步的）
    读的是同一份内容。两处各写一份的话，改一处忘一处就又分叉了。"""
    for name in ("基础模板", "凭证纸专用"):
        assert script.factory_content(name) == _entry(migration, name)["content"], name


def test_基线带着用户调过的那些参数(migration):
    """**换了基线不能换了个寂寞。**

    用户在设计器里调完保存的那份，与当初种下的 tpl=2 那份相比，实质变化是：

    * ``voucher.slots_per_page`` = 2（一页两张；旧的那份没这个字段 = 一页一张）
    * 合计大写那一行（``elements[capital]``）的文案、字重、坐标都调过了
    """
    content = _entry(migration, "基础模板")["content"]
    assert content["voucher"]["slots_per_page"] == 2, "基线不是一页两张 —— 参数回到旧值了"
    capital = next(e for e in content["elements"] if e["id"] == "capital")
    assert capital["bold"] is True
    assert capital["text"] == "合　计：{capital}"
    assert (capital["x"], capital["y"]) == (4.0, 74.2)


def test_凭证纸专用不在这次替换范围内(migration):
    """它是这一档的默认打印版式，用户 2026-10-29 明确说过它不参与基线换人。"""
    entry = _entry(migration, "凭证纸专用")
    assert entry["is_default"] is True
    assert entry["paper"] == "A5"
    assert entry["content"] != _entry(migration, "基础模板")["content"]


# --------------------------------------------------------------------- 新库 / 老库


def test_新库种进去的那份就是仓库那份(db, script):
    """**新库 = 新参数**：测试库是迁移刚种过的，它种进去的必须与仓库同一份。

    这一条是整套改动的落点 —— 用户要的就是「新库建出来就是最新版」。
    """
    row = _seeded_baseline(db)
    assert row.content == script.factory_content("基础模板"), \
        "迁移种进新库的那份与仓库里那份不是同一份"


def test_库里已经是仓库那份时不动它(db, script):
    """幂等：已经是同一份就别写库（白写一次审计日志、多一次 updated_at）。"""
    row = _seeded_baseline(db)
    before = dict(row.content or {})
    assert script.sync_content(db, apply=True) == 0
    db.refresh(row)
    assert row.content == before


def test_老库的那份被换成仓库里的新参数(db, script):
    """**老库是老参数**正是要治的那件事：内容换成仓库那份，归属不动。"""
    new_content = script.factory_content("基础模板")
    old_content = json.loads(json.dumps(new_content))
    old_content["elements"] = [e for e in old_content["elements"] if e["id"] != "capital"]
    old_content["voucher"] = {**old_content["voucher"], "slots_per_page": 1}

    row = _seeded_baseline(db)
    row.content = old_content
    db.flush()

    assert script.sync_content(db, apply=True) == 0
    db.refresh(row)
    assert row.content == new_content, "内容没换成仓库那份"
    assert row.user_id is None and row.book_id is None, "归属被改了 —— 这一步只该动内容"
    assert row.paper == "A4"


def test_同步会留审计日志(db, script):
    """改了基线版式是**留痕**的事：审计日志里要有这一条。"""
    from app.models.audit import AuditLog

    new_content = script.factory_content("基础模板")
    old_content = json.loads(json.dumps(new_content))
    old_content["voucher"] = {**old_content["voucher"], "slots_per_page": 1}
    row = _seeded_baseline(db)
    row.content = old_content
    db.flush()

    assert script.sync_content(db, apply=True) == 0
    entry = db.scalar(select(AuditLog).where(
        AuditLog.entity == "print_template",
        AuditLog.entity_id == str(row.id),
        AuditLog.action == "UPDATE_PRINT_TEMPLATE",
    ).order_by(AuditLog.id.desc()))
    assert entry is not None, "同步基线内容没有留审计日志"
    assert entry.after == new_content


def test_没有基线行时只报告不新建(db, script, capsys):
    """库里没有这一行，说明被人删过 —— 先查清楚是谁删的，别默默补一份。"""
    row = _seeded_baseline(db)
    db.delete(row)
    db.flush()

    assert script.sync_content(db, apply=True) == 1
    out = capsys.readouterr().out
    assert "没有" in out
    rows = db.scalars(select(PrintTemplate).where(
        PrintTemplate.scope == "VOUCHER",
        PrintTemplate.book_id.is_(None),
        PrintTemplate.user_id.is_(None),
        PrintTemplate.name == "基础模板",
    )).all()
    assert not rows, "脚本不该自己新建基线行"


def test_改之前先留一份底(db, script, backup_dir):
    """**基线版式改错了要能还原** —— 所以改之前先把这一范围的全部模板导一份 JSON。

    备份目录跟着这台机器的既有惯例走（仓库里 ``.run/``、生产上
    ``/root/cwq-baseline-backups/``）；测试里由 autouse 的 fixture 指到临时
    目录，不往这台机器的真实备份目录里写东西。
    """
    new_content = script.factory_content("基础模板")
    old_content = json.loads(json.dumps(new_content))
    old_content["voucher"] = {**old_content["voucher"], "slots_per_page": 1}
    row = _seeded_baseline(db)
    row.content = old_content
    db.flush()

    assert script.sync_content(db, apply=True) == 0

    files = list(backup_dir.glob("print_template_*.json"))
    assert len(files) == 1, f"备份没落盘：{list(tmp_path.iterdir())}"
    snapshot = json.loads(files[0].read_text(encoding="utf-8"))
    saved = next(t for t in snapshot if t["id"] == row.id)
    assert saved["content"] == old_content, "备份的是改完之后的内容 —— 那不叫备份"


def test_备份写不进去就不改(db, script, tmp_path, monkeypatch):
    """**留不下底就不动版式。**

    备份目录写不进去时直接放弃，而不是改完了才发现没得还原 —— 这条
    在 CI 上是真实踩过的：runner 不是 root，备份目录写不进去。
    """
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("我是文件，不是目录", encoding="utf-8")
    monkeypatch.setenv("CWQ_BASELINE_BACKUP_DIR", str(blocked / "backup"))

    new_content = script.factory_content("基础模板")
    old_content = json.loads(json.dumps(new_content))
    old_content["voucher"] = {**old_content["voucher"], "slots_per_page": 1}
    row = _seeded_baseline(db)
    row.content = old_content
    db.flush()

    with pytest.raises(SystemExit) as exc:
        script.sync_content(db, apply=True)
    assert "备份写不进去" in str(exc.value)

    db.refresh(row)
    assert row.content == old_content, "备份没写成，却把版式改了 —— 那就没有还原的东西"


def test_备份目录的默认判定(script, monkeypatch):
    """**判据是「有 .git」，不是「.run 已存在」。**

    这正是这一版第一次上 CI 挂掉的地方：CI 的检出是干净工作树，
    ``.run/`` 根本不存在 —— 按「存在才用」的旧判据会一路落到生产的
    ``/root/cwq-baseline-backups/``，而 runner 不是 root，写不进去。

    所以：仓库检出里（含 CI）落到仓库自己的 ``.run/``（gitignore 的），
    生产机上没有仓库，落到那台机器的既有惯例目录。
    """
    monkeypatch.delenv("CWQ_BASELINE_BACKUP_DIR", raising=False)
    repo_root = Path(__file__).resolve().parents[2]
    assert (repo_root / ".git").exists(), "测试跑的地方不像仓库检出"
    assert script.backup_dir() == repo_root / ".run"

    monkeypatch.setenv("CWQ_BASELINE_BACKUP_DIR", "/tmp/某处")
    assert script.backup_dir() == Path("/tmp/某处")


def test_基础模板出厂就锁定字体(migration):
    """**「基础模板」出厂即开锁定字体**（用户 2026-10-30：
    「系统基线改成默认打开锁定字体」）。

    锁定字体 = 打印时用随应用发布的那份思源宋体，而不是各台机器自己的
    系统字体 —— 换台机器印出来一模一样。
    """
    content = _entry(migration, "基础模板")["content"]
    assert content["font"].get("embed") is True, "基础模板的基线没有默认锁定字体"


def test_凭证纸专用一个字都没动(migration):
    """**「凭证纸专用」不许跟着改。**

    用户 2026-10-30 明确说过：「不要影响凭证纸专用，这是我好不容易调出来的」。
    它用的是**仿宋** —— 锁了字体就变成宋体系，那是另一副字。

    所以这份基线 content 里**不许出现** ``font.embed``：没有这个字段就走
    默认值（关），那份版式仍然是系统字体，与从前逐字相同。
    """
    content = _entry(migration, "凭证纸专用")["content"]
    assert "embed" not in (content.get("font") or {}), (
        "凭证纸专用被改了 —— 它一个字都不该动"
    )
    assert content["font"]["family"] == "仿宋", "凭证纸专用的字体被换了"


def test_基线里不许拿半角空格做字距(migration):
    """**字距必须用全角空格（U+3000），不能拿半角空格凑。**

    半角空格（U+0020）的宽度**由字体决定**：SimSun 里是 0.5em，
    Songti SC 里只有 0.2em。同一份模板，换台机器（字体回退不同），
    「合　计」三个字之间的空隙就会从**一个字宽变成三个字宽** —— 印出来
    一张纸挤得慌、另一台机器上又散开。

    2026-10-30 用户就是撞上这个：「本地开发端看着很协调，生产的空格太宽了」。
    实测：同一份 HTML 在同一台机器上，Chromium 量出来是 1.25 个字宽
    （macOS 上「宋体」回退到 Songti SC，半角空格 0.2em）；换到有 SimSun 的
    机器上同样的 5 个半角空格就是 2.5~3 个字宽。

    **全角空格的 advance 恒为 1em**，任何字体都一样 —— 这才是「宽度固定为
    一个字宽」的做法。仓库里总账那几行早就是这么写的（「累　　计」）。

    一次空格（单个 U+0020）不在这条规矩里：标题「记 账 凭 证」用单空格
    是一种排版风格，宽一点窄一点都在可接受范围。
    """
    # **只盯「基础模板」。** 「凭证纸专用」是用户亲手调出来的那份
    # （2026-10-30：「不要影响凭证纸专用，这是我好不容易调出来的」），
    # 它的字距怎么写都不由这条规矩管。
    content = _entry(migration, "基础模板")["content"]
    for el in content.get("elements") or []:
        text = el.get("text") or ""
        assert not re.search(r"  +", text), (
            f"《基础模板》的 {el.get('id')} 元素用了连续半角空格做字距：{text!r}"
            f" —— 宽度随字体变，改成一个全角空格 U+3000"
        )
