"""打印顺序：一页两张的错位配对 + 整叠倒着印（用户 2026-10-30 批注）。

**要解决的是打出来的一叠顺序不对，还得人工理一遍**：

* 一页两张凭证时，默认印成 1+2 / 3+4 / 5+6。沿中间裁开两半之后，
  上堆（每张纸的上半张）与下堆（下半张）各自都不是凭证顺序，两堆都得重排。
  错位配对印成 1+4 / 2+5 / 3+6，两堆各自成序。
* 「倒序打印」是**整页**翻的：第 3 张先印、第 1 张最后印，每张纸上配哪两格
  不变。打印机把先印的那张压在最上面还是把最后印的那张压在最上面，
  决定了这��要不要倒着来。
"""
import re

import pytest

from app.printing.template import (
    PRINT_ORDER_NORMAL,
    PRINT_ORDER_OPTIMIZED,
    PRINT_ORDERS,
    optimize_print_order,
)

#: 打印页上的凭证号：``记0001``（拆页时后面跟 ``- 001/002``）。
_VOUCHER_NO_RE = re.compile(r"记(\d{4})(?:\s*-\s*(\d+)/(\d+))?")


def _page_numbers(html: str) -> list[list[int]]:
    """把打印结果拆成「每页 → 该页各格的凭证序号」。

    按 ``<div class="page">`` 切页，按凭证号在页内取格序 —— 槽位是按顺序
    渲染的，页内出现的先后就是格子的先后。
    """
    pages = []
    for page in html.split('<div class="page">')[1:]:
        pages.append([int(m.group(1)) for m in _VOUCHER_NO_RE.finditer(page)])
    return pages


def _template(client, auth, book, slots: int, name: str) -> int:
    """建一份凭证模板，``slots`` = 一页放几张。"""
    r = client.post(
        f"/api/books/{book.id}/print/templates",
        json={"scope": "VOUCHER", "name": name,
              "content": {"voucher": {"slots_per_page": slots}}},
        headers=auth,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_vouchers(make_voucher, n: int, per: int = 1):
    """造 n 张凭证（同一期间，凭证号 1..n；每张 per 条分录）。"""
    for i in range(1, n + 1):
        make_voucher(f"2026-01-{i:02d}",
                     dr=[("1001", 100 + i, f"第{i}笔-{j}") for j in range(per)],
                     cr=[("5001", 100 + i, f"第{i}笔-{j}") for j in range(per)],
                     posted=True)


def _print(client, auth, book, period_id, **params):
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": period_id, **params}, headers=auth)
    assert r.status_code == 200, r.text
    return r.text


# --------------------------------------------------------------------- 纯算法


def test_六张凭证两张一张配成1加4_2加5_3加6():
    """错位配对：第 1 张印 1+4，第 2 张印 2+5，第 3 张印 3+6。"""
    assert optimize_print_order([1, 2, 3, 4, 5, 6], 2) == [[1, 4], [2, 5], [3, 6]]


def test_五张凭证时最后一格留空():
    """凭证数不是偶数时，靠后那一格印不出东西 —— 留空，不挪别人的凭证。"""
    assert optimize_print_order([1, 2, 3, 4, 5], 2) == [[1, 4], [2, 5], [3]]


def test_一张纸一张凭证时一页一格():
    """一页一张没有「两半」可裁，错位配对在这里什么也不做（退化成原顺序）。"""
    assert optimize_print_order([1, 2, 3], 1) == [[1], [2], [3]]


def test_只有一张凭证时不动():
    assert optimize_print_order([1], 2) == [[1]]


def test_重排不改变元素本身():
    items = [{"voucher_no": str(i)} for i in range(1, 7)]
    out = optimize_print_order(items, 2)
    assert [o["voucher_no"] for page in out for o in page] == \
        ["1", "4", "2", "5", "3", "6"]
    assert sorted(o["voucher_no"] for page in out for o in page) == \
        sorted(i["voucher_no"] for i in items)


def test_入参不被就地改写():
    items = [1, 2, 3, 4]
    optimize_print_order(items, 2)
    assert items == [1, 2, 3, 4]


# --------------------------------------------------------------------- 接口


def test_默认按原顺序印(client, auth, book, periods, make_voucher):
    """没给 print_order 就是老行为：1+2 / 3+4 / 5+6。"""
    _make_vouchers(make_voucher, 6)
    tpl_id = _template(client, auth, book, 2, "两张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id, template_id=tpl_id))
    assert pages == [[1, 2], [3, 4], [5, 6]]


def test_顺序优化后每页错位配对(client, auth, book, periods, make_voucher):
    """optimized：第 1 张 1+4、第 2 张 2+5、第 3 张 3+6。

    裁开两半：上堆（每张纸的上半格）自上而下 1、2、3，下堆 4、5、6。
    """
    _make_vouchers(make_voucher, 6)
    tpl_id = _template(client, auth, book, 2, "两张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id,
                                  template_id=tpl_id, print_order=PRINT_ORDER_OPTIMIZED))
    assert pages == [[1, 4], [2, 5], [3, 6]]
    assert [page[0] for page in pages] == [1, 2, 3]
    assert [page[1] for page in pages] == [4, 5, 6]


def test_顺序优化加倒序时整页反过来印(client, auth, book, periods, make_voucher):
    """倒序打印：第 1 张印 3+6、第 2 张印 2+5、第 3 张印 1+4。

    **每张纸上配哪两格不变**，翻的是**纸的先后** —— 跟着翻就把 3+6 印成了
    6+3，裁开之后两堆又对不上。
    """
    _make_vouchers(make_voucher, 6)
    tpl_id = _template(client, auth, book, 2, "两张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id, template_id=tpl_id,
                                  print_order=PRINT_ORDER_OPTIMIZED, reverse="true"))
    assert pages == [[3, 6], [2, 5], [1, 4]]


def test_倒序打印只翻纸不翻格(client, auth, book, periods, make_voucher):
    """没开错位配对时，倒序就是整叠反过来印（同一页里两格的先后不变）。"""
    _make_vouchers(make_voucher, 6)
    tpl_id = _template(client, auth, book, 2, "两张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id, template_id=tpl_id,
                                  reverse="true"))
    assert pages == [[5, 6], [3, 4], [1, 2]]


def test_顺序优化时凭证数不成对就留空(client, auth, book, periods, make_voucher):
    """5 张凭证：最后一格空着，不拿别的凭证来凑。"""
    _make_vouchers(make_voucher, 5)
    tpl_id = _template(client, auth, book, 2, "两张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id,
                                  template_id=tpl_id, print_order=PRINT_ORDER_OPTIMIZED))
    assert pages == [[1, 4], [2, 5], [3]]


def test_倒序时空格落到先印的那张纸上(client, auth, book, periods, make_voucher):
    """5 张凭证倒着印：空格跟着配对走，落在第 1 张纸上。"""
    _make_vouchers(make_voucher, 5)
    tpl_id = _template(client, auth, book, 2, "两张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id, template_id=tpl_id,
                                  print_order=PRINT_ORDER_OPTIMIZED, reverse="true"))
    assert pages == [[3], [2, 5], [1, 4]]


def test_倒着印时整叠反过来(client, auth, book, periods, make_voucher):
    """一页一张时倒序打印：最后一张凭证先印。"""
    _make_vouchers(make_voucher, 6)
    tpl_id = _template(client, auth, book, 1, "一张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id,
                                  template_id=tpl_id, reverse="true"))
    assert pages == [[6], [5], [4], [3], [2], [1]]


def test_一张一张时顺序优化等于原顺序(client, auth, book, periods, make_voucher):
    """一页一张没有两半可裁，错位配对在这里是空操作。"""
    _make_vouchers(make_voucher, 6)
    tpl_id = _template(client, auth, book, 1, "一张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id,
                                  template_id=tpl_id, print_order=PRINT_ORDER_OPTIMIZED))
    assert pages == [[1], [2], [3], [4], [5], [6]]


def test_不认识的顺序口径被拒(client, auth, book, periods, make_voucher):
    """这是个枚举，落到别的值上只能说明调用方写错了 —— 悄悄按原顺序印，
    会让人以为开关生效了。"""
    _make_vouchers(make_voucher, 2)
    r = client.get(f"/api/books/{book.id}/print/vouchers.html",
                   params={"period_id": periods[1].id, "print_order": "random"},
                   headers=auth)
    assert r.status_code == 422
    assert "打印顺序" in r.json()["detail"]


def test_超行凭证拆开后按格重排仍能合回原顺序(client, auth, book, periods, make_voucher):
    """拆成多格的凭证是**按格**排的：裁开合并回去仍然是原来的顺序。

    三张凭证各 6 行（每张拆成 2 片）= 6 格 = 3 张 A4。错位配对后印成
    1+2 / 1+3 / 2+3 —— 同一张凭证的两片分到了两张纸上，但裁开之后
    上堆是 1、1、2，下堆是 2、3、3，合起来仍是 1、1、2、2、3、3。
    """
    _make_vouchers(make_voucher, 3, per=3)
    tpl_id = _template(client, auth, book, 2, "两张一张")
    pages = _page_numbers(_print(client, auth, book, periods[1].id,
                                  template_id=tpl_id, print_order=PRINT_ORDER_OPTIMIZED))
    assert pages == [[1, 2], [1, 3], [2, 3]]
    upper = [page[0] for page in pages]
    lower = [page[1] for page in pages]
    assert upper + lower == [1, 1, 2, 2, 3, 3]
