-- 一次性数据订正：把凭证摘要末尾的「（含税）」标记去掉。
--
-- 背景：进项发票的税额并入第一条明细行时，系统曾经自动往那条摘要后面
-- 加一个「（含税）」。用户口径（2026-10-24）是**摘要就是发票上印的项目名**，
-- 不自动加任何系统标记。代码已改（backend/app/invoice/service.py 的
-- `_input_entries`），但**已经生成过的凭证**里，那条摘要当时就写进库了。
--
-- 本脚本把生产库里这些残留标记去掉，执行于 2026-10-24（发布 5a1e338 之后）：
--   * entry.summary            322 条（全部是发票生成的凭证；手工凭证不受影响）
--   * invoice_record.payload   432 条（已生成的记录里冻结的「凭证预览」，
--                               服务端对已入账的记录不重算，不改的话预览里
--                               还带着旧标记）
--
-- 动之前先备份：
--   cd /www/wwwroot/acct.xins.live/backend
--   .venv/bin/python scripts/db_backup.py --out <备份文件>.sql
--
-- 动之前核过：
--   * 这 322 条所在期间**都没结账**（已结账期间禁止改凭证，库里有触发器把关），
--     所以改得动，也不会违反「已结账期间不可记账」这条规矩。
--   * 只动发票生成的凭证（`EXISTS invoice_record`），手工/导入的凭证不碰。
--   * 借贷金额一个字没动 —— 改完基线逐项一致，借贷不平仍是 0。
--
-- 审计日志（audit_log）**不动**：那里记的是「当时发生了什么」的历史快照，
-- 改它等于篡改审计记录。旧标记留在历史里是对的。

\set ON_ERROR_STOP on
BEGIN;

-- 1) 凭证分录的摘要：去掉末尾的「（含税）」（4 个字符）
UPDATE entry e
   SET summary = left(e.summary, char_length(e.summary) - 4)
 WHERE e.summary LIKE '%（含税）'
   AND EXISTS (SELECT 1 FROM invoice_record r WHERE r.voucher_id = e.voucher_id);

-- 2) 发票记录里冻结的「凭证预览」：同一批分录的摘要也要一起去掉，
--    否则已生成的发票在预览里还带着旧标记。
UPDATE invoice_record r
   SET payload = jsonb_set(
         r.payload,
         '{resolution,entries}',
         coalesce((
           SELECT jsonb_agg(
                    CASE WHEN el->>'summary' LIKE '%（含税）'
                         THEN jsonb_set(el, '{summary}',
                                        to_jsonb(left(el->>'summary', char_length(el->>'summary') - 4)))
                         ELSE el END
                    ORDER BY ord)
             FROM jsonb_array_elements(coalesce(r.payload->'resolution'->'entries', '[]'::jsonb))
                    WITH ORDINALITY AS t(el, ord)
         ), '[]'::jsonb)
       )
 WHERE r.payload::text LIKE '%（含税）%';

-- ---- 事务内核对 ----
SELECT '还带（含税）的分录: ' || count(*) FROM entry WHERE summary LIKE '%（含税）';
SELECT '还带（含税）的发票记录: ' || count(*) FROM invoice_record WHERE payload::text LIKE '%（含税）';
SELECT '分录总数: ' || count(*) FROM entry;
SELECT '借贷不平的凭证: ' || count(*) FROM (
         SELECT voucher_id FROM entry GROUP BY voucher_id
          HAVING abs(sum(amount) FILTER (WHERE direction='DEBIT')
                   - sum(amount) FILTER (WHERE direction='CREDIT')) > 0.005) x;
SELECT '摘要变成空的分录: ' || count(*) FROM entry WHERE summary IS NULL OR btrim(summary) = '';
COMMIT;
