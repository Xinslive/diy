-- =====================================================================
-- 会计护栏：全部落在数据库层，不依赖应用代码和前端按钮。
-- 四条核心不变量：
--   1. 借方合计 = 贷方合计
--   2. 已结账期间禁止任何凭证写入（INSERT / UPDATE / DELETE）
--   3. 凭证号在同一期间内唯一
--   4. 已被使用的科目不允许改编码 / 类别 / 余额方向
--   5. 分录金额不得为 0（负数合法：红字冲销）
-- =====================================================================

-- ---------- 基础约束 ----------
-- 金额允许为负：红字冲销。一张红字凭证借贷两边都是负的（-300 / -300），
-- 它是平的、也该存 —— 冲掉一张记错的凭证本来就是记账的日常。
-- 真正要挡的是「一条金额为 0 的分录」：那是没填，不是记了 0 块。
ALTER TABLE entry   DROP CONSTRAINT IF EXISTS ck_entry_amount_positive;
ALTER TABLE entry   DROP CONSTRAINT IF EXISTS ck_entry_amount_nonzero;
ALTER TABLE entry   ADD  CONSTRAINT ck_entry_amount_nonzero CHECK (amount <> 0);
ALTER TABLE entry   DROP CONSTRAINT IF EXISTS ck_entry_direction;
ALTER TABLE entry   ADD  CONSTRAINT ck_entry_direction CHECK (direction IN ('DEBIT','CREDIT'));
ALTER TABLE voucher DROP CONSTRAINT IF EXISTS ck_voucher_status;
ALTER TABLE voucher ADD  CONSTRAINT ck_voucher_status CHECK (status IN ('UNPOSTED','POSTED'));
ALTER TABLE voucher DROP CONSTRAINT IF EXISTS ck_voucher_source;
ALTER TABLE voucher ADD  CONSTRAINT ck_voucher_source CHECK (source IN ('MANUAL','IMPORT','CARRY_FORWARD'));

-- ---------- 1. 分录冗余列同步 ----------
-- entry.book_id / entry.period_ord 由凭证派生，保证账簿聚合只需扫单表
--
-- **为什么触发的是「任何 UPDATE」而不是「UPDATE OF voucher_id」。**（审计 P2-05）
-- 旧写法只在 voucher_id 变化时重算，于是 `UPDATE entry SET book_id = 别的账套`
-- 这条直写 SQL 根本不触发：分录从此挂在一个与凭证无关的账套上，账簿按 book_id
-- 聚合时会把它算进另一个单位，而凭证页面又照常显示 —— 两边数字对不上，
-- 且没有任何报错。冗余列只要有第二条写入路径，就一定会被漂移；唯一的解法是
-- 让触发器对**每一次** UPDATE 都无条件重算（重算是幂等的，多算一次的代价是
-- 一次主键 join，而漏算的代价是账错）。
--
-- 反过来说，这也让「冗余列只由本触发器维护」成为库级不变量：绕过触发器
-- 唯一的办法是关掉它，而关掉它需要 DDL 权限。
CREATE OR REPLACE FUNCTION fn_entry_sync() RETURNS trigger AS $$
DECLARE
    v_book_id bigint;
    v_ord     integer;
BEGIN
    SELECT v.book_id, p.ord INTO v_book_id, v_ord
    FROM voucher v JOIN period p ON p.id = v.period_id
    WHERE v.id = NEW.voucher_id;

    IF v_book_id IS NULL THEN
        RAISE EXCEPTION '凭证不存在: %', NEW.voucher_id USING ERRCODE = 'check_violation';
    END IF;

    NEW.book_id    := v_book_id;
    NEW.period_ord := v_ord;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_entry_sync ON entry;
CREATE TRIGGER trg_entry_sync BEFORE INSERT OR UPDATE ON entry
    FOR EACH ROW EXECUTE FUNCTION fn_entry_sync();

-- ---------- 1b. 凭证换期后回填分录冗余列 ----------
-- entry.book_id / entry.period_ord 是从 voucher 反规范化出来的冗余列，账簿聚合
-- 只扫 entry 单表。但 trg_entry_sync 只在「分录自己变了」时才触发，
-- 凭证被改日期换到另一个会计期间时它是不会响应的 —— 那样分录会留在旧期间的
-- ord 上，账簿就把金额算进错误的月份了，且不报任何错。
-- 所以这里在 voucher.period_id / book_id 变化时主动把该凭证的全部分录刷一遍。
--
-- 分录冗余列**两个方向都要有触发器**（本项目一贯的做法，见上）：
-- 少了这一条，「凭证改月不算账」；少了上一条，「分录改挂别的账套」。
CREATE OR REPLACE FUNCTION fn_voucher_resync_entries() RETURNS trigger AS $$
DECLARE
    v_ord integer;
BEGIN
    IF NEW.period_id IS DISTINCT FROM OLD.period_id
       OR NEW.book_id   IS DISTINCT FROM OLD.book_id THEN
        SELECT p.ord INTO v_ord FROM period p WHERE p.id = NEW.period_id;
        IF v_ord IS NULL THEN
            RAISE EXCEPTION '会计期间不存在: %', NEW.period_id USING ERRCODE = 'check_violation';
        END IF;
        UPDATE entry
           SET book_id = NEW.book_id, period_ord = v_ord
         WHERE voucher_id = NEW.id;
    END IF;
    RETURN NULL;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voucher_resync ON voucher;
CREATE TRIGGER trg_voucher_resync AFTER UPDATE OF period_id, book_id ON voucher
    FOR EACH ROW EXECUTE FUNCTION fn_voucher_resync_entries();

-- ---------- 2. 已结账期间禁写（分录层） ----------
-- **为什么必须加 FOR SHARE（审计 P1-2）。**
--
-- 裸 SELECT 在 READ COMMITTED 下读到的是**快照**，不是「此刻的事实」。于是：
--   T1  close_period 持 period 的 FOR UPDATE，准备置 locked=True
--   T2  INSERT voucher：裸 SELECT 读到快照里的 locked=False → 放行
--       → 外键检查才去取 period 的 FOR KEY SHARE，与 T1 的 FOR UPDATE 冲突 → 阻塞
--   T1  提交（locked=True 已落库）
--   T2  解阻塞、继续、落库 —— **已结账期间里凭空多了一张凭证**。
--
-- 那张凭证是 UNPOSTED：记不了账（一切记账路径都要求前置期间已结账、而后置未结账），
-- 默认口径账簿只算 POSTED，于是它的金额对所有余额和报表**隐形**，
-- 账面平衡却看不出来 —— 这是最贵的一种脏数据：存在，但没人知道它存在。
--
-- 改用 `SELECT ... FOR SHARE` 后，T2 在 BEFORE 触发器里就要拿 period 的共享锁，
-- 与 T1 的排他锁直接冲突 → T2 阻塞在**守卫里**，等 T1 提交后重新求值，
-- 这次看到的就是 locked=True，守卫拒绝。没有第二条写穿路径。
--
-- **锁顺序验证（已实测，见 tests/test_migration_guardrails.py::test_结账与建凭证并发…）。**
--   * 写侧：INSERT voucher → BEFORE 守卫先锁 period(共享)，再取 voucher 行锁
--           （外键的 FOR KEY SHARE 在 AFTER 阶段才发生），所以写侧恒为
--           「period → voucher」。
--   * 结账侧：close_period 先 `SELECT ... FOR UPDATE` 整条期间链，
--           再自动记账去 UPDATE voucher，所以结账侧也是「period → voucher」。
--   * 两侧同序，且 FOR SHARE 只与 FOR UPDATE / FOR NO KEY UPDATE 冲突，
--     与外键那把 FOR KEY SHARE 兼容 —— 因此本改动**没有引入新的死锁边**。
--     （UPDATE voucher 时行锁先于触发器拿到，是「voucher → period」，
--     但这条边在改动前就由外键的 FOR KEY SHARE 存在，不是新增的。）
CREATE OR REPLACE FUNCTION fn_entry_lock_guard() RETURNS trigger AS $$
DECLARE
    v_voucher_id bigint;
    v_locked     boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_voucher_id := OLD.voucher_id;
    ELSIF TG_OP = 'UPDATE' THEN
        -- 修改时新旧凭证都要检查（跨期移动凭证的场景）。
        -- 先锁住旧期间再看 locked：读到的值必须是「锁拿到之后」的，
        -- 与下面 NEW 那一段用的是同一种读法，两处不能一个锁一个不锁。
        SELECT p.locked INTO v_locked
          FROM period p
          JOIN voucher v ON v.id = OLD.voucher_id
         WHERE p.id = v.period_id
           FOR SHARE OF p;
        IF COALESCE(v_locked, false) THEN
            RAISE EXCEPTION '会计期间已结账，禁止修改凭证 (voucher_id=%)', OLD.voucher_id
                USING ERRCODE = 'check_violation';
        END IF;
        v_voucher_id := NEW.voucher_id;
    ELSE
        v_voucher_id := NEW.voucher_id;
    END IF;

    SELECT p.locked INTO v_locked
      FROM period p
      JOIN voucher v ON v.period_id = p.id
     WHERE v.id = v_voucher_id
       FOR SHARE OF p;

    IF COALESCE(v_locked, false) THEN
        RAISE EXCEPTION '会计期间已结账，禁止修改凭证 (voucher_id=%)', v_voucher_id
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_entry_lock_guard ON entry;
CREATE TRIGGER trg_entry_lock_guard BEFORE INSERT OR UPDATE OR DELETE ON entry
    FOR EACH ROW EXECUTE FUNCTION fn_entry_lock_guard();

-- ---------- 2b. 已记账凭证锁定 ----------
-- 「已记账」意味着这张凭证的每一笔分录已经被总账、科目余额表、报表引用。
-- 事后直接改它，报表里的数和凭证里写的数就对不上，而且没有任何痕迹说明
-- 差在哪一步 —— 这是会计系统最贵的一类脏数据。所以已记账凭证必须先反记账。
--
-- 例外只有两处，都是系统内部动作：
--   1) 记账 / 反记账这一次状态跳转
--   2) 「整理凭证」重编号（只动 voucher_no，不碰账内数字）
--   3) 结转凭证（CARRY_FORWARD）由系统生成，「重新结转损益」要能整张重做
CREATE OR REPLACE FUNCTION fn_posted_voucher_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        -- 唯一合法的状态跳转：未记账 → 已记账，及它的反向
        IF NEW.status IS DISTINCT FROM OLD.status THEN
            IF NOT ((OLD.status = 'UNPOSTED' AND NEW.status = 'POSTED')
                 OR (OLD.status = 'POSTED'   AND NEW.status = 'UNPOSTED')) THEN
                RAISE EXCEPTION '凭证 % 的状态只能由未记账改已记账（或反向），不能直接改写', OLD.voucher_no
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END IF;

        -- 只改凭证号：整理凭证在重编号，不影响任何账内数字，放行
        IF NEW.voucher_no IS DISTINCT FROM OLD.voucher_no
           AND NEW.voucher_date  IS NOT DISTINCT FROM OLD.voucher_date
           AND NEW.period_id     IS NOT DISTINCT FROM OLD.period_id
           AND NEW.source        IS NOT DISTINCT FROM OLD.source
           AND NEW.remark        IS NOT DISTINCT FROM OLD.remark
           AND NEW.attachments   IS NOT DISTINCT FROM OLD.attachments
           AND NEW.created_by    IS NOT DISTINCT FROM OLD.created_by
           AND NEW.posted_by     IS NOT DISTINCT FROM OLD.posted_by
           AND NEW.posted_at     IS NOT DISTINCT FROM OLD.posted_at THEN
            RETURN NEW;
        END IF;
    END IF;

    -- 结转凭证是系统自己生成的，「重新结转损益」允许整张删掉重做
    IF OLD.source = 'CARRY_FORWARD' THEN
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'POSTED' THEN
        RAISE EXCEPTION '凭证 % 已记账，不能修改或删除；如需改动请先反记账', OLD.voucher_no
            USING ERRCODE = 'check_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_posted_voucher_guard ON voucher;
CREATE TRIGGER trg_posted_voucher_guard BEFORE UPDATE OR DELETE ON voucher
    FOR EACH ROW EXECUTE FUNCTION fn_posted_voucher_guard();

-- ---------- 3. 借贷平衡（延迟到事务提交时校验） ----------
CREATE OR REPLACE FUNCTION fn_check_voucher_balance() RETURNS trigger AS $$
DECLARE
    v_voucher_id bigint;
    v_debit  numeric;
    v_credit numeric;
BEGIN
    IF TG_TABLE_NAME = 'voucher' THEN
        -- 空凭证检查：触发器挂在 voucher 上
        v_voucher_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
    ELSIF TG_OP = 'DELETE' THEN
        v_voucher_id := OLD.voucher_id;
    ELSE
        v_voucher_id := NEW.voucher_id;
    END IF;

    SELECT COALESCE(SUM(amount) FILTER (WHERE direction = 'DEBIT'),  0),
           COALESCE(SUM(amount) FILTER (WHERE direction = 'CREDIT'), 0)
      INTO v_debit, v_credit
      FROM entry WHERE voucher_id = v_voucher_id;

    IF v_debit <> v_credit THEN
        RAISE EXCEPTION '凭证 % 借贷不平衡：借方 % / 贷方 %', v_voucher_id, v_debit, v_credit
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_entry_balance ON entry;
CREATE CONSTRAINT TRIGGER trg_entry_balance
    AFTER INSERT OR UPDATE OR DELETE ON entry
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_voucher_balance();

-- 空凭证（一条分录都没有）同样要拦住
DROP TRIGGER IF EXISTS trg_voucher_balance ON voucher;
CREATE CONSTRAINT TRIGGER trg_voucher_balance
    AFTER INSERT ON voucher
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_check_voucher_balance();

-- ---------- 4. 凭证层：期间锁 + 日期与期间一致性 ----------
-- FOR SHARE 的理由与死锁分析见上面「2. 已结账期间禁写（分录层）」那段，
-- 三处守卫（fn_voucher_guard / fn_voucher_delete_guard / fn_entry_lock_guard）
-- 必须用**同一种**读法：任何一处退回裸 SELECT，它就重新变成一条绕过路径。
CREATE OR REPLACE FUNCTION fn_voucher_guard() RETURNS trigger AS $$
DECLARE
    v_locked boolean;
    v_year   int;
    v_month  int;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        SELECT p.locked INTO v_locked FROM period p WHERE p.id = OLD.period_id FOR SHARE;
        IF COALESCE(v_locked, false) THEN
            RAISE EXCEPTION '会计期间已结账，禁止修改凭证' USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    SELECT p.locked, p.year, p.month INTO v_locked, v_year, v_month
    FROM period p WHERE p.id = NEW.period_id FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION '会计期间不存在: %', NEW.period_id USING ERRCODE = 'check_violation';
    END IF;
    IF v_locked THEN
        RAISE EXCEPTION '会计期间已结账，禁止新增凭证' USING ERRCODE = 'check_violation';
    END IF;
    IF EXTRACT(YEAR  FROM NEW.voucher_date)::int <> v_year
    OR EXTRACT(MONTH FROM NEW.voucher_date)::int <> v_month THEN
        RAISE EXCEPTION '凭证日期 % 与所属会计期间 (% / %) 不一致', NEW.voucher_date, v_year, v_month
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voucher_guard ON voucher;
CREATE TRIGGER trg_voucher_guard BEFORE INSERT OR UPDATE ON voucher
    FOR EACH ROW EXECUTE FUNCTION fn_voucher_guard();

CREATE OR REPLACE FUNCTION fn_voucher_delete_guard() RETURNS trigger AS $$
DECLARE v_locked boolean;
BEGIN
    SELECT p.locked INTO v_locked FROM period p WHERE p.id = OLD.period_id FOR SHARE;
    IF COALESCE(v_locked, false) THEN
        RAISE EXCEPTION '会计期间已结账，禁止删除凭证' USING ERRCODE = 'check_violation';
    END IF;
    RETURN OLD;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voucher_delete_guard ON voucher;
CREATE TRIGGER trg_voucher_delete_guard BEFORE DELETE ON voucher
    FOR EACH ROW EXECUTE FUNCTION fn_voucher_delete_guard();

-- ---------- 5. 科目不可变性 ----------
CREATE OR REPLACE FUNCTION fn_subject_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE'
       AND (NEW.code IS DISTINCT FROM OLD.code
         OR NEW.direction IS DISTINCT FROM OLD.direction
         OR NEW.category IS DISTINCT FROM OLD.category) THEN
        IF EXISTS (SELECT 1 FROM entry WHERE subject_id = OLD.id) THEN
            RAISE EXCEPTION '科目 % 已被凭证使用，不能修改编码/类别/余额方向', OLD.code
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_subject_guard ON subject;
CREATE TRIGGER trg_subject_guard BEFORE UPDATE ON subject
    FOR EACH ROW EXECUTE FUNCTION fn_subject_guard();

-- ---------- 5b. is_leaf 必须与「有没有下级」一致（审计 P1-01 / P2-04）----------
-- is_leaf 是派生字段，但它决定了分录能挂在哪里：
-- 凭证校验和期初写入都拿它当权威判据。一旦它被写成与实际子级关系矛盾的值，
-- 汇总科目就会被当成末级科目，分录挂上去之后明细账里这笔钱凭空消失。
--
-- 危险方向只有一个：**is_leaf = true 却真的有下级**。
-- 反方向（is_leaf = false 但没有下级）不挡 —— 那只会让这个科目暂时录不了
-- 分录，界面上表现为「不能选」，绝不会产生错账；而把它也挡掉的话，
-- 应用层每次挂子节点都必须同一条 UPDATE 里把父节点改对，少一个中间态就报错。
-- 拦「错账方向」而不拦「不方便方向」，是这套护栏一贯的分寸。
--
-- **旧写法漏了两处（审计 P2-04）。**
--   1. 只挂 ``BEFORE UPDATE``：插入一行时完全不检查。
--   2. 只挡 ``false -> true``：``true -> true``（改个名字、调下排序，
--      把「有下级却仍是末级」这个矛盾原样带着走一遍）照样放行。
--
-- 现在的判据从「某一次翻转」改成「**此刻** is_leaf 与子级关系是否自洽」：
-- 只要 NEW.is_leaf 为真、而库里确实有 parent_id = NEW.id 的行，就拒绝。
-- 触发时机也从「只看 UPDATE」扩到 ``BEFORE INSERT OR UPDATE``。
--
-- **库级兜底的边界（这一段是 P2-04 的另一半；上一批刻意没上，这一版上了）。**
-- 上面那条触发器只保证**被改的那一行**自洽。它挡不住下面这条路径：
--
--     INSERT INTO subject (..., parent_id = 100202, ...)   -- 插一个子科目
--     -- 父节点 100202 一行都不碰，它仍然是 is_leaf = true
--
-- 此时「父节点有下级却仍是末级」这个矛盾确实产生了，而上面那条不会响
-- （它挂在子行上，子行自己 is_leaf=true 是对的）。补这一刀必须用一条
-- **延迟到提交时**的约束触发器去查父节点 —— 为什么非得是延迟的：
-- 真实的创建路径（api/subjects.py 的 _sync_leaf_flags / _recompute_subtree /
-- _recompute_root_leaf_flags）是「先插子行、再把父节点改成非末级」，同一事务内
-- **后**一步才能修好前一步造出的中间态。写成 BEFORE / IMMEDIATE 的话，
-- 创建接口自己先报错。延迟之后，「插子科目却不动父节点」在 COMMIT 上被拒。
--
-- 只查父节点的 is_leaf，**不查反向**（父没有下级却写着 is_leaf=false）：那个
-- 方向只会让科目暂时录不了分录，不会产生错账，与上面那条护栏一贯的分寸一致
-- （拦错账方向、不拦不方便方向）。也不检查跨账套挂父，那不是这条护栏的职责。
--
-- **上一批欠的账已经结清。** 那批写出了这条触发器却没上线，代价是若干既有夹具
-- 会红 —— 而其中 tests/test_ledger_openings_rollup.py 的 _add_child 自己的
-- docstring 写着「走真实的科目接口（父级会自动变成非末级）」，代码却是直接
-- db.add(child)，**文档与代码自相矛盾**。现已把这些夹具改成与它们宣称的一致：
-- 显式把父节点改成非末级，并照实改写 docstring（它们是在构造底层状态，
-- 不是在走接口）。库里不再留「明知是错、却当状态用」的构造。
--
-- 存量脏数据不追溯：已经错了的 is_leaf 需要应用层重算或人工修，
-- 触发器只保证它不会再被**改出来**。
CREATE OR REPLACE FUNCTION fn_subject_leaf_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.is_leaf AND EXISTS (SELECT 1 FROM subject c WHERE c.parent_id = NEW.id) THEN
        RAISE EXCEPTION
            '科目 % 有下级科目，is_leaf 必须是 false（is_leaf 由服务端按子级关系维护）',
            NEW.code USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_subject_leaf_guard ON subject;
CREATE TRIGGER trg_subject_leaf_guard BEFORE INSERT OR UPDATE ON subject
    FOR EACH ROW EXECUTE FUNCTION fn_subject_leaf_guard();

-- 父节点侧的同一条不变量，延迟到提交时判（理由见上面的注释）。
CREATE OR REPLACE FUNCTION fn_subject_leaf_parent_guard() RETURNS trigger AS $$
DECLARE
    v_leaf boolean;
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NULL;
    END IF;
    SELECT p.is_leaf INTO v_leaf FROM subject p WHERE p.id = NEW.parent_id;
    IF COALESCE(v_leaf, false) THEN
        RAISE EXCEPTION
            '父科目 % 仍是末级科目，不能挂下级（is_leaf 由服务端按子级关系维护）',
            NEW.parent_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END; $$ LANGUAGE plpgsql;

-- 本段幂等：先 DROP 同名触发器再 CREATE，重复重放 db_init.sql 不会报重名。
DROP TRIGGER IF EXISTS trg_subject_leaf_parent_guard ON subject;
CREATE CONSTRAINT TRIGGER trg_subject_leaf_parent_guard
    AFTER INSERT OR UPDATE OF parent_id ON subject
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_subject_leaf_parent_guard();

-- ---------- 6. 期初快照只读（除应用层显式写入外） ----------
CREATE OR REPLACE FUNCTION fn_opening_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION '期初快照不可直接修改，请通过反结账流程重算'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_opening_guard ON period_opening;
CREATE TRIGGER trg_opening_guard BEFORE UPDATE ON period_opening
    FOR EACH ROW EXECUTE FUNCTION fn_opening_guard();

-- =====================================================================
-- 迁移：取消「公司」实体，公司名下沉为账套属性
--
-- 公司表不持有任何账簿数据 —— 没有科目、没有凭证、没有期间、没有权限，
-- 唯一职责就是一个名字。把它建模成实体的代价是：建账套前必须先建公司、
-- 报表/打印再多一次外键查询、年度结转还要跨实体复制一条没有信息量的关联。
-- 账套本身才是会计上自洽的单元，公司名降级成 company_name 文本即可。
--
-- 本段幂等：新建的库跳过，老库执行一次，之后每次 init 都不再动。
-- =====================================================================
DO $$
BEGIN
    IF to_regclass('sys_company') IS NOT NULL THEN
        ALTER TABLE book ADD COLUMN IF NOT EXISTS company_name varchar(128);

        -- 先把公司名抄进账套，再拆外键
        UPDATE book b SET company_name = c.name
          FROM sys_company c
         WHERE c.id = b.company_id AND b.company_name IS NULL;

        ALTER TABLE book DROP CONSTRAINT IF EXISTS book_company_id_fkey;
        ALTER TABLE book DROP COLUMN IF EXISTS company_id;
        DROP TABLE IF EXISTS sys_company CASCADE;
    END IF;

    -- 账套唯一性：同一单位、同一账套名、同一年度只能有一个账套
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'book' AND column_name = 'company_name') THEN
        UPDATE book SET company_name = '未命名单位'
         WHERE company_name IS NULL OR btrim(company_name) = '';
        ALTER TABLE book ALTER COLUMN company_name SET NOT NULL;

        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_book') THEN
            ALTER TABLE book ADD CONSTRAINT uq_book UNIQUE (company_name, name, year);
        END IF;
    END IF;
END $$;

-- =====================================================================
-- 迁移：年度从「账套的身份」降级为「账套的属性」
--
-- 旧模型：book 表一行 = 一个「公司 + 账套名 + 年度」三元组，年度结转时
-- 整套复制出下一年的账套。结果是账套列表一年一行，同一家单位被拆成
-- 「租金账套037569」「租金账套037569 2027」两条，用户得自己记哪个在用。
--
-- 新模型：book 表一行 = 一个账套，start_year 是它的起始年度，
-- 会计期间（period）按 ord = (year - start_year) * 12 + month 跨年度连续铺开。
-- 科目表、权限、期初录入都只跟账套走，年度结转只是「追加下一年的 12 个期间」。
--
-- 本段幂等：新库跳过，老库执行一次。
-- =====================================================================
DO $$
DECLARE
    v_old_year boolean;
    v_dupes   integer;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'book' AND column_name = 'year') THEN
        ALTER TABLE book RENAME COLUMN year TO start_year;
    END IF;

    -- ord 现在要跨年度连续：单年度账套的 ord 本来就等于月份，重排是恒等变换；
    -- 已经被合并过的多年度账套会在合并脚本里算好，这里只处理单年度的
    UPDATE period p
       SET ord = (p.year - b.start_year) * 12 + p.month
      FROM book b
     WHERE b.id = p.book_id
       AND p.ord <> (p.year - b.start_year) * 12 + p.month;

    -- 唯一键去掉年度。老库里可能已经有「一个年度一个账套」留下的重复行，
    -- 这时不能建约束（否则服务起不来），只警告，等合并脚本跑完再建。
    SELECT count(*) INTO v_dupes FROM (
        SELECT company_name, name FROM book GROUP BY company_name, name HAVING count(*) > 1
    ) d;
    IF v_dupes = 0 THEN
        ALTER TABLE book DROP CONSTRAINT IF EXISTS uq_book;
        ALTER TABLE book ADD CONSTRAINT uq_book UNIQUE (company_name, name);
    ELSE
        RAISE WARNING
            '检测到 % 组同名账套（如「租金账套037569」与「租金账套037569 2027」是同一年代结转留下的）。'
            '请运行 python scripts/merge_book_years.py --apply 合并成「一个账套多个年度」。', v_dupes;
    END IF;
END $$;

ALTER TABLE book ALTER COLUMN start_year SET NOT NULL;

-- ---------- 迁移：管理员从「用户名叫 admin」改成独立字段 ----------
ALTER TABLE sys_user ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false;
UPDATE sys_user SET is_admin = true WHERE username = 'admin';

-- =====================================================================
-- 科目体系的两处补充（幂等）：账套的科目编码规则 + 科目的三个核算开关
-- =====================================================================
ALTER TABLE book    ADD COLUMN IF NOT EXISTS subject_code_rule varchar(32) NOT NULL DEFAULT '4-2-2-2';
ALTER TABLE subject ADD COLUMN IF NOT EXISTS qty_calc boolean NOT NULL DEFAULT false;
ALTER TABLE subject ADD COLUMN IF NOT EXISTS aux_calc boolean NOT NULL DEFAULT false;
ALTER TABLE subject ADD COLUMN IF NOT EXISTS fx_calc  boolean NOT NULL DEFAULT false;

UPDATE book SET subject_code_rule = '4-2-2-2'
 WHERE subject_code_rule IS NULL OR btrim(subject_code_rule) = '';

-- =====================================================================
-- 迁移：period_opening.subject_id 改成 RESTRICT（审计 P1-05）
--
-- 原来是 ON DELETE CASCADE。应用层删科目时只检查了 Entry 引用和下级科目，
-- 没检查期初，于是「只被期初引用、没有凭证引用」的科目一删，
-- 期初记录随之级联消失，余额凭空漂移（实测：2 条期初删成 1 条，0 → -100）。
-- 没有任何报错或审计线索 —— 属于静默数据损失。
--
-- 改完之后数据库层也兜底：即使将来有人绕过应用层直接删，
-- 有期初引用的科目删不掉。这是本项目护栏的一贯做法
-- （结账、凭证编辑、期初不可变都由触发器挡）。
--
-- 幂等：先删旧约束再加新的，跑第二遍结果一致。
-- 已有脏数据（期初引用了已不存在的科目）不会被这条 ALTER 影响 —— 外键
-- 从 CASCADE 改 RESTRICT 不校验存量行。
-- =====================================================================
DO $$
DECLARE
    v_old text;
BEGIN
    SELECT conname INTO v_old
      FROM pg_constraint
     WHERE conrelid = 'period_opening'::regclass
       AND contype = 'f'
       AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                            WHERE attrelid = 'period_opening'::regclass
                              AND attname = 'subject_id')];

    IF v_old IS NOT NULL THEN
        EXECUTE format('ALTER TABLE period_opening DROP CONSTRAINT %I', v_old);
    END IF;

    ALTER TABLE period_opening
        ADD CONSTRAINT fk_period_opening_subject
        FOREIGN KEY (subject_id) REFERENCES subject(id) ON DELETE RESTRICT;
END $$;

-- =====================================================================
-- 7. updated_at：触发器改写行时也要刷新（审计 P2-09）
--
-- ORM 侧靠 ``onupdate=func.now()`` 刷新，但那**只对 ORM 发出的 UPDATE 生效**。
-- 凡是数据库自己改写行的路径都绕过 ORM：
--   * fn_voucher_resync_entries —— 凭证换期时回填全部分录的 book_id/period_ord；
--   * merge_book_years.py 之类直接跑 SQL 的维护脚本；
--   * 任何「绕过 API 直写」的运维操作。
-- 这些路径下 updated_at 停在旧值，而「按时间戳取证」正是审计与排障的
-- 常用手段 —— 一行被改过的数据看上去却像从没动过。
--
-- 这里给全部带 TimestampMixin 的表挂同一条 BEFORE UPDATE 触发器，
-- 与 ORM 的 onupdate 语义一致：每次 UPDATE 都把 updated_at 推到
-- 本事务的时间戳。对 ORM 路径没有任何行为变化（它自己已经写了一遍，
-- 值相同），只是把「库写的」这一半也补上。
-- =====================================================================
CREATE OR REPLACE FUNCTION fn_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'book', 'period', 'period_opening', 'subject', 'voucher', 'entry',
        'sys_user', 'sys_user_book', 'import_batch',
        'print_template', 'report_template', 'report_result'
    ] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_updated_at ON %I', t);
        EXECUTE format(
            'CREATE TRIGGER trg_updated_at BEFORE UPDATE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()', t);
    END LOOP;
END $$;

-- =====================================================================
-- 8. sys_user_book.role 的取值必须受约束（审计 P2-12）
--
-- ``role`` 是权限判据，而判据散落各处：``core/deps.py`` 只挡字面量
-- ``"VIEWER"``、``api/reports.py`` 写 ``role != "VIEWER"``、
-- ``api/books.py`` 写 ``role != "OWNER"``。都是大小写敏感的**黑名单**判断 ——
-- 绕过 API 直接写进一个 ``'viewer'``，它既不等于 ``"VIEWER"``（放行）
-- 又不等于 ``"OWNER"``（某些接口也放行），于是一行本该被拒的账号拿到了
-- 完整写权限。fail-open 与这套系统一贯的 fail-closed 原则正好相反。
--
-- 取值集合与 ``app/api/system.py::ROLES`` **完全一致**
-- （OWNER / ACCOUNTANT / VIEWER）。改那边就要改这里，改了不同时改就会
-- 出现「库里进不去但代码以为能进」—— 这一条已点名给鉴权工作包：
-- 那边的 ``_check_roles`` 已经是精确集合比对，与本约束一致，无需改动。
--
-- 存量脏数据用 NOT VALID 处理：CHECK 约束对**新增/更新**的行照样生效
-- （fail-closed 走在正确的方向上），但不去回溯判死已经写坏的历史行，
-- 只发一条 WARNING 把它们列出来。否则这条迁移会在一个从没被约束过的
-- 生产库上直接失败 —— 那才是真正让「加护栏」这件事没法落地的原因。
-- =====================================================================
DO $$
DECLARE
    v_bad integer;
BEGIN
    SELECT count(*) INTO v_bad FROM sys_user_book
     WHERE role NOT IN ('OWNER', 'ACCOUNTANT', 'VIEWER');

    ALTER TABLE sys_user_book DROP CONSTRAINT IF EXISTS ck_sys_user_book_role;
    IF v_bad = 0 THEN
        ALTER TABLE sys_user_book
            ADD CONSTRAINT ck_sys_user_book_role
            CHECK (role IN ('OWNER', 'ACCOUNTANT', 'VIEWER'));
    ELSE
        ALTER TABLE sys_user_book
            ADD CONSTRAINT ck_sys_user_book_role
            CHECK (role IN ('OWNER', 'ACCOUNTANT', 'VIEWER')) NOT VALID;
        RAISE WARNING
            'sys_user_book.role 有 % 行不在 OWNER/ACCOUNTANT/VIEWER 之内，已按 NOT VALID 加约束：'
            '新写入会被拒绝，历史行暂时保留。请核对后修正（SELECT DISTINCT role FROM sys_user_book）。',
            v_bad;
    END IF;
END $$;

-- =====================================================================
-- 9. 结构护栏（审计 P1-10 / P2-05 / P2-08 / P2-10 / P1-5）
--
-- 这一段全是**结构**而不是行为，靠 pg_catalog 判断现状，做到重复执行结果一致。
-- 每一处改动都有对应的 migration（0005 / 0006），两边必须同步：
-- 新库走 0001 的这份 SQL，存量库走 0005/0006，两条路径留下的痕迹要长得一样。
-- =====================================================================

-- ---- 9a. entry.period_ord 收敛到 integer（审计 P2-08）----
-- 来源 period.ord 早就是 integer，这里还窄一档 —— 派生列与来源列类型不一致，
-- 就等于留了一个「理论上走不到」的溢出点，而这类化石一旦形成就再也查不清
-- 是哪一轮改动留下的（P3-01 的整个教训）。
DO $$
DECLARE
    v_type text;
BEGIN
    SELECT format_type(a.atttypid, a.atttypmod) INTO v_type
      FROM pg_attribute a
     WHERE a.attrelid = 'entry'::regclass AND a.attname = 'period_ord'
       AND a.attnum > 0 AND NOT a.attisdropped;
    IF v_type IS NOT NULL AND v_type IN ('smallint', 'int2') THEN
        ALTER TABLE entry ALTER COLUMN period_ord TYPE integer;
    END IF;
END $$;

-- ---- 9b. period 的 (book_id, ord) 唯一键 + entry 冗余列的组合外键（审计 P2-05）----
-- period_ord 是指向「某个账套的某个期间」的指针，两列合起来才是完整的指向。
-- PG 要求外键目标列有 UNIQUE/主键，所以先给 period 加 (book_id, ord) 唯一键。
-- 这条唯一键本来就是成立的（ord = (year - start_year) * 12 + month 唯一决定），
-- 加上去不会和存量数据冲突；它同时让 entry 的冗余列第一次能被库层钉住 ——
-- 此前 `UPDATE entry SET book_id = 别的账套` 完全合法且不报错。
DO $$
DECLARE
    v_bad integer;
    v_fk  text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'period'::regclass
                      AND conname = 'uq_period_book_ord') THEN
        SELECT count(*) INTO v_bad FROM (
            SELECT book_id, ord FROM period
             GROUP BY book_id, ord HAVING count(*) > 1
        ) d;
        IF v_bad > 0 THEN
            RAISE EXCEPTION
                'period 里有 % 组 (book_id, ord) 重复，无法建立 uq_period_book_ord。'
                'ord 由 (year - start_year) * 12 + month 决定，出现重复说明 ord 已经漂了，'
                '必须先人工核对 —— 不要直接删重复行，那会改动已结账期间的历史。', v_bad;
        END IF;
        ALTER TABLE period ADD CONSTRAINT uq_period_book_ord UNIQUE (book_id, ord);
    END IF;

    -- 按**列**找现存外键再删，而不是按名字：0003 的幂等性漏洞（P3-2）就在这里 ——
    -- 「约束同名但删除动作不同」时它会因为名字相同而不删、紧接着 ADD 同名约束，
    -- 于是 upgrade 直接失败。这里按列找、按名字删，对任何状态都收敛。
    SELECT c.conname INTO v_fk
      FROM pg_constraint c
      JOIN pg_attribute a
        ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
       AND a.attname = 'book_id' AND a.attnum > 0 AND NOT a.attisdropped
     WHERE c.conrelid = 'entry'::regclass AND c.contype = 'f'
     LIMIT 1;
    IF v_fk IS DISTINCT FROM 'fk_entry_book_period' THEN
        IF v_fk IS NOT NULL THEN
            EXECUTE format('ALTER TABLE entry DROP CONSTRAINT %I', v_fk);
        END IF;
        ALTER TABLE entry
            ADD CONSTRAINT fk_entry_book_period
            FOREIGN KEY (book_id, period_ord) REFERENCES period(book_id, ord)
            ON DELETE CASCADE;
    END IF;
END $$;

-- ---- 9c. 业务默认值补上 server_default（审计 P2-10）----
-- 原来这些默认值只写在 ORM 的 ``default=`` 里。绕过 API 的写入路径
-- （导入脚本、维护脚本、手工修数）拿不到 ORM 侧默认值，NOT NULL 直接失败，
-- 而失败点离「少了这个默认」很远。server_default 让「这一列有业务默认值」
-- 成为库里的事实，和模型不再分家。
--
-- 逐列 ADD/ALTER SET DEFAULT 都是元数据操作，PG 11+ 不重写表。
-- 0004 给 sys_user.token_version 加完又撤掉 server_default 是**另一回事**：
-- 那是一个安全计数器，宁可让人少看见一次变更；这里是一组业务常量，
-- 拿不到就会写不进去。
DO $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('subject',   'level',          '1'),
        ('subject',   'direction',      '''DEBIT'''),
        ('subject',   'is_leaf',        'true'),
        ('subject',   'carry_forward',  'false'),
        ('subject',   'qty_calc',       'false'),
        ('subject',   'aux_calc',       'false'),
        ('subject',   'fx_calc',        'false'),
        ('subject',   'sort_no',        '0'),
        ('subject',   'active',         'true'),
        ('subject',   'ext_attrs',      '''{}''::jsonb'),
        ('voucher',   'status',         '''UNPOSTED'''),
        ('voucher',   'source',         '''MANUAL'''),
        ('voucher',   'attachments',    '0'),
        ('voucher',   'remark',         ''''''),
        ('voucher',   'ext_attrs',      '''{}''::jsonb'),
        ('period',    'locked',         'false'),
        ('period',    'version',        '1'),
        ('period_opening', 'amount',    '0'),
        ('entry',     'summary',        ''''''),
        ('entry',     'aux_ref',        '''{}''::jsonb'),
        ('book',      'start_month',    '1'),
        ('book',      'subject_code_rule', '''4-2-2-2'''),
        ('book',      'status',         '''ACTIVE'''),
        ('print_template', 'paper',    '''A4'''),
        ('print_template', 'is_default', 'false'),
        ('print_template', 'remark',   '''''')
    ) AS t(tbl, col, val) LOOP
        EXECUTE format('ALTER TABLE %I ALTER COLUMN %I SET DEFAULT %s',
                       r.tbl, r.col, r.val);
    END LOOP;
END $$;

-- ---- 9d. print_template 归属账套（审计 P1-5 的根治项）----
-- 模板过去是全局的，而取默认模板时任何账套（含只读账号的浏览器）都会拿到它。
-- 加上 book_id 之后门禁可以回到「该账套的角色」。
--
-- **存量模板一律留 NULL，不回填。** NULL 的语义是「全局兜底」：
-- 回填到任意一个账套等于替人断言归属，而归属猜错的后果是别人的账套
-- 突然没有自己的模板。留 NULL 由应用层兜底，既不丢数据也不假装隔离。
--
-- 因此这条落地**不等于**门禁可以放宽 —— ``api/printing.py`` 还没按账套取模板，
-- 在那之前 ``_require_global_template_write`` 的 OWNER 级门禁仍是唯一的一道。
--
-- 这里用 pg_attribute 而不是 information_schema.columns：**后者列出的是当前
-- 用户可见的**全部 schema 里的同名表**，不看 search_path。于是在一个「临时
-- schema 当测试库」的场景里，它会先看见 public.print_template（那张没有
-- book_id），从而误判「已经有这一列了」而跳过 ADD COLUMN，紧接着去给临时
-- schema 里那张真的没有 book_id 的表加外键 ——
-- ``column "book_id" referenced in foreign key constraint does not exist``。
-- ``to_regclass`` 走 search_path，解析到的就是本库这一张表。实测踩过。
DO $$
BEGIN
    IF to_regclass('print_template') IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM pg_attribute
                        WHERE attrelid = 'print_template'::regclass
                          AND attname = 'book_id'
                          AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE print_template ADD COLUMN book_id integer;
    END IF;

    IF to_regclass('print_template') IS NOT NULL
       AND EXISTS (SELECT 1 FROM pg_attribute
                    WHERE attrelid = 'print_template'::regclass
                      AND attname = 'book_id'
                      AND attnum > 0 AND NOT attisdropped) THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                        WHERE conrelid = 'print_template'::regclass
                          AND conname = 'fk_print_template_book') THEN
            ALTER TABLE print_template
                ADD CONSTRAINT fk_print_template_book
                FOREIGN KEY (book_id) REFERENCES book(id) ON DELETE CASCADE;
        END IF;
        CREATE INDEX IF NOT EXISTS ix_print_template_book_scope
            ON print_template (book_id, scope);
    END IF;
END $$;

-- =====================================================================
-- 迁移：取消「公司」实体，公司名下沉为账套属性
--
-- 公司表不持有任何账簿数据 —— 没有科目、没有凭证、没有期间、没有权限，
-- 唯一职责就是一个名字。把它建模成实体的代价是：建账套前必须先建公司、
-- 报表/打印再多一次外键查询、年度结转还要跨实体复制一条没有信息量的关联。
-- 账套本身才是会计上自洽的单元，公司名降级成 company_name 文本即可。
--
-- 本段幂等：新建的库跳过，老库执行一次，之后每次 init 都不再动。
-- =====================================================================
DO $$
BEGIN
    IF to_regclass('sys_company') IS NOT NULL THEN
        ALTER TABLE book ADD COLUMN IF NOT EXISTS company_name varchar(128);

        -- 先把公司名抄进账套，再拆外键
        UPDATE book b SET company_name = c.name
          FROM sys_company c
         WHERE c.id = b.company_id AND b.company_name IS NULL;

        ALTER TABLE book DROP CONSTRAINT IF EXISTS book_company_id_fkey;
        ALTER TABLE book DROP COLUMN IF EXISTS company_id;
        DROP TABLE IF EXISTS sys_company CASCADE;
    END IF;

    -- 账套唯一性：同一单位、同一账套名、同一年度只能有一个账套
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'book' AND column_name = 'company_name') THEN
        UPDATE book SET company_name = '未命名单位'
         WHERE company_name IS NULL OR btrim(company_name) = '';
        ALTER TABLE book ALTER COLUMN company_name SET NOT NULL;

        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_book') THEN
            ALTER TABLE book ADD CONSTRAINT uq_book UNIQUE (company_name, name, year);
        END IF;
    END IF;
END $$;

-- =====================================================================
-- 迁移：年度从「账套的身份」降级为「账套的属性」
--
-- 旧模型：book 表一行 = 一个「公司 + 账套名 + 年度」三元组，年度结转时
-- 整套复制出下一年的账套。结果是账套列表一年一行，同一家单位被拆成
-- 「租金账套037569」「租金账套037569 2027」两条，用户得自己记哪个在用。
--
-- 新模型：book 表一行 = 一个账套，start_year 是它的起始年度，
-- 会计期间（period）按 ord = (year - start_year) * 12 + month 跨年度连续铺开。
-- 科目表、权限、期初录入都只跟账套走，年度结转只是「追加下一年的 12 个期间」。
--
-- 本段幂等：新库跳过，老库执行一次。
-- =====================================================================
DO $$
DECLARE
    v_old_year boolean;
    v_dupes   integer;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'book' AND column_name = 'year') THEN
        ALTER TABLE book RENAME COLUMN year TO start_year;
    END IF;

    -- ord 现在要跨年度连续：单年度账套的 ord 本来就等于月份，重排是恒等变换；
    -- 已经被合并过的多年度账套会在合并脚本里算好，这里只处理单年度的
    UPDATE period p
       SET ord = (p.year - b.start_year) * 12 + p.month
      FROM book b
     WHERE b.id = p.book_id
       AND p.ord <> (p.year - b.start_year) * 12 + p.month;

    -- 唯一键去掉年度。老库里可能已经有「一个年度一个账套」留下的重复行，
    -- 这时不能建约束（否则服务起不来），只警告，等合并脚本跑完再建。
    SELECT count(*) INTO v_dupes FROM (
        SELECT company_name, name FROM book GROUP BY company_name, name HAVING count(*) > 1
    ) d;
    IF v_dupes = 0 THEN
        ALTER TABLE book DROP CONSTRAINT IF EXISTS uq_book;
        ALTER TABLE book ADD CONSTRAINT uq_book UNIQUE (company_name, name);
    ELSE
        RAISE WARNING
            '检测到 % 组同名账套（如「租金账套037569」与「租金账套037569 2027」是同一年代结转留下的）。'
            '请运行 python scripts/merge_book_years.py --apply 合并成「一个账套多个年度」。', v_dupes;
    END IF;
END $$;

ALTER TABLE book ALTER COLUMN start_year SET NOT NULL;

-- ---------- 迁移：管理员从「用户名叫 admin」改成独立字段 ----------
ALTER TABLE sys_user ADD COLUMN IF NOT EXISTS is_admin boolean NOT NULL DEFAULT false;
UPDATE sys_user SET is_admin = true WHERE username = 'admin';

-- =====================================================================
-- 科目体系的两处补充（幂等）：账套的科目编码规则 + 科目的三个核算开关
-- =====================================================================
ALTER TABLE book    ADD COLUMN IF NOT EXISTS subject_code_rule varchar(32) NOT NULL DEFAULT '4-2-2-2';
ALTER TABLE subject ADD COLUMN IF NOT EXISTS qty_calc boolean NOT NULL DEFAULT false;
ALTER TABLE subject ADD COLUMN IF NOT EXISTS aux_calc boolean NOT NULL DEFAULT false;
ALTER TABLE subject ADD COLUMN IF NOT EXISTS fx_calc  boolean NOT NULL DEFAULT false;

UPDATE book SET subject_code_rule = '4-2-2-2'
 WHERE subject_code_rule IS NULL OR btrim(subject_code_rule) = '';

-- =====================================================================
-- 迁移：period_opening.subject_id 改成 RESTRICT（审计 P1-05）
--
-- 原来是 ON DELETE CASCADE。应用层删科目时只检查了 Entry 引用和下级科目，
-- 没检查期初，于是「只被期初引用、没有凭证引用」的科目一删，
-- 期初记录随之级联消失，余额凭空漂移（实测：2 条期初删成 1 条，0 → -100）。
-- 没有任何报错或审计线索 —— 属于静默数据损失。
--
-- 改完之后数据库层也兜底：即使将来有人绕过应用层直接删，
-- 有期初引用的科目删不掉。这是本项目护栏的一贯做法
-- （结账、凭证编辑、期初不可变都由触发器挡）。
--
-- 幂等：先删旧约束再加新的，跑第二遍结果一致。
-- 已有脏数据（期初引用了已不存在的科目）不会被这条 ALTER 影响 —— 外键
-- 从 CASCADE 改 RESTRICT 不校验存量行。
-- =====================================================================
DO $$
DECLARE
    v_old text;
BEGIN
    SELECT conname INTO v_old
      FROM pg_constraint
     WHERE conrelid = 'period_opening'::regclass
       AND contype = 'f'
       AND conkey = ARRAY[(SELECT attnum FROM pg_attribute
                            WHERE attrelid = 'period_opening'::regclass
                              AND attname = 'subject_id')];

    IF v_old IS NOT NULL THEN
        EXECUTE format('ALTER TABLE period_opening DROP CONSTRAINT %I', v_old);
    END IF;

    ALTER TABLE period_opening
        ADD CONSTRAINT fk_period_opening_subject
        FOREIGN KEY (subject_id) REFERENCES subject(id) ON DELETE RESTRICT;
END $$;
