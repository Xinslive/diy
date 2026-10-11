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
CREATE OR REPLACE FUNCTION fn_entry_sync() RETURNS trigger AS $$
DECLARE
    v_book_id bigint;
    v_ord     smallint;
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
CREATE TRIGGER trg_entry_sync BEFORE INSERT OR UPDATE OF voucher_id ON entry
    FOR EACH ROW EXECUTE FUNCTION fn_entry_sync();

-- ---------- 1b. 凭证换期后回填分录冗余列 ----------
-- entry.book_id / entry.period_ord 是从 voucher 反规范化出来的冗余列，账簿聚合
-- 只扫 entry 单表。但 trg_entry_sync 只在「分录自己变了」时才触发，
-- 凭证被改日期换到另一个会计期间时它是不会响应的 —— 那样分录会留在旧期间的
-- ord 上，账簿就把金额算进错误的月份了，且不报任何错。
-- 所以这里在 voucher.period_id 变化时主动把该凭证的全部分录刷一遍。
CREATE OR REPLACE FUNCTION fn_voucher_resync_entries() RETURNS trigger AS $$
DECLARE
    v_ord smallint;
BEGIN
    IF NEW.period_id IS DISTINCT FROM OLD.period_id THEN
        SELECT p.ord INTO v_ord FROM period p WHERE p.id = NEW.period_id;
        IF v_ord IS NULL THEN
            RAISE EXCEPTION '会计期间不存在: %', NEW.period_id USING ERRCODE = 'check_violation';
        END IF;
        UPDATE entry
           SET book_id = NEW.book_id, period_ord = v_ord
         WHERE voucher_id = NEW.id;
    END IF;
    RETURN NEW;
END; $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_voucher_resync ON voucher;
CREATE TRIGGER trg_voucher_resync AFTER UPDATE OF period_id ON voucher
    FOR EACH ROW EXECUTE FUNCTION fn_voucher_resync_entries();

-- ---------- 2. 已结账期间禁写（分录层） ----------
CREATE OR REPLACE FUNCTION fn_entry_lock_guard() RETURNS trigger AS $$
DECLARE
    v_voucher_id bigint;
    v_locked     boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_voucher_id := OLD.voucher_id;
    ELSIF TG_OP = 'UPDATE' THEN
        -- 修改时新旧凭证都要检查（跨期移动凭证的场景）
        PERFORM 1 FROM period p
        WHERE p.locked AND p.id = (SELECT period_id FROM voucher WHERE id = OLD.voucher_id);
        IF FOUND THEN
            RAISE EXCEPTION '会计期间已结账，禁止修改凭证 (voucher_id=%)', OLD.voucher_id
                USING ERRCODE = 'check_violation';
        END IF;
        v_voucher_id := NEW.voucher_id;
    ELSE
        v_voucher_id := NEW.voucher_id;
    END IF;

    SELECT p.locked INTO v_locked FROM period p
    JOIN voucher v ON v.period_id = p.id
    WHERE v.id = v_voucher_id;

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
CREATE OR REPLACE FUNCTION fn_voucher_guard() RETURNS trigger AS $$
DECLARE
    v_locked boolean;
    v_year   int;
    v_month  int;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        SELECT p.locked INTO v_locked FROM period p WHERE p.id = OLD.period_id;
        IF COALESCE(v_locked, false) THEN
            RAISE EXCEPTION '会计期间已结账，禁止修改凭证' USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    SELECT p.locked, p.year, p.month INTO v_locked, v_year, v_month
    FROM period p WHERE p.id = NEW.period_id;

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
    SELECT p.locked INTO v_locked FROM period p WHERE p.id = OLD.period_id;
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
