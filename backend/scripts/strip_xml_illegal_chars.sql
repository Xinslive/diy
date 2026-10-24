-- 一次性数据订正：去掉文本里的 XML 1.0 非法字符（Excel 导不出）。
--
-- 背景：生产导出「管理费用明细账」（全部子科目、2026-01～2026-12，
-- 1934 行）直接提示「导出失败」。**与数据量无关** —— 1934 行远在
-- MAX_EXPORT_ROWS = 50_000 之下，同一份数据少一行也照样失败。
--
-- 真因：Excel 底层是 XML，lxml 遇到 XML 1.0 不允许的字符直接抛
--     ValueError: All strings must be XML compatible:
--     Unicode or ASCII, no NULL bytes or control characters
-- 抛在**写文件**的环节 —— 不是那一格没写上，是整个 xlsx 一个字节都生成不出来，
-- 接口 500，前端只显示「导出失败」。一行坏字符能拖垮一份导出件，
-- 不管那一份有多少行。
--
-- 坏字符从哪来：
--   * U+FFFE（19 条，全是发票生成的凭证）：发票 PDF 的文字层里混进了
--     UTF-16 的 BOM，pypdfium2 的 get_text_range() 把它漏成 U+FFFE。
--     例：「*电线电缆*广州五羊ZC<U+FFFE>BV2.5平方线」。
--   * U+007F（1 条，手工凭证）：摘要末尾混进了 DEL 控制符。
--
-- 代码侧已经修好（backend/app/printing/excel.py 的 _scrub_workbook，
-- 以及 backend/app/invoice/text_parser.py 的源头拦截），本脚本负责把
-- **已经存进库里的**脏数据清掉 —— 不然历史凭证永远导不出来。
--
-- 动之前先备份：
--   cd /www/wwwroot/acct.xins.live/backend
--   .venv/bin/python scripts/db_backup.py --out <备份文件>.sql
--
-- 动之前核过：
--   * 脏字符只落在 entry.summary 与 invoice_record.payload 两处；
--     voucher.remark / subject.name / book.name 全部干净（0 条）。
--   * 借贷金额一个字没动 —— 改完基线逐项一致，借贷不平仍是 0。
--   * 只改文本，不改任何 id / 金额 / 方向 / 期间。
--
-- 审计日志（audit_log）**不动**：那里记的是「当时发生了什么」的历史快照，
-- 改它等于篡改审计记录。
--
-- 执行于 2026-10-24（发布导出修复之后）。

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- 递归清洗 JSONB：所有字符串值都过一遍正则。
-- payload 里的脏字符不只在 resolution.entries[*].summary，还在 lines[*]
-- （发票解析出来的项目名）—— 两处都要清，所以不能只改一个路径。
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION pg_temp.scrub_xml_illegal(v jsonb) RETURNS jsonb AS $$
DECLARE
  arr  jsonb := '[]'::jsonb;
  obj  jsonb := '{}'::jsonb;
  item jsonb;
  k    text;
  i    int;
BEGIN
  CASE jsonb_typeof(v)
    WHEN 'string' THEN
      RETURN to_jsonb(regexp_replace(
        v #>> '{}',
        '[\U00000000-\U00000008\U0000000B\U0000000C\U0000000E-\U0000001F\U0000007F-\U0000009F\U0000FFFE\U0000FFFF]',
        '', 'g'));
    WHEN 'array' THEN
      IF jsonb_array_length(v) = 0 THEN
        RETURN '[]'::jsonb;
      END IF;
      FOR i IN 0 .. jsonb_array_length(v) - 1 LOOP
        arr := arr || jsonb_build_array(pg_temp.scrub_xml_illegal(v -> i));
      END LOOP;
      RETURN arr;
    WHEN 'object' THEN
      FOR k, item IN SELECT * FROM jsonb_each(v) LOOP
        obj := obj || jsonb_build_object(k, pg_temp.scrub_xml_illegal(item));
      END LOOP;
      RETURN obj;
    ELSE
      RETURN v;
  END CASE;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- 1) 凭证分录的摘要
UPDATE entry e
   SET summary = regexp_replace(
         e.summary,
         '[\U00000000-\U00000008\U0000000B\U0000000C\U0000000E-\U0000001F\U0000007F-\U0000009F\U0000FFFE\U0000FFFF]',
         '', 'g')
 WHERE e.summary ~ '[\U00000000-\U00000008\U0000000B\U0000000C\U0000000E-\U0000001F\U0000007F-\U0000009F\U0000FFFE\U0000FFFF]';

-- 2) 发票记录里冻结的解析结果与「凭证预览」
UPDATE invoice_record r
   SET payload = pg_temp.scrub_xml_illegal(r.payload)
 WHERE r.payload::text ~ '[\U00000000-\U00000008\U0000000B\U0000000C\U0000000E-\U0000001F\U0000007F-\U0000009F\U0000FFFE\U0000FFFF]';

-- ---- 事务内核对 ----
SELECT '还带非法字符的分录: ' || count(*) FROM entry
 WHERE summary ~ '[\U00000000-\U00000008\U0000000B\U0000000C\U0000000E-\U0000001F\U0000007F-\U0000009F\U0000FFFE\U0000FFFF]';
SELECT '还带非法字符的发票记录: ' || count(*) FROM invoice_record
 WHERE payload::text ~ '[\U00000000-\U00000008\U0000000B\U0000000C\U0000000E-\U0000001F\U0000007F-\U0000009F\U0000FFFE\U0000FFFF]';
SELECT '摘要变成空的分录: ' || count(*) FROM entry WHERE summary IS NULL OR btrim(summary) = '';
SELECT '分录总数: ' || count(*) FROM entry;
SELECT '借贷不平的凭证: ' || count(*) FROM (
         SELECT voucher_id FROM entry GROUP BY voucher_id
          HAVING abs(sum(amount) FILTER (WHERE direction='DEBIT')
                   - sum(amount) FILTER (WHERE direction='CREDIT')) > 0.005) x;
COMMIT;
