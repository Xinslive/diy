"""账套磁贴的**个人**排序（2026-10）

Revision ID: 0019_book_order
Revises: 0018_book_initialization
Create Date: 2026-10-15

**为什么排在这一列上，而不是 book 表上。**

账套磁贴的先后是**每个人的工作台布局**，不是账套的属性。同一个账套，
甲把它排在第一个（天天用），乙根本不开它、希望它待在最后 ——
把顺序写进 ``book`` 表，等于让甲的顺手拖动去改乙的页面，而且谁拖的
根本查不出来（那一列没有 updated_by、没有审计，只有一个数字在变）。

排序是 **(谁, 哪个账套)** 这一对的关系属性，而这张关系表已经存在了：
``sys_user_book`` 就是「这个用户能看到哪些账套、各自什么角色」的授权行。
每本账套对这个用户都恰好有一行，所以加一列就够，不必新开一张表
（新表会带来自己的外键、级联和「授权撤了但排序行还在」的一类脏数据）。

**为什么允许 NULL。**

``sort_order`` 可空，且 NULL 一律排最后：用户从没拖过时全表都是 NULL，
列表维持原来的 ``company_name, name`` 排序 —— 也就是说这个特性对
不使用它的人是完全不可见的，而不是把所有人的磁贴重排一遍。
第一次拖动时前端会把**当前看到的全部**账套连同新顺序一起提交，
于是从那一刻起每一本都有明确序号。

以后新建的账套没有序号（建账时不写这一列），它就落在已排序的后面 ——
用户刚建完就被系统顶到最前面，会把精心排好的布局冲掉；落在末尾更符合
「加了一个新东西」的直觉。
"""
from __future__ import annotations

from alembic import op


revision = "0019_book_order"
down_revision = "0018_book_initialization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 裸 SQL 的 ADD COLUMN IF NOT EXISTS，而不是 op.add_column：
    # 这条 revision 会被**重放**（tests/test_migration_guardrails.py 把存量库
    # stamp 回早期版本再重跑整条链），到这儿表上已经有这一列时
    # op.add_column 就是 DuplicateColumn。形状照抄 0015 / 0018。
    #
    # 不回填：NULL 就是「没排过」，而存量账套的默认顺序是
    # company_name/name —— 凭空给它们编一个序号只会得到一份
    # 「谁排的、为什么这么排」都答不上来的顺序。
    op.execute("ALTER TABLE sys_user_book ADD COLUMN IF NOT EXISTS sort_order INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE sys_user_book DROP COLUMN IF EXISTS sort_order")
