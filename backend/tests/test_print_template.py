"""打印模板引擎：模板归一化、纸型、金额大写、HTML 渲染。"""
from decimal import Decimal

import pytest

from app.printing.template import (
    DEFAULT_TEMPLATES,
    PAPERS,
    SCOPES,
    merge_defaults,
    render_table_page,
    render_voucher_page,
    to_cn_capital,
    validate_template,
)


# --------------------------------------------------------------------- 金额大写


@pytest.mark.parametrize(
    "raw,want",
    [
        ("0", "零元整"),
        ("0.05", "零元伍分"),
        ("0.10", "零元壹角整"),
        ("0.50", "零元伍角整"),
        ("0.57", "零元伍角柒分"),
        ("305.00", "叁佰零伍元整"),
        ("1234.56", "壹仟贰佰叁拾肆元伍角陆分"),
        ("100000", "壹拾万元整"),
        ("100001", "壹拾万零壹元整"),
        ("100100", "壹拾万零壹佰元整"),
        ("1000000", "壹佰万元整"),
        ("20000.07", "贰万元零角柒分"),
        ("100000000", "壹亿元整"),
        ("101000000", "壹亿零壹佰万元整"),
        ("3050000.50", "叁佰零伍万元伍角整"),
        ("-1234.56", "负壹仟贰佰叁拾肆元伍角陆分"),
    ],
)
def test_金额大写(raw, want):
    assert to_cn_capital(Decimal(raw)) == want


def test_金额大写非法输入返回空串():
    assert to_cn_capital("不是数字") == ""


# --------------------------------------------------------------------- 模板归一化


def test_每个内置范围都能通过校验():
    for scope in SCOPES:
        tpl = validate_template(scope, {})
        assert tpl["columns"], scope
        assert abs(sum(c["width"] for c in tpl["columns"]) - 100) < 0.01, scope


def test_列宽归一化到100():
    tpl = validate_template("BALANCE", {"columns": [
        {"key": "code", "width": 1},
        {"key": "name", "width": 1},
        {"key": "opening_debit", "width": 2},
    ]})
    widths = [c["width"] for c in tpl["columns"]]
    assert widths == [25.0, 25.0, 50.0]


def test_未知纸型报错并列出可选项():
    with pytest.raises(ValueError) as e:
        validate_template("VOUCHER", {"paper": "泰坦纸"})
    assert "A4" in str(e.value)


def test_横版纸型强制横向():
    tpl = validate_template("VOUCHER", {"paper": "A4_LANDSCAPE", "orientation": "portrait"})
    assert tpl["orientation"] == "landscape"


def test_非法方向被纠正为纵向():
    assert validate_template("VOUCHER", {"orientation": "diagonal"})["orientation"] == "portrait"


def test_非法对齐被纠正为左对齐():
    tpl = validate_template("VOUCHER", {"columns": [{"key": "a", "align": "居中"}]})
    assert tpl["columns"][0]["align"] == "left"


def test_没有列时报错():
    with pytest.raises(ValueError):
        validate_template("VOUCHER", {"columns": []})


def test_列缺少key时报错():
    with pytest.raises(ValueError) as e:
        validate_template("VOUCHER", {"columns": [{"label": "没写字段名"}]})
    assert "key" in str(e.value)


def test_负页边距被夹到0():
    tpl = validate_template("VOUCHER", {"margins": {"top": -5, "left": -1}})
    assert tpl["margins"]["top"] == 0.0
    assert tpl["margins"]["left"] == 0.0


def test_未知范围报错():
    with pytest.raises(ValueError):
        validate_template("PAYROLL", {})


# --------------------------------------------------------------------- 向前兼容


def test_新增配置项不影响老模板():
    """老模板里没有新字段时，靠默认值补齐 —— 这是「框架不妨碍迭代」的关键。"""
    old = {"title": {"text": "老模板"}}          # 模拟只存了标题的老数据
    tpl = validate_template("VOUCHER", old)
    assert tpl["title"]["text"] == "老模板"
    assert tpl["font"]["family"]                 # 老模板没存字体，靠默认值
    assert tpl["margins"]["top"] == 10.0
    assert tpl["footer"]["show_page_no"] is True


def test_merge_defaults不修改入参():
    src = {"title": {"text": "X"}}
    merge_defaults("VOUCHER", src)
    assert src == {"title": {"text": "X"}}


def test_覆盖值优先于默认值():
    tpl = validate_template("VOUCHER", {"title": {"text": "自定义"}, "font": {"size": 14}})
    assert tpl["title"]["text"] == "自定义"
    assert tpl["title"]["bold"] is True        # 没被覆盖的仍取默认
    assert tpl["font"]["size"] == 14
    assert tpl["font"]["family"] == "宋体"


# --------------------------------------------------------------------- 渲染


def test_凭证页含关键要素():
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_page(tpl, {"company": "紫欣星悦荟", "book": "租金账套"},
                               {"voucher_no": "记0001", "voucher_date": "2026-01-05",
                                "attachments": 2, "remark": "首月租金",
                                "total_debit": "1234.56", "total_credit": "1234.56",
                                "entries": [
                                    {"line_no": 1, "summary": "收租", "subject_code": "1002",
                                     "subject_name": "银行存款", "direction": "DEBIT",
                                     "amount": "1234.56"},
                                ]},
                               1, 1)
    assert "@page" in html
    assert "记0001" in html and "紫欣星悦荟" in html
    assert "壹仟贰佰叁拾肆元伍角陆分" in html   # 金额大写
    assert "首月租金" in html                     # 备注
    assert "附单据" in html                       # 附单据数
    assert "2026-01-05" in html                   # 制单日期
    assert "凭证字号" in html


def test_表格页含表头与行():
    tpl = validate_template("BALANCE", {})
    rows = [{"code": "1002", "name": "银行存款", "level": 1,
             "opening_debit": "200000", "period_credit": "97000"}]
    html = render_table_page(tpl, {"company": "X", "period": "2026-12"}, rows, 1, 1)
    assert "科目余额表" in html
    assert "银行存款" in html and "200,000.00" in html
    # 级次是居中列，不能被当成金额格式化成 "1.00"
    assert ">1<" in html and "1.00" not in html


def test_表格页空数据显示占位():
    html = render_table_page(validate_template("BALANCE", {}), {}, [], 1, 1)
    assert "无数据" in html


def test_渲染会转义HTML():
    """科目名里带尖括号时不能破坏 DOM。"""
    tpl = validate_template("BALANCE", {})
    html = render_table_page(tpl, {}, [{"name": "<script>x</script>", "code": "1"}], 1, 1)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_纸型映射表完整():
    for key in ("A4", "A3", "A5", "B5"):
        assert PAPERS[key]["w"] > 0 and PAPERS[key]["h"] > 0
    assert PAPERS["A4_LANDSCAPE"]["w"] > PAPERS["A4_LANDSCAPE"]["h"]
