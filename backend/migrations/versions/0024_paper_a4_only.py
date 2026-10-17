"""打印纸型只保留 A4；「一页几张凭证」独立成模板字段（2026-10）

Revision ID: 0024_paper_a4_only
Revises: 0023_print_template_private_copy
Create Date: 2026-10-17

为什么有这次迁移
----------------
系统里原来只有 A4 与 A5 两种纸可供选择，「一页放几张凭证」是**由纸型推出来
的**：``voucher_slots_per_page()`` 判一下纸型是不是 ``TWO_UP_PAPERS``，是就 2、
否则 1。于是 A5 的全部意义就是「一张小纸上放一张」。

现在按要求：纸型只有 A4，界面上不再让人选；A4 要放一张还是两张，改由模板里的
``voucher.slots_per_page`` 自己说（见 ``template.py::validate_template``）。

只改代码不迁数据会怎样
----------------------
``validate_template`` 已经会把 ``paper`` 收敛成 A4。老模板（``paper: "A5"``）
经它一处理就变成 A4，于是 ``slots_per_page`` 只能取默认值 2 —— 一张**精心调好
的、一页一张**的凭证纸会**静默**变成一页两张。内容一个字节没动，印出来的张数
变了，而且没有任何报错。所以「A5 意味着一页一张」这个意图必须在覆盖 ``paper``
的**同一个迁移里**接过来。

改了什么
--------
对每一条 ``content->>'paper'`` 不是 'A4' 的模板：

* ``content.paper`` → ``'A4'``（``print_template.paper`` 那一列是保存时冗余的
  副本，一并跟上，否则它会继续撒谎说自己是 A5）；
* ``content.orientation`` → ``'portrait'``。A5 是「小横向卡片」，搬到 A4 上
  对应的是一张纵向整页 —— 这也是用户点名要的「A4 纵向，每页一张」。
  **例外**：纸名以 ``_LANDSCAPE`` 结尾的（``A4_LANDSCAPE``）保持 ``'landscape'``。
  那不是另一种纸，是同一张 A4 横着放，归一化成纵向会把用户调好的横排版式
  静默转 90°。规则与 ``template.py::validate_template`` 里的
  ``_forced`` 一致。
* 凭证范围且原来不是 two-up 纸型的，补上 ``voucher.slots_per_page = 1``。
  原来就是 A4 的**不写**：``validate_template`` 对它本来就取 2，写一遍只是
  多改一份用户的数据。

不碰的东西
----------
``margins`` / ``elements`` / ``columns`` / ``font`` 一个字节都不动 —— 用户在
设计器里调好的版式必须原样保留，包括那份基线的元素坐标。

downgrade
---------
不做数据回滚，**并且是有意的**：这里把「原来是什么纸型」压成了 A4，而那份信息
没有另外留档，硬要还原就只能靠猜（看到 ``slots_per_page = 1`` 就当成 A5？
可 A4 一页一张也是用户能自己选的）。猜错的后果是把用户**正常改出来的**版式
按错误的纸型改回去，比不回滚糟得多。
""" 

revision = "0024_paper_a4_only"
down_revision = "0023_print_template_private_copy"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa

#: 原来「一页两张」的纸型。与 template.py::TWO_UP_PAPERS 同一份名单，
#: 但这里是**迁移当时的快照** —— 迁移不能 import 业务代码（业务代码往后
#: 改了就该改历史数据，那不叫迁移）。名单本身也在 application.py 里断言过。
TWO_UP_PAPERS = ("A4", "A3", "A4_LANDSCAPE")

OLD_PAPERS = ("A5", "B5", "CONTINUOUS")


def upgrade():
    bind = op.get_bind()
    # 一次 UPDATE 里做完三件事，靠 CASE 判断，不依赖语句顺序 ——
    # 分成三条 UPDATE 的话，第二条就得先读到第一条改完的 paper。
    two_up = " OR ".join(f"content->>'paper' = '{p}'" for p in TWO_UP_PAPERS)
    bind.execute(
        sa.text(
            f"""
            UPDATE print_template
               SET content = content
                              || jsonb_build_object('paper', 'A4')
                              || jsonb_build_object(
                                   'orientation',
                                   CASE WHEN content->>'paper' LIKE '%\_LANDSCAPE'
                                        THEN 'landscape' ELSE 'portrait' END)
                              || CASE
                                   WHEN scope = 'VOUCHER'
                                    AND NOT ({two_up})
                                    AND NOT (content->'voucher' ? 'slots_per_page')
                                   THEN jsonb_build_object('voucher',
                                          jsonb_set(COALESCE(content->'voucher', '{{}}'::jsonb),
                                                    '{{slots_per_page}}', '1'::jsonb))
                                   ELSE '{{}}'::jsonb
                                 END,
                   paper = 'A4',
                   updated_at = now()
             WHERE content->>'paper' IS DISTINCT FROM 'A4'
               AND content ? 'paper'
            """
        )
    )
    print("0024: 打印纸型统一成 A4；A5 的「一页一张」搬进 voucher.slots_per_page")


def downgrade():
    # 见模块 docstring：数据不可逆，且刻意不可逆。
    print("0024: 数据不可逆（纸型与方向已收敛成 A4，不靠猜回滚）")
