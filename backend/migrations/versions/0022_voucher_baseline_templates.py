"""凭证模板基线入库：凭证纸专用 + 基础模板（2026-10）

Revision ID: 0022_voucher_baseline_templates
Revises: 0021_youyou_chart_of_accounts
Create Date: 2026-10-17

**为什么要有这一条。** 打印模板一直是**纯用户数据** —— 表里没有种子，迁移链上
也没有任何一条去建凭证模板。于是「用哪份版式」这件事只存在于某一个库：换个环境
（另一台机器、生产的库）重新初始化，凭证版式就退回后端 ``DEFAULT_TEMPLATES``
那份硬编码内置版式，用户在设计器里调好的 A5 凭证纸一张也带不过去。

所以这两份是**基线**，不是某个人在某个库里的私产：``凭证纸专用``（A5，打印
凭证的日常版式，默认）与 ``基础模板``（A4）。它们以 ``book_id IS NULL``
落库，于是 ``_visible_templates()`` 对**每个**账套都可见 —— 新账套一建好就有
凭证版式可用，不用谁先去设计器里手点一遍。

**基线人人可改，但改动只属于改的那个人。** 这是这一条与 0020 报表模板的
**粒度**差别：报表模板是按**账套** fork，凭证模板是按**用户** fork
（``print_template.user_id``，迁移 0023）。基线本身对所有账套的所有人可见，
任何人改了它 —— 改动落成一份 ``user_id = 当前用户`` 的私有副本，**只有他自己
看得见**，基线一个字节都不动。别的人看到的还是出厂那份。

为什么不能像 0020 那样「按账套 fork」：账套里不止一个人。凭证版式恰恰是
**每个人自己的事** —— 谁打谁的凭证，谁调谁的 A5 纸，按账套 fork 意味着张三改
版式，李四明天打印出来的凭证也跟着变了，而李四从没同意过。所以粒度必须落到
人，不是落到账套。

为什么不干脆锁成只读（0020 报表基线那种）：两份都是用户亲手调出来的版式，
不给改就等于「系统基线」这件事只剩了一个空壳，用户真正的需求 —— 「我要按
我这台打印机微调一下」—— 一步都走不了。私有副本把「可改」和「不影响别人」
两件事分开了。

**只补缺，不覆盖。** 按 (scope, name) 判存在：已经有的模板原样留下，
content 与 is_default 都不动 —— 存量库里这两份可能是用户改过的，
迁移把它们按回出厂值就是一次静默丢数据。用户后来自己把默认改成别的，
也不在这里翻回去（那是用户的选择，不是基线该管的事）。

**「基础模板」的出厂内容刷新过一次（2026-10-30）。** 用户在模板设计器里把
这份调到了自己满意的样子，要求**仓库里的基线跟着换**（原话：「把本地开发库的
『基础摸板』最新的参数，更新进仓库，作为基线，替换现在的 tpl=2 的旧参数」）。
于是 ``CONTENT_PLAIN`` 换成用户调完那一份的 content —— 换掉的正是当初种下
**tpl=2** 的那份出厂内容（它在 2026-10-29 那次基线换人时被 tpl=8 顶掉了，
但仓库里这一份没跟着换）。

**这一条的「只补缺，不覆盖」在这里仍然成立。** 存量库里已经有「基础模板」的行
（本地是 tpl=8、生产是 tpl=9），upgrade 按 (scope, name, book_id IS NULL)
判存在，**照样整条跳过** —— 改这一份只影响**以后新建的库**，不会把用户改过的
版式按回出厂值。已经在跑的库由 ``backend/scripts/set_voucher_baseline.py
--sync-content`` 同步（幂等、默认 dry-run、``--apply`` 才写库），**仓库、
本地库、生产库是同一份**。

**downgrade 只删还是出厂内容的那一份。** 不是「按名字删」：同名的模板可能在
这次迁移之前就在库里（上面「只补缺」那种情况），那是用户的数据，删掉它就是
一次静默丢数据。所以删除条件里带上 ``content = 出厂 content``：被别人改过的
同名模板原样留下，只有仍与出厂一致的那一行被删。实测验证过：库里先塞一份被改
过的「凭证纸专用」，upgrade 只补另一份、downgrade 之后被改过的那份还在。
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "0022_voucher_baseline_templates"
down_revision = "0021_youyou_chart_of_accounts"
branch_labels = None
depends_on = None

#: 两份基线模板的 content，**原样贴着 ``json.dumps(spec['content'],
#: ensure_ascii=False, indent=2)`` 的输出**，不手工折行。
#:
#: 外层必须是 **raw 字符串**（r"""…"""）。凭证纸的页脚里有真实换行
#: （``{date}`` / ``本币：人民币  单位：元`` / ``{voucher_no}`` 三行），
#: ``json.dumps`` 把它写成两字符的 ``\n``；放进普通三引号字符串，Python 会
#: **先**把那两字符解释成真换行，于是 ``json.loads`` 读到的字符串值里带着
#: 裸控制字符，直接 ``json.LError: Invalid control character``。写成 r"""
#: 才轮到 json 去做这层解释，转义才对得上。
#:
#: 手工折行是同一个坑的另一种踩法：JSON 字符串值里不允许出现换行，
#: 一折就坏 —— 这也是这一段不能靠人肉排版的原因。

CONTENT_VOUCHER_PAPER = r"""
{
  "body": {
    "border": true,
    "repeat_header": true
  },
  "font": {
    "size": 11.0,
    "family": "仿宋"
  },
  "meta": {
    "book": true,
    "date": true,
    "period": true,
    "remark": true,
    "cashier": true,
    "checker": true,
    "company": true,
    "accountant": true,
    "voucher_no": true,
    "attachments": true
  },
  "show": {
    "date": true,
    "period": true,
    "remark": true,
    "page_no": true,
    "voucher_no": true,
    "attachments": true
  },
  "paper": "A5",
  "title": {
    "bold": true,
    "size": 14.0,
    "text": "记 账 凭 证",
    "align": "center"
  },
  "footer": {
    "text": "",
    "format": "第 {page} 页　共 {total} 页",
    "show_page_no": true
  },
  "columns": [
    {
      "key": "summary",
      "align": "left",
      "label": "摘要",
      "width": 22.2,
      "indent": 0
    },
    {
      "key": "subject",
      "align": "left",
      "label": "科目",
      "width": 47.4,
      "indent": 0
    },
    {
      "key": "debit",
      "align": "right",
      "label": "借方",
      "width": 15.2,
      "indent": 0
    },
    {
      "key": "credit",
      "align": "right",
      "label": "贷方",
      "width": 15.2,
      "indent": 0
    }
  ],
  "margins": {
    "top": 0,
    "left": 19.0,
    "right": 5.0,
    "bottom": 10.0
  },
  "voucher": {
    "fill_rows": true,
    "split_format": "{voucher_no} - {page}/{pages}",
    "split_number": true,
    "rows_per_page": 5
  },
  "elements": [
    {
      "h": 8.0,
      "w": 36.0,
      "x": 30.0,
      "y": 6.0,
      "id": "title",
      "bind": "title",
      "bold": true,
      "text": "记 账 凭 证",
      "type": "text",
      "align": "center",
      "border": true,
      "visible": true,
      "font_size": 22.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 5.0,
      "w": 32.0,
      "x": 13.4,
      "y": 14.0,
      "id": "company",
      "bind": "company",
      "bold": true,
      "text": "{company}",
      "type": "text",
      "align": "left",
      "border": true,
      "visible": true,
      "font_size": 11.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 15.9,
      "w": 22.1,
      "x": 77.9,
      "y": 7.5,
      "id": "voucher_no",
      "bind": "voucher_no",
      "bold": true,
      "text": "{date}\n本币：人民币  单位：元\n{voucher_no}",
      "type": "text",
      "align": "left",
      "border": true,
      "visible": true,
      "font_size": 10.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 53.9,
      "w": 94.0,
      "x": 3.0,
      "y": 29.0,
      "id": "table",
      "bind": "entries",
      "bold": false,
      "text": "分录表格",
      "type": "table",
      "align": "left",
      "border": false,
      "visible": true,
      "font_size": 9.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": false,
      "total_label": "",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 6.0,
      "w": 40.0,
      "x": 13.6,
      "y": 75.9,
      "id": "el-mv6pmpr2-0",
      "bind": "capital",
      "bold": true,
      "text": "{capital}",
      "type": "text",
      "align": "left",
      "border": true,
      "visible": true,
      "font_size": 11.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": true
    }
  ],
  "subtitle": {
    "bold": false,
    "size": 11.0,
    "text": "",
    "align": "center"
  },
  "orientation": "landscape"
}
"""

#: 「基础模板」的出厂 content —— **2026-10-30 换成用户在设计器里调过的那一版**
#: （用户 2026-10-30 批注：「把本地开发库的『基础摸板』最新的参数，更新进仓库，
#: 作为基线，替换现在的 tpl=2 的旧参数」）。
#:
#: 换掉的那一份就是当初种下 **tpl=2** 的那份出厂 content —— 它在 2026-10-29
#: 那次基线换人时被 tpl=8 顶掉了，但**仓库里这一份还是旧的**（新库建出来
#: 仍然是老参数）。现在仓库、本地库、生产库是同一份。
#:
#: 这一份的来源是**本地开发库 tpl=8 的 content 原样**（用户在模板设计器里
#: 调完保存的那一行），由序列化器 ``json.dumps(content, ensure_ascii=False,
#: indent=2)`` 原样输出，不手工折行 —— 与上面那份同一规矩。
#:
#: 与旧的那份相比只有三处实质变化：
#:   1. ``voucher.slots_per_page`` = 2（一页两张；旧的那份没有这个字段 = 一页一张）
#:   2. ``elements[capital]``（合计大写那一行）：文案、字重、坐标都调过了
#:   3. ``columns`` 每列多了 ``"vertical": false``（设计器现在把这个默认值写出来）
#: 其余逐字节相同。

CONTENT_PLAIN = r"""
{
  "body": {
    "border": true,
    "repeat_header": true
  },
  "font": {
    "size": 10.0,
    "family": "宋体"
  },
  "meta": {
    "book": true,
    "date": true,
    "period": true,
    "cashier": true,
    "checker": true,
    "company": true,
    "accountant": true,
    "voucher_no": true,
    "attachments": true
  },
  "show": {
    "date": true,
    "period": true,
    "remark": true,
    "page_no": true,
    "voucher_no": true,
    "attachments": true
  },
  "paper": "A4",
  "title": {
    "bold": true,
    "size": 18.0,
    "text": "记 账 凭 证",
    "align": "center"
  },
  "footer": {
    "text": "",
    "format": "第 {page} 页　共 {total} 页",
    "show_page_no": true
  },
  "columns": [
    {
      "key": "line_no",
      "align": "center",
      "label": "",
      "width": 5.8824,
      "indent": 0,
      "vertical": false
    },
    {
      "key": "summary",
      "align": "left",
      "label": "摘　要",
      "width": 30.5882,
      "indent": 0,
      "vertical": false
    },
    {
      "key": "subject",
      "align": "left",
      "label": "会计科目",
      "width": 30.5882,
      "indent": 0,
      "vertical": false
    },
    {
      "key": "debit",
      "align": "right",
      "label": "借方金额",
      "width": 16.4706,
      "indent": 0,
      "vertical": false
    },
    {
      "key": "credit",
      "align": "right",
      "label": "贷方金额",
      "width": 16.4706,
      "indent": 0,
      "vertical": false
    }
  ],
  "margins": {
    "top": 10.0,
    "left": 8.0,
    "right": 8.0,
    "bottom": 10.0
  },
  "voucher": {
    "fill_rows": true,
    "split_format": "{voucher_no} - {page}/{pages}",
    "split_number": true,
    "rows_per_page": 5,
    "slots_per_page": 2
  },
  "elements": [
    {
      "h": 8.0,
      "w": 36.0,
      "x": 32.0,
      "y": 3.0,
      "id": "title",
      "bind": "title",
      "bold": true,
      "text": "记 账 凭 证",
      "type": "text",
      "align": "center",
      "border": true,
      "visible": true,
      "font_size": 18.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 5.0,
      "w": 32.0,
      "x": 3.0,
      "y": 15.0,
      "id": "company",
      "bind": "company",
      "bold": false,
      "text": "单位：{company}",
      "type": "text",
      "align": "left",
      "border": true,
      "visible": true,
      "font_size": 9.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 5.0,
      "w": 31.0,
      "x": 66.0,
      "y": 12.0,
      "id": "voucher_no",
      "bind": "voucher_no",
      "bold": false,
      "text": "{voucher_no}",
      "type": "text",
      "align": "right",
      "border": true,
      "visible": true,
      "font_size": 9.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 5.0,
      "w": 31.0,
      "x": 66.0,
      "y": 17.0,
      "id": "date",
      "bind": "date",
      "bold": false,
      "text": "{date}",
      "type": "text",
      "align": "right",
      "border": true,
      "visible": true,
      "font_size": 9.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 57.0,
      "w": 94.0,
      "x": 3.0,
      "y": 23.0,
      "id": "table",
      "bind": "entries",
      "bold": false,
      "text": "分录表格",
      "type": "table",
      "align": "left",
      "border": true,
      "visible": true,
      "font_size": 9.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "",
      "header_height": 14.0,
      "last_page_only": false
    },
    {
      "h": 5.0,
      "w": 64.0,
      "x": 4.0,
      "y": 74.2,
      "id": "capital",
      "bind": "capital",
      "bold": true,
      "text": "合     计：{capital}",
      "type": "text",
      "align": "left",
      "border": true,
      "visible": true,
      "font_size": 9.0,
      "line_style": "solid",
      "line_width": 0.3,
      "show_header": true,
      "total_label": "合　计",
      "header_height": 14.0,
      "last_page_only": true
    }
  ],
  "subtitle": {
    "bold": false,
    "size": 11.0,
    "text": "",
    "align": "center"
  },
  "orientation": "portrait"
}
"""

BASELINE = [
    {"name": '凭证纸专用', "paper": 'A5', "is_default": True,
     "content": json.loads(CONTENT_VOUCHER_PAPER)},
    {"name": '基础模板', "paper": 'A4', "is_default": False,
     "content": json.loads(CONTENT_PLAIN)},
]


#: 基线模板一律全局（book_id IS NULL）且不属于任何个人（user_id IS NULL，
#: 见迁移 0023）：每个人、每个账套都看得见同一份，改它也不会影响别人 ——
#: 写路径会把改动落成一份只属于当前用户的私有副本。
GLOBAL_BOOK_ID = None


def upgrade():
    bind = op.get_bind()
    # 只按 (scope, name, book_id IS NULL) 判存在。**不掺 user_id**：
    # 这一条跑在 0023 之前，那时 ``user_id`` 这一列还不存在，SQL 里写它
    # 会直接 ``column "user_id" does not exist``。私有副本那一层在 0023
    # 之后才存在，而那时 0022 早已跑完，不存在「拿副本当基线」的时机。
    exists = sa.text(
        "SELECT 1 FROM print_template "
        "WHERE scope = 'VOUCHER' AND name = :name AND book_id IS NULL"
    )
    inserted = []
    for spec in BASELINE:
        # 已存在就整条跳过：content 与 is_default 都不碰。
        if bind.execute(exists, {"name": spec["name"]}).first():
            continue
        bind.execute(
            sa.text(
                "INSERT INTO print_template "
                "(book_id, scope, name, paper, content, is_default, remark, "
                " created_at, updated_at) "
                "VALUES (:book_id, 'VOUCHER', :name, :paper, CAST(:content AS jsonb), "
                " :is_default, '', now(), now())"
            ),
            {
                "book_id": GLOBAL_BOOK_ID,
                "name": spec["name"],
                "paper": spec["paper"],
                "content": json.dumps(spec["content"], ensure_ascii=False),
                "is_default": spec["is_default"],
            },
        )
        inserted.append(spec["name"])
    if inserted:
        print(f"0022: 补上凭证基线模板 {'、'.join(inserted)}")


def downgrade():
    bind = op.get_bind()
    for spec in BASELINE:
        bind.execute(
            sa.text(
                "DELETE FROM print_template "
                "WHERE scope = 'VOUCHER' AND name = :name AND book_id IS NULL "
                "AND content = CAST(:content AS jsonb)"
            ),
            {
                "name": spec["name"],
                # 出厂内容作为删除条件的一部分：只剩出厂内容的那一行才是
                # 「基线」，被人改过的那一份属于用户，不删。
                "content": json.dumps(spec["content"], ensure_ascii=False),
            },
        )
