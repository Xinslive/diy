"""打印模板的**按用户私有副本**（写时复制，粒度到人）（2026-10）

Revision ID: 0023_print_template_private_copy
Revises: 0022_voucher_baseline_templates
Create Date: 2026-10-17

**要解决的问题。** 0022 把两份凭证版式作为**系统基线**落了库，人人可见。
需求是「用户也要可改」，但改完不能影响基线、不能影响别人 —— 于是只能有两条路：
锁成只读（0020 报表基线那种），或者给改动一个**只属于改动者**的落点。
选后者，因为它同时满足「可改」和「不影响别人」；前者会把「基线」降格成一张
谁都不敢碰的样张。

**为什么是「按用户」而不是「按账套」。** 账套里不止一个人，凭证版式恰恰是每
个人自己的事：谁打谁的凭证，谁调谁的 A5 纸。按账套 fork 的话张三改版式、李四
明天打印出来的凭证也跟着变了，而李四从没同意过。所以粒度落到人。

**三层，互斥。** 加上 ``user_id`` 之后一张模板恰好处在一层：

===================  ==========================================  ==========
层                   判定                                      谁看得见
===================  ==========================================  ==========
系统基线             ``book_id IS NULL AND user_id IS NULL``    所有人
账套模板             ``book_id = X AND user_id IS NULL``        账套 X 的人
个人私有副本         ``book_id IS NULL AND user_id = 我``       只有我
===================  ==========================================  ==========

私有副本**不占 book_id**：它是「我这个人打印凭证的习惯」，换一本账也得跟着我，
不是某本账的私产。正因如此下面那条 CHECK 才敢写成硬约束 —— 两层同时占着就说明
写入路径写错了，宁可插入失败也不要留一张归属含混的模板（那种行在可见性判定里
会落进两个分支，取默认模板时就会随机挑中一份）。

``forked_from_id`` 记的是「这份是从哪份基线 fork 的」。它不是为了溯源好看，
是为了两件真事：**列表去重**（我 fork 过的那份基线就别再摆出来了，摆出来就是
「两份同名」让人猜哪份在用）与 **fork 幂等**（第二次改的还是同一份，不越改越多）。
靠 name 去重不行：名字是用户自己可改的，改完就对不上了。

**downgrade 先删私有副本。** 见下面 ``downgrade()`` 里的注释：列一撤，这些
行就会掉进「全局基线」那一层去。

**回填。** 存量行的 ``user_id`` 留 NULL —— 迁移之前的模板没有「属于谁」这个
事实，回填到任意一个用户等于替人断言归属。它们继续按原来的两层语义工作。
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_print_template_private_copy"
down_revision = "0022_voucher_baseline_templates"
branch_labels = None
depends_on = None


def upgrade():
    # **裸 SQL 而不是 op.add_column**：这条 revision 会被重放 ——
    # tests/test_migration_guardrails.py 把存量库 stamp 回早期版本再跑整条链，
    # 而那条链经过的 schema 是**按模型建的**（列已经在），op.add_column 到这儿
    # 就是 DuplicateColumn。形状照抄 0020（report_template 加 book_id 那条）。
    op.execute("ALTER TABLE print_template ADD COLUMN IF NOT EXISTS user_id INTEGER")
    # user_id 可空：NULL 不是缺值，是「这一行属于基线或账套，不属于个人」。
    # ondelete CASCADE：账号没了，它那份私有版式留着也没人认领。
    op.execute("ALTER TABLE print_template ADD COLUMN IF NOT EXISTS forked_from_id INTEGER")
    # 基线被删时置 NULL：这份副本降级成一份普通的个人版式，仍然只有本人可见。
    # 用 SET NULL 而不是 CASCADE：别人的私产不该因为我删了个基线就没了。
    _ensure_fk("user_id", "fk_print_template_user_id", "sys_user (id)", "CASCADE")
    _ensure_fk("forked_from_id", "fk_print_template_forked_from",
               "print_template (id)", "SET NULL")

    # 可见性判定与「我 fork 过哪几份」都是按 (user_id, scope) 查的。
    op.execute("CREATE INDEX IF NOT EXISTS ix_print_template_user_scope "
               "ON print_template (user_id, scope)")
    # 兜底基线仍按 (scope, name, book_id IS NULL) 查存在性（0022 的 upgrade），
    # 这条索引让它别退化成顺序扫。
    op.execute("CREATE INDEX IF NOT EXISTS ix_print_template_scope_name "
               "ON print_template (scope, name)")
    # 同一个人对同一份基线只许有一份副本。应用里已经先查后插（见
    # ``_fork_private``），这一条是那道检查的**背书**：真撞上并发时宁可让
    # 第二个请求报错，也不要攒出两份一模一样的私有副本 —— 那两份会让
    # 「默认」标记与取用顺序开始依赖 id 大小，而用户完全看不出来。
    # NULL 不参与唯一约束（PG 默认语义），基线与账套模板那几行不受影响。
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_print_template_user_fork "
               "ON print_template (user_id, forked_from_id)")

    # 两层不许同时占着。PG 没有 ADD CONSTRAINT IF NOT EXISTS，先问再决定。
    # 判据是「同名且定义一致」：重放时同名但内容不同，说明库里那份是别的东西，
    # 得换成这条要的，而不是跳过去把错的留下（P3-2 的教训：「在」不等于「对」）。
    op.execute(
        """
        DO $$
        DECLARE
            v_def text;
        BEGIN
            SELECT pg_get_constraintdef(oid) INTO v_def
              FROM pg_constraint
             WHERE conrelid = 'print_template'::regclass
               AND conname = 'ck_print_template_one_owner';

            IF v_def IS DISTINCT FROM
               'CHECK ((NOT ((book_id IS NOT NULL) AND (user_id IS NOT NULL))))' THEN
                IF v_def IS NOT NULL THEN
                    ALTER TABLE print_template DROP CONSTRAINT ck_print_template_one_owner;
                END IF;
                ALTER TABLE print_template
                    ADD CONSTRAINT ck_print_template_one_owner
                    CHECK (NOT (book_id IS NOT NULL AND user_id IS NOT NULL));
            END IF;
        END $$
        """
    )


def _ensure_fk(column: str, name: str, references: str, on_delete: str) -> None:
    """「这一列上的外键是不是已经是我们要的那一个」——是就跳过，不是就换掉。

    写法照抄 0020（fk_report_template_book）与 ``db_init.sql`` 里「按列找外键」
    的那段 DO 块：PG 没有 ADD CONSTRAINT IF NOT EXISTS，裸写就是 DuplicateObject；
    而只判「有没有」不够，重放时若同名但删除动作不同，跳过就等于把错的留下。
    """
    # pg_constraint.confdeltype 存的是单字母码，不是 'CASCADE' 这样的字面量。
    # 写错的后果很安静：判据恒不相等，于是每次重放都「换」一次外键 ——
    # 第一次真换，后面全是 DROP + ADD 的空转。
    action_code = {"CASCADE": "c", "SET NULL": "n"}[on_delete]
    ref_table = references.split(" ", 1)[0]
    op.execute(
        f"""
        DO $$
        DECLARE
            v_fk_name   text;
            v_fk_action text;
            v_fk_ref    text;
        BEGIN
            SELECT c.conname, c.confdeltype::text,
                   (SELECT relname FROM pg_class WHERE oid = c.confrelid)
              INTO v_fk_name, v_fk_action, v_fk_ref
              FROM pg_constraint c
             WHERE c.conrelid = 'print_template'::regclass
               AND c.contype = 'f'
               AND c.conkey = ARRAY[(SELECT attnum FROM pg_attribute
                                    WHERE attrelid = 'print_template'::regclass
                                      AND attname = '{column}')];

            IF v_fk_name IS DISTINCT FROM '{name}'
               OR v_fk_action IS DISTINCT FROM '{action_code}'
               OR v_fk_ref IS DISTINCT FROM '{ref_table}' THEN
                IF v_fk_name IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER TABLE print_template DROP CONSTRAINT %I', v_fk_name);
                END IF;
                EXECUTE format(
                    'ALTER TABLE print_template ADD CONSTRAINT %I '
                    'FOREIGN KEY ({column}) REFERENCES {references} '
                    'ON DELETE {on_delete}', '{name}');
            END IF;
        END $$
        """
    )


def downgrade():
    # **先删私有副本，再删列。** 顺序反了会出事：``user_id`` 一去，这些行就变成
    # 「book_id IS NULL 且不属于任何人」的**全局基线** —— 某个人的私有版式会被
    # 提升成人人可见的基线 —— 等于在降级时把「仅自己可见」反过来变成
    # 「所有人都看得见」。一次静默的越权。
    # 表结构既然要撤，这些行就没有能承载「属于谁」的列了；宁可少掉几份个人版式
    # （基线行一个不动，谁都还能看到出厂那份），也不能把它们发出去。
    private = op.get_bind().execute(
        sa.text("SELECT count(*) FROM print_template WHERE user_id IS NOT NULL")
    ).scalar()
    op.get_bind().execute(sa.text("DELETE FROM print_template WHERE user_id IS NOT NULL"))
    if private:
        print(f"0023 downgrade: 删掉 {private} 行私有副本（列要撤，归属无处安放）")

    op.execute("ALTER TABLE print_template DROP CONSTRAINT IF EXISTS ck_print_template_one_owner")
    op.execute("DROP INDEX IF EXISTS uq_print_template_user_fork")
    op.execute("DROP INDEX IF EXISTS ix_print_template_scope_name")
    op.execute("DROP INDEX IF EXISTS ix_print_template_user_scope")
    op.execute("ALTER TABLE print_template DROP CONSTRAINT IF EXISTS fk_print_template_forked_from")
    op.execute("ALTER TABLE print_template DROP CONSTRAINT IF EXISTS fk_print_template_user_id")
    op.execute("ALTER TABLE print_template DROP COLUMN IF EXISTS forked_from_id")
    op.execute("ALTER TABLE print_template DROP COLUMN IF EXISTS user_id")

