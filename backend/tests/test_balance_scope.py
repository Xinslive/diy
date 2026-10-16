"""科目余额表的**科目范围**（`subject_scope`：仅一级 / 仅明细 / 全部）。

余额表默认只列一级科目（一级 62 行对明细 1253 行），而**打印预览和导出 Excel
走的是后端**。屏幕上的范围不带过去的话，用户看到的是 62 行、打出来 1253 行 ——
他不会以为导出坏了，只会把纸上那一份当成对的，而余额表正是交给客户和审计的
纸面数。所以这条口径必须一路带到三个通道，且只有一份实现。

每一条都单独钉住：

* 三档互斥且档位之间不加不减 —— 一级与明细在「顶层科目没有下级」时**同时**成立
  （启明物业 65 个科目里 58 个一级、62 个明细，重叠 55 个），不能按「明细 ⊂ 全部」
  想当然地写断言
* 三档看到的加总必须一样（一级行是下级的上卷），否则试算那条 check 会跟屏幕打架
* 三通道（屏幕 / 打印 / 导出）同一档位，答案一致
* 不传参数时是全部科目（老链接、老书签还得能用）
* 未知档位按全部处理：宁可多给几行，也不能给一份缺行的账
"""
from __future__ import annotations

import io
import re
from datetime import date

from openpyxl import load_workbook

from app.ledger import queries

# 1002 银行存款：顶层（测试账套装的是用友 4-2-2 表，它没有下级）
# 5602 管理费用：顶层且带下级
# 560206 水电费：5602 的明细，挂在 5602 下面 —— 用它才看得出两档真的分开了
TOP = "5602"
LEAF = "560206"


def _mv(make_voucher, period):
    """记一笔把明细科目 560206 顶起来的凭证：借 水电费 100 / 贷 银行存款 100。

    1002 是顶层叶子，560206 是 5602 的明细。于是 5602 这一行会有发生额，
    「仅一级」和「仅明细」列出来的编码集合必然不同 —— 这正是要验的差别。
    """
    day = date(period.year, period.month, 1)
    return make_voucher(day, dr=[(LEAF, 100, "水电费")], cr=[("1002", 100, "水电费")],
                        posted=True)


def _rows(db, book, period):
    subjects = queries.subject_tree(db, book.id)
    return queries.balance_sheet_of_accounts(db, book.id, subjects, period, True)[0]


def _codes(rows):
    return {r["code"] for r in rows}


#: 打印页每行第一个单元格就是「编码」列。**必须按整格取**，两个坑：
#:
#: 1. `"5602" in html` 会命中 560201、560202…，那不叫「纸上有一级科目」，
#:    那叫「纸上有一堆以 5602 开头的明细」。
#: 2. 汇总行（一级科目）的 td 是 `class="ctr total"`，不是 `class="ctr"`
#:    （render_table_page 命中行级 __kind=TOTAL 就加 total 类），
#:    只写死 `class="ctr"` 会把**所有一级科目行整片漏掉** —— 症状是断言说
#:    「纸上没有 5602」，而 5602 明明在纸上。类名要放宽到含 ctr 即可。
_CODE_CELL = re.compile(r'<tr[^>]*><td class="[^"]*\bctr\b[^"]*">\s*(\d+)\s*</td>')


def _paper_codes(html: str) -> set[str]:
    return set(_CODE_CELL.findall(html))


# ----------------------------------------------------------------- 纯函数


def test_三档各自取该取的():
    rows = [
        {"code": "1002", "level": 1, "is_leaf": True},    # 顶层且是叶子：两档都算
        {"code": TOP, "level": 1, "is_leaf": False},       # 顶层有下级：只在「一级」里
        {"code": LEAF, "level": 2, "is_leaf": True},      # 明细：只在「明细」里
    ]
    assert _codes(queries.filter_balance_rows(rows, "top")) == {"1002", TOP}
    assert _codes(queries.filter_balance_rows(rows, "leaf")) == {"1002", LEAF}
    assert _codes(queries.filter_balance_rows(rows, "all")) == {"1002", TOP, LEAF}


def test_不传档位就是全部_老链接还得能用():
    """默认值必须是 all：打印链接可能被存成书签、可能被外部系统直接调。

    反过来想更诱人 —— 默认 top 跟屏幕一致。但那是拿「老调用方突然少了几千行」
    换一个「大多数时候点导出的是本页自己的链接」，不划算。
    """
    rows = [{"code": "1002", "level": 1, "is_leaf": True},
            {"code": LEAF, "level": 2, "is_leaf": True}]
    assert _codes(queries.filter_balance_rows(rows)) == {"1002", LEAF}


def test_未知档位按全部处理():
    """查询参数的值是外部输入。缺行的账看不出问题（用户只会以为这张账就这么点），
    多给行的账用户自己会发现多了什么 —— 两个方向的代价不对称。"""
    rows = [{"code": "1002", "level": 1, "is_leaf": True},
            {"code": LEAF, "level": 2, "is_leaf": True}]
    assert _codes(queries.filter_balance_rows(rows, "TOP")) == {"1002", LEAF}
    assert _codes(queries.filter_balance_rows(rows, "")) == {"1002", LEAF}


def test_过滤不碰原列表():
    """要是在 rows 上就地过滤，第二次过滤就只剩一档了 —— 而三个通道共享同一份结果。"""
    rows = [{"code": "1002", "level": 1, "is_leaf": True},
            {"code": TOP, "level": 1, "is_leaf": False},
            {"code": LEAF, "level": 2, "is_leaf": True}]
    queries.filter_balance_rows(rows, "top")
    assert len(rows) == 3, "原列表被就地改了，第二次过滤会只剩一档"


# ----------------------------------------------------------------- 加总不变


def test_一级和明细看到的发生额加总一样(db, book, make_voucher, periods):
    """一级行是下级的上卷，所以「仅一级」和「仅明细」看到的加总相同。

    这条是试算平衡那条 check 能跟屏幕共存的根据：check 只累计叶子（否则父级
    汇总行会重复计数），而页面切档位时 check 不重算 —— 只有这两档加总相等，
    屏幕上的数才和 check 对得上。

    **「全部科目」不在这个等式里，而且不应该在**：那一档里 5602 这一行和它的
    明细 560206 同时在表上，加起来是双倍。这不是 bug，是「全部科目」本来就是
    一张带汇总行的树，用户自己会知道不能整列相加；反过来说，这条断言正好钉住
    「为什么 check 不能改成累计全部行」。
    """
    _mv(make_voucher, periods[1])
    rows = _rows(db, book, periods[1])
    totals = {
        s: sum(r["period_debit"] for r in queries.filter_balance_rows(rows, s))
        for s in queries.BALANCE_SUBJECT_SCOPES
    }
    assert totals["top"] > 0, "这笔凭证没顶到任何一级科目上，测不出差别"
    assert totals["top"] == totals["leaf"], (
        f"一级和明细的加总不等，屏幕上的数会和试算条打架：{totals}")
    assert totals["all"] == totals["top"] + totals["leaf"], (
        f"「全部科目」既不是 top 也不是 leaf，说明树里还有别的行没收干净：{totals}")


# ----------------------------------------------------------------- 打印 / 导出


def test_打印预览认范围(client, auth, db, book, make_voucher, periods):
    _mv(make_voucher, periods[1])
    url = f"/api/books/{book.id}/print/balance.html"

    top = client.get(url, params={"period_id": periods[1].id,
                                  "subject_scope": "top"}, headers=auth)
    assert top.status_code == 200, top.text
    assert TOP in _paper_codes(top.text), f"仅一级：纸上没有 {TOP}，实得 {_paper_codes(top.text)}"
    assert LEAF not in _paper_codes(top.text), "仅一级：纸上混进了明细科目"

    leaf = client.get(url, params={"period_id": periods[1].id,
                                   "subject_scope": "leaf"}, headers=auth)
    assert leaf.status_code == 200, leaf.text
    assert LEAF in _paper_codes(leaf.text), f"仅明细：纸上没有 {LEAF}"
    assert TOP not in _paper_codes(leaf.text), "仅明细：纸上混进了一级科目"


def test_导出Excel认范围(client, auth, db, book, make_voucher, periods):
    _mv(make_voucher, periods[1])
    url = f"/api/books/{book.id}/print/balance.xlsx"

    def codes(params):
        r = client.get(url, params={"period_id": periods[1].id, **params}, headers=auth)
        assert r.status_code == 200, r.text
        sheet = load_workbook(io.BytesIO(r.content)).active
        return {str(c.value) for row in sheet.iter_rows() for c in row
                if c.value is not None and str(c.value).strip().isdigit()}

    assert TOP in codes({"subject_scope": "top"}), "仅一级：文件里没有管理费用"
    assert LEAF not in codes({"subject_scope": "top"}), "仅一级：文件里混进了明细科目"
    assert LEAF in codes({"subject_scope": "leaf"}), "仅明细：文件里没有水电费"
    assert TOP not in codes({"subject_scope": "leaf"}), "仅明细：文件里混进了一级科目"


def test_三个通道同一档位答案一致(client, auth, db, book, make_voucher, periods):
    """屏幕、打印、导出问的是同一张表。屏幕是前端过滤、纸面是后端过滤，
    两边各写一套判据迟早对不上 —— 这条把它们钉在一起。"""
    _mv(make_voucher, periods[1])

    screen = client.get(f"/api/books/{book.id}/ledger/balance",
                        params={"period_id": periods[1].id}, headers=auth).json()
    screen_top = {r["code"] for r in screen["rows"] if r["level"] == 1}

    paper = client.get(f"/api/books/{book.id}/print/balance.html",
                       params={"period_id": periods[1].id, "subject_scope": "top"},
                       headers=auth).text

    missing = {c for c in screen_top if c not in _paper_codes(paper)}
    assert not missing, f"屏幕上「仅一级」有、纸上没有：{sorted(missing)}"
