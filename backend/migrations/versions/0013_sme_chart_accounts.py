"""补齐《小企业会计准则》科目，让会小企01表每一行都挂得上科目

Revision ID: 0013_sme_chart_accounts
Revises: 0012_balance_sheet_dual_sided
Create Date: 2026-10-14

**要改的现象。** 0012 把资产负债表改成了《小企业会计准则》的「会小企01表」
（左右两栏、行次 1~53），但 **科目表是照《企业会计准则》建的**。两套标准不是
同一套编码，于是表上有 16 行挂不上任何科目，在资产负债格里永远是空的::

    应收股利 / 应收利息 / 周转材料 / 长期债券投资 / 工程物资 / 生产性生物资产
    应付利息 / 应付利润 / 递延收益 / 长期应付款

用户看到的不是「这个企业没有这项」，而是「系统不支持」—— 表单上明明印着行，
数字栏永远是空的。

**为什么补的是这些编码。** 全部取自财政部《小企业会计准则》附录「一、会计科目」
的正式编号：1131 应收股利、1132 应收利息、1411 周转材料、1501 长期债券投资、
1605 工程物资、1621/1622 生产性生物资产及其累计折旧、2231 应付利息、
2232 应付利润、2401 递延收益、2701 长期应付款。

**只补，不改。** 1101 交易性金融资产、1123 预付款项、1603 在建工程这些**已有编码
一律不动**：存量账套的凭证和期初余额都指着它们，改编码等于改历史账。代价是两套
编号会并存（1603 在建工程 与准则的 1604 在建工程同时存在），这是刻意的取舍 ——
**让老账算得对，比让新账符合某一版的编号表更要紧。**

**仍然留空的 6 行是有意的。** 其他流动资产、其他流动负债、其他非流动资产、
其他非流动负债、开发支出、在产品 —— 这几项在两套准则里都没有对应的资产/负债
一级科目；「在产品」取自成本类「生产成本」的期末余额，而本项目根本没有成本类科目。
《小企业会计准则》本身写明「小企业不存在的交易或者事项，可不设置相关会计科目」，
所以它们在表上保持空白，由财务在设计模式里挂自己建的科目。**不替他们编一个
不存在的标准编码。**

**为什么顺带重播资产负债表模板。** 新科目要挂到具体的行上（公式里出现
``科目("1131",…)``），而模板是数据不是 schema。``install_chart`` 对已存在的编码
是跳过的，所以这是纯增项；``install(db, codes=…)`` 只重播资产负债表，不动利润表。
"""
from __future__ import annotations

from alembic import op
from sqlalchemy import select

revision = "0013_sme_chart_accounts"
down_revision = "0012_balance_sheet_dual_sided"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.core.db import SessionLocal
    from app.models import Book
    from app.seed.builtin_reports import install
    from app.seed.chart_of_accounts import install_chart

    db = SessionLocal(bind=op.get_bind())
    try:
        # 存量账套补科目。install_chart 只在编码不存在时插入，
        # 所以已建过账的账套不会被改动，也不会碰到任何一条已有余额。
        books = db.scalars(select(Book.id)).all()
        added = 0
        for book_id in books:
            added += install_chart(db, book_id)
        # 模板里的公式要挂上新科目，所以重播一次资产负债表
        install(db, codes=("BALANCE_SHEET",))
        db.commit()
        print(f"补齐 {len(books)} 个账套的科目表，共新增 {added} 个科目")
    finally:
        db.close()


def downgrade() -> None:
    """不删科目。

    ``install_chart`` 是纯增量的，撤掉它等于让所有账套的科目表回到「少几个科目」
    的状态 —— 而那正是 0012 之后的状态：资产负债表上十行永远是空的。
    新科目零余额、零凭证，留在库里也没有任何副作用；真要删就手工删。
    """
    raise NotImplementedError(
        "新增科目为纯增量且零余额，请按需手工删除，不要用 downgrade 回退账套的科目表"
    )