"""模型层守卫：**任何**写入路径都过不掉脏字符。

**为什么在模型层，而不是在各个业务函数里各写一遍。**

脏字符（XML 1.0 非法字符，详见 ``app/core/text_clean.py``）的最后一道
防线是「导出前扫一遍」—— 那是在**读**的时候补救。问题是读的时候才知道
有脏字符，而脏字符是**写**的时候进去的：写进去的时候没人拦，它就躺在库
里，直到某天有人导出这份账表，整份文件一个字节都出不来
（生产事故：导出「管理费用明细账」1934 行直接失败）。

在业务函数里各写一遍会漏。写进库的路子有：手工记账、Excel 导入、发票
自动生成、拆分凭证、MCP 工具、插件、结转损益…… 漏一条就等于没做。

**模型层的 ``before_insert`` / ``before_update`` 事件是唯一收口**：
它们在 ORM 把行写进数据库**之前**触发，把值改掉。手工、导入、发票、
拆分、MCP —— 一视同仁，谁都绕不过去。

**范围**：只管**用户可填的自由文本**（摘要、备注、科目名、账套名、单位名）。
这些是脏字符的来源。科目编码、金额、日期不是文本，不在其中。

**已知边界**（写在这里，别当成没有）：

* 走 Core 层批量 ``update()`` 的写入**不会**触发这两个事件。本仓里
  这样的写入只有两处，写的都是 ``status`` / ``parent_id`` / ``locked``
  这类非文本列（见 ``accounting/book_deletion.py`` 与
  ``seed/chart_of_accounts.py``），不碰文本。
* 已经躺在库里的历史脏数据不会被这个守卫自动清掉（它只在**写**的时候
  生效）。历史脏数据由一次性订正脚本清（``backend/scripts/``），
  导出层还有一道兜底（``app/printing/excel.py`` 的 ``_scrub_workbook``）。
"""
from __future__ import annotations

from sqlalchemy import event

from app.core.text_clean import scrub_control_chars
from app.models.accounting import Entry, Subject, Voucher
from app.models.book import Book


def _scrub(target, attrs: tuple[str, ...]) -> None:
    """把 ``target`` 上 ``attrs`` 里的文本列洗一遍（原地改）。"""
    for attr in attrs:
        value = getattr(target, attr, None)
        if isinstance(value, str):
            cleaned = scrub_control_chars(value)
            if cleaned != value:
                setattr(target, attr, cleaned)


#: 分录摘要 —— 脏字符最常见的落点（发票 PDF 文字层漏出来的 UTF-16 BOM）。
@event.listens_for(Entry, "before_insert")
@event.listens_for(Entry, "before_update")
def _guard_entry_text(mapper, connection, target) -> None:
    _scrub(target, ("summary",))


#: 凭证备注。
@event.listens_for(Voucher, "before_insert")
@event.listens_for(Voucher, "before_update")
def _guard_voucher_text(mapper, connection, target) -> None:
    _scrub(target, ("remark",))


#: 科目名 —— 每一张明细账/总账的表头都印着它。
@event.listens_for(Subject, "before_insert")
@event.listens_for(Subject, "before_update")
def _guard_subject_text(mapper, connection, target) -> None:
    _scrub(target, ("name",))


#: 账套名与单位名 —— 每一份导出件的抬头都印着它们。
@event.listens_for(Book, "before_insert")
@event.listens_for(Book, "before_update")
def _guard_book_text(mapper, connection, target) -> None:
    _scrub(target, ("name", "company_name"))
