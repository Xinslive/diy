"""UFO 导入：报错文案要指向**真正的原因**（审计 P3-1）。

利润表分支以前是「先算行次列、后判金额列数量」。顺序反了的时候，用户看到的
引导指向错误的列：

* 模板有**三个**金额列（如多一个「上年同期金额」）→ 真正的原因是「金额列多
  了一个」，但更准的那条文案被前面的判断挡住了；
* 模板有两个写着「行次/行号」的列 + 一个金额列 → 真正的原因是「金额列只
  有一个」，却报「无法确定利润表的行次列」—— 用户照着提示去检查行次列怎么
  改都改不对，因为行次列没问题。

**文案里必须带实际列号**：只说「多于两个金额列」而不指明是哪几列，用户还是
得自己回头数格子 —— 而他真正要改的就是那几列。
"""
from __future__ import annotations

import pytest

from app.reports.importer import TemplateImportError, parse_ufo_report


def _cell(text="", formula="", missing=False):
    return {"text": text, "formula": formula, "missing": missing}


def _grid(header: list[str], body: list[list]) -> list[list[dict]]:
    """表头一行 + 表体若干行。"""
    return [[_cell(text) for text in header]] + [
        [_cell(text) for text in row] for row in body
    ]


def _patch(monkeypatch, grid):
    import app.reports.importer as importer

    monkeypatch.setattr(importer, "_stream", lambda raw: b"fixture")
    monkeypatch.setattr(importer, "_grid", lambda data: (grid, "UFO8.00"))


def test_三个金额列时报多金额列而不是行次列(monkeypatch):
    """四个非首列里有三个带金额字样 —— 真正的原因是「金额列多了一个」。

    以前这里先撞上行次列那条，报「无法确定利润表的行次列，请检查模板版式」。
    """
    _patch(monkeypatch, _grid(
        ["项     目", "本期金额", "本年累计金额", "上年同期金额"],
        [["房租收入", "", "", ""]],
    ))

    with pytest.raises(TemplateImportError) as exc:
        parse_ufo_report(b"fixture", "多一个金额列的利润表.rep")
    message = str(exc.value)
    assert "多于两个金额列" in message, message
    assert "行次列" not in message, f"文案仍然指向错误的列：{message}"
    # 实际列号要写出来（第 2、3、4 列）
    assert "2" in message and "3" in message and "4" in message, message


def test_只有一个金额列时报缺一列而不是行次列(monkeypatch):
    """两列写着「行次/行号」+ 一个金额列 —— 真正的原因是「金额列只一个」。

    以前 ``line_source_cols`` 算成两列 → 先抛「无法确定利润表的行次列」。
    """
    _patch(monkeypatch, _grid(
        ["项     目", "行次", "行号", "本期金额"],
        [["房租收入", "1", "1", ""]],
    ))

    with pytest.raises(TemplateImportError) as exc:
        parse_ufo_report(b"fixture", "少一个金额列的利润表.rep")
    message = str(exc.value)
    assert "只有一个金额列" in message, message
    assert "行次列" not in message, f"文案仍然指向错误的列：{message}"
    assert "4" in message, f"文案没有指出是哪一列：{message}"


def test_版式判定的文案带这份模板的实际列数(monkeypatch):
    """「当前只支持 4 列利润表和 8 列资产负债表」只说了支持什么。

    用户对不上号 —— 他手里那份是几列？文案里要写出来。

    这一条要过**真的** ``_grid``（版式判定就住在它里面），所以用真实的 UFO
    二进制流，别 mock 掉 —— mock 掉之后测的是下游逻辑，不是这条判定。
    """
    import app.reports.importer as importer
    from tests.test_report_template_import import _ufo_stream

    cells = {
        (1, 1): ("项     目", ""), (1, 2): ("行次", ""),
        (1, 3): ("本月金额", ""), (1, 4): ("本年累计金额", ""),
        (1, 5): ("上年同期", ""),
        (2, 1): ("房租收入", ""), (2, 2): ("1", ""),
        (2, 3): ("", 'fs(5001,月,"贷",,年)'),
        (2, 5): ("123", ""),
    }
    stream = _ufo_stream(cells, rows=2, cols=5)
    monkeypatch.setattr(importer, "_stream", lambda raw: stream)

    with pytest.raises(TemplateImportError) as exc:
        parse_ufo_report(b"fixture", "带比较列的利润表.rep")
    message = str(exc.value)
    assert "5 列" in message, message
    assert "4 列利润表" in message, message


def test_两个金额列的正常模板照常导入(monkeypatch):
    """改了顺序不许把正常模板也拒了。"""
    _patch(monkeypatch, _grid(
        ["项     目", "行次", "本月金额", "本年累计金额"],
        [["房租收入", "1", "", ""]],
    ))

    parsed = parse_ufo_report(b"fixture", "正常利润表.rep")
    assert [(c["col_no"], c["kind"]) for c in parsed["cols"]] == [
        (1, "STATIC"), (2, "FORMULA"), (3, "FORMULA")
    ]


def test_表头没有金额字样的老模板照常导入(monkeypatch):
    """老模板的列头可能压根不写「金额」（项目/行次/第1期/第2期）。

    那种模板以前是**能导入**的（按固定位置回落成第 2 列行次、第 3、4 列金额）。
    改判断顺序不许顺手把它拒了 —— 那是既有能力，不是缺陷。
    """
    _patch(monkeypatch, _grid(
        ["项     目", "行次", "第1期", "第2期"],
        [["房租收入", "1", "", ""]],
    ))

    parsed = parse_ufo_report(b"fixture", "老式利润表.rep")
    assert [(c["col_no"], c["kind"]) for c in parsed["cols"]] == [
        (1, "STATIC"), (2, "FORMULA"), (3, "FORMULA")
    ]
