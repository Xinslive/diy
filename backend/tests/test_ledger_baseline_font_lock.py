"""总账 / 明细账 / 科目余额表：**基线模板的「锁定字体」默认打开**。

**用户 2026-10-31：**「帮我把『总账』『明细账』『科目余额表』的基线模板的
『锁定字体』开关设置成打开，然后提交推送部署生产。」

**为什么是这三张纸。** 它们是**要拿给外部看的正式账簿与正式表**：总账是
用友式的「总分类账」，明细账是成册的分类账，科目余额表是给客户与审计方
看的那张表。字形（长什么样）必须换台机器一模一样 —— 这正是「锁定字体」
这一层要做的事（见 ``app/printing/fonts.py``）。

**为什么改的是 ``DEFAULT_TEMPLATES``（代码里的出厂基线）。** 基线分两处，
它们**必须是同一份内容**：

* **代码里的出厂默认**（``DEFAULT_TEMPLATES``）—— 某一范围一份存下来的模板
  都没有时，它就是唯一的兜底。科目余额表（``BALANCE``）至今没有基线行，
  它的基线就是这一份。
* **库里的基线行**（``print_template`` 里 ``book_id IS NULL AND user_id IS
  NULL`` 那份）—— 总账 / 明细账各有一行（2026-10-22 由用户自己调出来的那份
  定为基线），由 ``scripts/set_ledger_baseline.py`` 从代码默认值同步。

所以改代码一处，再用那个脚本把库里的拉齐 —— 两边不会各说各话
（这正是 ``set_ledger_baseline.py`` 文档里那句「改版式只改一处」的由来）。

**凭证范围一个字都没动。** 用户 2026-10-30 明确说过「不要影响凭证纸专用，
这是我好不容易调出来的」—— 它用的是**仿宋**，锁了字体就变成宋体系，那是另
一副字。它的基线 content 里**没有** ``font.embed`` 这个字段，走默认值（关），
与从前逐字相同。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import select

from app.printing import fonts as pf
from app.printing.template import (
    DEFAULT_TEMPLATES,
    render_ledger_book_page,
    render_table_page,
    validate_template,
)

#: 这一次开锁定的三个范围。
LOCKED_SCOPES = ("LEDGER", "DETAIL_LEDGER", "BALANCE")


def _load_ledger_baseline():
    """按文件路径加载 ``scripts/set_ledger_baseline.py``。

    不走 ``from scripts import set_ledger_baseline``：``scripts/`` 没有
    ``__init__.py``，它是一堆独立运维脚本而不是一个包（同
    ``test_yonyou_general_ledger.py``）。
    """
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "cwq_set_ledger_baseline_lock", root / "scripts" / "set_ledger_baseline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------- 出厂基线


def test_三个范围的出厂基线都锁定字体():
    """**总账 / 明细账 / 科目余额表：出厂即锁定。**

    锁定字体 = 打印时用随应用发布的那份思源宋体（Noto Serif SC），而不是
    各台机器自己的系统字体 —— 换台机器印出来一模一样。
    """
    for scope in LOCKED_SCOPES:
        font = DEFAULT_TEMPLATES[scope]["font"]
        assert font.get("embed") is True, f"{scope} 的出厂基线没有锁定字体"
        assert font.get("embed_font") == pf.DEFAULT_EMBEDDED, \
            f"{scope} 的出厂基线锁的不是登记在册的那份字体"


def test_归一化之后仍然是锁定的():
    """**归一化不许把开关洗掉。**

    ``validate_template`` 里的 ``font.embed`` 归一是**布尔化 + 登记在册的
    字体键**；归一化之后必须是「开」+ 那个键 —— 否则模板从库里读出来、
    过一遍校验就变成关的了，基线等于没改。
    """
    for scope in LOCKED_SCOPES:
        tpl = validate_template(scope, DEFAULT_TEMPLATES[scope])
        assert tpl["font"]["embed"] is True, f"{scope} 归一化之后开关被关了"
        assert tpl["font"]["embed_font"] == pf.DEFAULT_EMBEDDED


def test_凭证范围一个字都没动():
    """**凭证范围一个字都不该动。**

    用户 2026-10-30：「不要影响凭证纸专用，这是我好不容易调出来的」。
    它用的是**仿宋** —— 锁了字体就变成宋体系，那是另一副字。

    所以内置的凭证版式里**不许出现** ``font.embed``：没有这个字段就走
    默认值（关），那份版式仍然是系统字体，与从前逐字相同。
    """
    font = DEFAULT_TEMPLATES["VOUCHER"]["font"]
    assert "embed" not in font, "凭证范围被改了 —— 它一个字都不该动"
    assert "embed_font" not in font
    assert font["family"] == "宋体"


def test_报表范围一个字都没动():
    """**法定报表不在这一次范围内。**

    用户点的是三张纸：总账、明细账、科目余额表。报表（``REPORT``）没被点到，
    就不动它 —— 少做一件与用户要求无关的事，是这一层的分寸。
    """
    font = DEFAULT_TEMPLATES["REPORT"]["font"]
    assert "embed" not in font, "报表范围不该在这一次被改"


# --------------------------------------------------------------------- 渲染层


def test_总账打印页真的引了字体样式表():
    """**开关打开 = 打印页真的用上那份字体**，不是只把字段记下来了。

    字体族必须排在最前面 —— 排在后面等于没开（系统字体先命中了）。
    """
    tpl = validate_template("LEDGER", DEFAULT_TEMPLATES["LEDGER"])
    html = render_ledger_book_page(tpl, {}, [], page=1, pages=1, year="2026")
    version = pf.font_version("cwq-serif")
    assert version, "读不出字体的资源版本号 —— 样式表与代码对不上"
    assert f'href="/api/print/fonts/v/{version}/cwq-serif.css"' in html
    assert 'font-family: "CWQ Serif", "宋体"' in html
    assert "font-src 'self'" in html


def test_科目余额表打印页真的引了字体样式表():
    tpl = validate_template("BALANCE", DEFAULT_TEMPLATES["BALANCE"])
    html = render_table_page(tpl, {}, [], 1, 1)
    version = pf.font_version("cwq-serif")
    assert f'href="/api/print/fonts/v/{version}/cwq-serif.css"' in html
    assert 'font-family: "CWQ Serif", "宋体"' in html
    assert "font-src 'self'" in html


def test_关掉之后回到系统字体():
    """**开关能进也能退。** 关掉之后与从前**逐字相同**。

    用户存的那份模板若显式写了 ``font.embed: false``，它就该走系统字体 ——
    锁定字体是基线的默认值，不是不可撤销的强制。
    """
    tpl = validate_template("LEDGER", {"font": {"embed": False}})
    html = render_ledger_book_page(tpl, {}, [], page=1, pages=1, year="2026")
    assert 'font-family: "宋体", "SimSun", "Songti SC", serif;' in html
    assert "cwq-serif.css" not in html
    assert "font-src 'none'" in html


# --------------------------------------------------------------------- 库里的基线行


def test_基线脚本同步后库里的那份也是锁定的(db, book):
    """**库里的基线行必须跟着代码默认值走。**

    基线是「代码默认值 + 库里那一行」两处，内容**必须是同一份** ——
    改版式只改一处，另一处由 ``set_ledger_baseline.py`` 同步。两边各说各话
    的话，用户改的是一份、印出来的是另一份。
    """
    from app.models.printing import PrintTemplate

    script = _load_ledger_baseline()
    for scope in ("LEDGER", "DETAIL_LEDGER"):
        script.apply_scope(db, scope, apply=True)
        db.flush()
        base = db.scalar(select(PrintTemplate).where(
            PrintTemplate.scope == scope,
            PrintTemplate.book_id.is_(None),
            PrintTemplate.user_id.is_(None)))
        assert base is not None, f"{scope} 没有基线模板行"
        saved = validate_template(scope, base.content or {})
        assert saved["font"]["embed"] is True, \
            f"{scope} 的基线模板行没有锁定字体 —— 库里与代码各说各话了"
        assert saved["font"]["embed_font"] == pf.DEFAULT_EMBEDDED


def test_同步是幂等的(db, book):
    """**同步两次与同步一次结果一样。** 重复跑不得把基线搞乱。"""
    from app.models.printing import PrintTemplate

    script = _load_ledger_baseline()
    script.apply_scope(db, "LEDGER", apply=True)
    db.flush()
    first = db.scalar(select(PrintTemplate).where(
        PrintTemplate.scope == "LEDGER",
        PrintTemplate.book_id.is_(None),
        PrintTemplate.user_id.is_(None)))
    first_content = dict(first.content)

    script.apply_scope(db, "LEDGER", apply=True)
    db.flush()
    second = db.scalar(select(PrintTemplate).where(
        PrintTemplate.scope == "LEDGER",
        PrintTemplate.book_id.is_(None),
        PrintTemplate.user_id.is_(None)))
    assert second.id == first.id, "同步多出了一行基线"
    assert second.content == first_content, "同步不是幂等的"
