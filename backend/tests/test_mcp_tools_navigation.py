"""MCP 导航类工具（账套 / 期间 / 科目 / 上下文）的用例。

**为什么测的是「口径」而不是「返回值」。**
这四个工具的返回体里没有金额，值不值得测全看它们会不会**说错话**：
年度混用、关键词漏命中、权限没拦住。这三件事都不会抛异常，只会让 agent
答出一个看着很正常的错答案 —— 所以断言都落在这三处，而不是「返回了东西」。

**怎么调工具。**
MCP 工具是同步函数，被 ``base.mcp_tool`` 包过一层：装饰器负责开数据库会话、
鉴权、写读取审计，并把异常翻成结构化错误。``tests/mcp_helpers.py`` 把
「拼一个真的 Context + 挂上主体」这件事收成一处，测试只管调。
"""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.models import Book, Subject, UserBook
from tests.mcp_helpers import call_tool, call_tool_expect_error, make_principal


# --------------------------------------------------------------------- 夹具

@pytest.fixture
def principal(admin_user):
    return make_principal(user_id=admin_user.id, username=admin_user.username)


@pytest.fixture
def other_principal(plain_user):
    """另一个用户的主体 —— 用来验「没有授权就拿不到账套」。"""
    return make_principal(user_id=plain_user.id, username=plain_user.username)


@pytest.fixture
def prior_year_book(db, admin_user):
    """一本**上一年度**的账。

    为什么要单独造一本：验「今年」不是服务器日历年，就必须有一本
    当前在记的年度 ≠ 服务器日历年的账。用 `date.today().year - 1` 而不是
    写死某个年份 —— 写死的那个一旦到了下一年，断言会自己塌掉。
    """
    from app.accounting.periods import create_periods
    from app.accounting.years import ensure_book_year
    from app.seed.chart_of_accounts import install_chart

    year = date.today().year - 1
    book = Book(company_name="往年公司", name="往年账套",
                start_year=year, start_month=1)
    db.add(book)
    db.flush()
    create_periods(db, book)
    install_chart(db, book.id)
    book_year = ensure_book_year(db, book, year)
    book_year.init_subjects_at = datetime.now(timezone.utc)
    book_year.init_subjects_source = "CHART"
    db.add(UserBook(user_id=admin_user.id, book_id=book.id, role="OWNER"))
    db.commit()
    db.refresh(book)
    return book


def _add_subject(db, book, code, name, *, category="PROFIT", direction="DEBIT",
                 year=None, active=True, level=2, is_leaf=True, parent_id=None):
    """往某年度的科目表里插一个科目。"""
    subject = Subject(
        book_id=book.id,
        year=year if year is not None else book.start_year,
        code=code, name=name, category=category, direction=direction,
        level=level, is_leaf=is_leaf, active=active, parent_id=parent_id,
    )
    db.add(subject)
    db.commit()
    db.refresh(subject)
    return subject


def _make_voucher(db, book, admin_user, when, dr, cr):
    """给任意一本账造一张凭证（不依赖 conftest 里绑死 ``book`` 的那个工厂）。"""
    from app.accounting.vouchers import create_voucher

    subjects = {
        s.code: s for s in db.scalars(
            select(Subject).where(Subject.book_id == book.id, Subject.year == when.year)
        ).all()
    }

    def _conv(rows, direction):
        out = []
        for code, amount, summary in rows:
            subject = subjects.get(code)
            assert subject is not None, f"测试科目不存在：{code}"
            out.append({"subject_id": subject.id, "amount": str(amount),
                        "summary": summary, "direction": direction})
        return out

    entries = _conv(dr, "DEBIT") + _conv(cr, "CREDIT")
    voucher = create_voucher(
        db, book_id=book.id, user=admin_user, voucher_date=when,
        entries=entries, source="MANUAL", remark="测试凭证", commit=False,
    )
    db.commit()
    return voucher


# --------------------------------------------------------------------- list_books

def test_list_books_returns_authorized_book_with_year_progress(principal, book):
    result = call_tool("list_books", principal)

    assert result["caliber"]
    assert result["notes"]
    rows = {row["id"]: row for row in result["items"]}
    assert book.id in rows

    row = rows[book.id]
    assert row["name"] == book.name
    assert row["company_name"] == book.company_name
    assert row["role"] == "OWNER"
    assert row["start_year"] == book.start_year
    assert row["latest_year"] == book.start_year
    assert row["years"] == [{
        "year": book.start_year, "total": 12, "closed": 0,
        "vouchers": 0, "carried": False,
    }]
    # init 是「这个账套当前在记那一年」的初始化进度，原样带出来。
    assert row["init"]["year"] == book.start_year
    assert row["init"]["subjects_ready"] is True


def test_list_books_hides_books_without_grant(principal, book, db, plain_user):
    """没授权的账套**不能出现**在列表里 —— 可见性 100% 来自授权表。"""
    other = Book(company_name="别家公司", name="别家账套",
                 start_year=2026, start_month=1)
    db.add(other)
    db.flush()
    db.add(UserBook(user_id=plain_user.id, book_id=other.id, role="OWNER"))
    db.commit()

    result = call_tool("list_books", principal)
    ids = [row["id"] for row in result["items"]]
    assert book.id in ids
    assert other.id not in ids


def test_list_books_counts_vouchers_per_year(principal, book, make_voucher):
    make_voucher("2026-03-10",
                 dr=[("1122", 100, "应收")], cr=[("1002", 100, "收款")])
    result = call_tool("list_books", principal)
    row = next(r for r in result["items"] if r["id"] == book.id)
    year = row["years"][0]
    assert year["vouchers"] == 1
    assert year["total"] == 12
    assert year["closed"] == 0
    assert year["carried"] is False


# --------------------------------------------------------------------- list_periods

def test_list_periods_lists_every_period(principal, book):
    result = call_tool("list_periods", principal, book_id=book.id)

    assert result["book_id"] == book.id
    assert result["caliber"]
    assert result["notes"]
    items = result["items"]
    assert len(items) == 12
    assert items[0]["code"] == f"{book.start_year}-01"
    assert items[-1]["code"] == f"{book.start_year}-12"
    assert [p["ord"] for p in items] == sorted(p["ord"] for p in items)
    first = items[0]
    assert first["locked"] is False
    assert first["closed_at"] is None
    assert first["closed_by"] is None
    assert first["vouchers"] == 0
    assert first["posted"] == 0
    assert first["unposted"] == 0


def test_list_periods_counts_vouchers_and_posted(principal, book, make_voucher):
    make_voucher("2026-05-10",
                 dr=[("1122", 100, "应收")], cr=[("1002", 100, "收款")])
    result = call_tool("list_periods", principal, book_id=book.id)
    row = next(p for p in result["items"] if p["code"] == f"{book.start_year}-05")
    assert row["vouchers"] == 1
    assert row["posted"] == 0
    assert row["unposted"] == 1


# --------------------------------------------------------------------- find_subjects

def test_find_subjects_returns_all_keyword_hits(principal, book, db):
    """关键词命中多个科目时**全部返回** —— 只回一个的话 agent 会漏掉
    「付出去的租金」和「收到的租金」是两张不同的科目。"""
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "5602"))
    assert parent is not None
    _add_subject(db, book, "560299", "场地租赁费", parent_id=parent.id)

    result = call_tool("find_subjects", principal, book_id=book.id, keyword="租赁")
    names = {item["name"] for item in result["items"]}
    assert names == {"房屋租赁费", "场地租赁费"}
    assert result["total"] == 2


def test_find_subjects_matches_code_as_well_as_name(principal, book):
    """关键词也要能命中**编码** —— agent 有时只知道「1002」。"""
    result = call_tool("find_subjects", principal, book_id=book.id, keyword="1002")
    assert [item["code"] for item in result["items"]] == ["1002"]


def test_find_subjects_keeps_years_apart(principal, book, db):
    """同一个编码在两个年度是两个 id。混用不会报错，只会静默返回空。"""
    from app.accounting.years import copy_chart

    copy_chart(db, book.id, book.start_year, book.start_year + 1)
    db.commit()

    first = call_tool("find_subjects", principal, book_id=book.id,
                      year=book.start_year, keyword="银行存款")
    second = call_tool("find_subjects", principal, book_id=book.id,
                       year=book.start_year + 1, keyword="银行存款")

    assert first["total"] == 1 and second["total"] == 1
    assert first["items"][0]["code"] == second["items"][0]["code"] == "1002"
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert first["items"][0]["year"] == book.start_year
    assert second["items"][0]["year"] == book.start_year + 1
    assert first["year"] == book.start_year
    assert second["year"] == book.start_year + 1


def test_find_subjects_defaults_to_current_accounting_year(principal, book):
    """不传 year 时取的是**这个账套当前在记的那一年**，并在 notes 里说明。"""
    result = call_tool("find_subjects", principal, book_id=book.id, keyword="银行存款")
    assert result["year"] == book.start_year
    assert result["items"][0]["year"] == book.start_year
    assert any(str(book.start_year) in note for note in result["notes"])


def test_find_subjects_applies_filters(principal, book, db):
    parent = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "5602"))
    _add_subject(db, book, "560299", "场地租赁费", parent_id=parent.id)

    # 类别
    assets = call_tool("find_subjects", principal, book_id=book.id, category="ASSET")
    assert assets["total"] > 0
    assert {i["category"] for i in assets["items"]} == {"ASSET"}
    assert {i["category_name"] for i in assets["items"]} == {"资产"}

    # 方向
    credit = call_tool("find_subjects", principal, book_id=book.id, direction="CREDIT")
    assert credit["total"] > 0
    assert {i["direction"] for i in credit["items"]} == {"CREDIT"}

    # 只要末级
    leaves = call_tool("find_subjects", principal, book_id=book.id, only_leaf=True)
    assert leaves["total"] > 0
    assert all(i["is_leaf"] for i in leaves["items"])
    all_rows = call_tool("find_subjects", principal, book_id=book.id)
    assert leaves["total"] < all_rows["total"], "标准科目表里应当有非末级科目"

    # 只要在用的
    target = db.scalar(select(Subject).where(
        Subject.book_id == book.id, Subject.year == book.start_year,
        Subject.code == "560207"))
    target.active = False
    db.commit()

    with_inactive = call_tool("find_subjects", principal, book_id=book.id,
                               keyword="房屋租赁费")
    assert with_inactive["total"] == 1
    only_active = call_tool("find_subjects", principal, book_id=book.id,
                            keyword="房屋租赁费", active_only=True)
    assert only_active["total"] == 0
    assert only_active["items"] == []


def test_find_subjects_paginates_and_clamps_page_size(principal, book):
    from app.core.config import settings

    first = call_tool("find_subjects", principal, book_id=book.id, page=1, page_size=5)
    assert first["page"] == 1
    assert first["page_size"] == 5
    assert len(first["items"]) == 5
    assert first["total"] > 5

    second = call_tool("find_subjects", principal, book_id=book.id, page=2, page_size=5)
    assert second["page"] == 2
    assert second["total"] == first["total"]
    assert [i["id"] for i in second["items"]] != [i["id"] for i in first["items"]]

    # 页长超限是被**夹住**，不是报错 —— agent 说了「我要更多」，不是「我做错了」。
    huge = call_tool("find_subjects", principal, book_id=book.id, page_size=100000)
    assert huge["page_size"] == settings.mcp_max_page_size
    assert huge["total"] == first["total"]


def test_find_subjects_rejects_unknown_category(principal, book):
    failure = call_tool_expect_error("find_subjects", principal,
                                     book_id=book.id, category="收入")
    assert failure.code == "bad_request"
    assert "ASSET" in failure.message


# --------------------------------------------------------------------- accounting_context

def test_accounting_context_reports_current_state(principal, book):
    result = call_tool("accounting_context", principal, book_id=book.id)

    assert result["caliber"]
    assert result["notes"]
    assert result["book"]["id"] == book.id
    assert result["book"]["name"] == book.name
    assert result["book"]["company_name"] == book.company_name
    assert result["book"]["start_year"] == book.start_year
    assert result["book"]["start_month"] == book.start_month
    assert result["book"]["subject_code_rule"] == book.subject_code_rule

    assert result["current_accounting_year"] == book.start_year
    assert [row["year"] for row in result["years"]] == [book.start_year]
    assert result["years"][0]["subject_count"] > 0
    assert result["years"][0]["vouchers"] == 0
    assert result["years"][0]["closed"] == 0

    assert result["subject_count"] > 0
    assert result["voucher_count"] == 0
    assert result["latest_closed_period"] is None


def test_accounting_context_has_current_period_without_vouchers(principal, book):
    """账上一张凭证都没有时，**当前期仍然有值**（取最后一个期间）。

    答「没有当前期」会让 agent 以为这本账不能用；它只是还没记第一张凭证。
    """
    result = call_tool("accounting_context", principal, book_id=book.id)
    assert result["current_period"] is not None
    assert result["current_period"]["code"] == f"{book.start_year}-12"
    assert result["current_period"]["locked"] is False
    assert result["voucher_count"] == 0


def test_accounting_context_uses_accounting_year_not_server_year(
        principal, prior_year_book, db, admin_user):
    """「今年」是这个账套当前在记的那一年，**不是服务器日历年**。"""
    year = prior_year_book.start_year
    assert year != date.today().year

    _make_voucher(db, prior_year_book, admin_user, date(year, 3, 10),
                  dr=[("1122", 100, "应收")], cr=[("1002", 100, "收款")])

    result = call_tool("accounting_context", principal, book_id=prior_year_book.id)
    assert result["current_accounting_year"] == year
    assert result["current_period"]["code"] == f"{year}-03"
    assert result["voucher_count"] == 1
    assert result["years"][0]["vouchers"] == 1
    assert result["years"][0]["year"] == year


def test_accounting_context_cash_subjects_only_cash(principal, book):
    result = call_tool("accounting_context", principal, book_id=book.id)
    codes = {item["code"] for item in result["cash_subjects"]}
    assert codes == {"1001", "1002", "1012"}
    assert all(item["code"].startswith(("1001", "1002", "1012"))
               for item in result["cash_subjects"])
    assert all(item["year"] == book.start_year
               for item in result["cash_subjects"])
    # 非现金科目不许混进来。
    assert "1122" not in codes


def test_accounting_context_counts_locked_periods(principal, book, db):
    from sqlalchemy import update

    from app.models import Period

    db.execute(
        update(Period)
        .where(Period.book_id == book.id, Period.month <= 3)
        .values(locked=True)
    )
    db.commit()

    result = call_tool("accounting_context", principal, book_id=book.id)
    assert result["years"][0]["closed"] == 3
    # 已结账的期间照读不误。
    periods = call_tool("list_periods", principal, book_id=book.id)
    assert sum(1 for p in periods["items"] if p["locked"]) == 3


# --------------------------------------------------------------------- 权限

@pytest.mark.parametrize("tool_name", ["list_periods", "find_subjects",
                                       "accounting_context"])
def test_book_tools_forbid_without_grant(other_principal, book, tool_name):
    """令牌绑定的用户没有这本账的授权 ⇒ 结构化 forbidden，**不是**异常、不是 500。"""
    failure = call_tool_expect_error(tool_name, other_principal, book_id=book.id)
    assert failure.code == "forbidden"
    assert failure.message == "没有该账套的访问权限"


def test_list_books_does_not_leak_other_books_names(other_principal, book, db,
                                                     admin_user):
    """别人的账套连名字都不该出现在 list_books 里。"""
    mine = Book(company_name="我的公司", name="我的账套",
                start_year=2026, start_month=1)
    db.add(mine)
    db.flush()
    db.add(UserBook(user_id=admin_user.id, book_id=mine.id, role="OWNER"))
    db.commit()

    result = call_tool("list_books", other_principal)
    names = [row["name"] for row in result["items"]]
    assert "我的账套" not in names
    assert book.name not in names
