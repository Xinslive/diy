# 工具速查（10 个）

每个工具都返回 `caliber` + `notes[]` + `currency`。凡取凭证金额的工具另有 `coverage`。
本文件只列**参数**与**关键返回字段**，口径语义见 SKILL.md。

所有工具的 `book_id` 都是必填，且只对令牌绑定用户有授权的账套可见。

---

## 导航组

### list_books

列出令牌用户**有授权**的账套。无参数。

返回 `items[]`（`BookSummary`）+ `total`：

| 字段 | 说明 |
|---|---|
| `id` / `name` / `company_name` | 账套 id、名称、公司名 |
| `role` | 当前用户在此账套的角色：`OWNER` / `ACCOUNTANT` / `VIEWER` |
| `start_year` / `latest_year` | 启用年度 / 最近一个有期间的年度 |
| `years[]` | 各年度结账进度：`year`、`total`（期间总数）、`closed`（已结账期数）、`vouchers`（凭证数）、`posted_vouchers`、`unposted_vouchers`、`carried`（是否已结完损益） |
| `init` | 初始化进度：`step`、`subjects_ready`、`openings_ready`、`opening_count` |

`years[].unposted_vouchers > 0` ⇒ 这一年账还没记完，余额与报表只含已记账部分。

### list_periods

参数：`book_id`（必填）。

返回 `items[]`（`PeriodRow`）：`id`、`code`（`2026-01`）、`year`、`month`、`ord`、
`locked`（已结账）、`closed_at`、`closed_by`、`vouchers`、`posted`、`unposted`、
`has_carry_forward`、`subject_count`。

已结账期间照读不误。`has_carry_forward=true` ⇒ 该期有损益结转凭证，会影响多个工具的取数口径。

### find_subjects

参数：`book_id`（必填）、`year`、`keyword`、`category`、`direction`、`only_leaf`、
`active_only`、`page`、`page_size`。

- `keyword` 同时模糊匹配**科目编码与科目名**，大小写不敏感，可命中多个。
- `year` 不传时取该账套**当前在记的那一年**。
- `category` 收英文枚举与中文名（`资产` / `资产类` 也收），出参里 `category` 恒为英文枚举，
  中文名在 `category_name`。
- `direction` 收 `DEBIT` / `CREDIT`。
- 强制分页，`page_size` 上限 200。

返回 `items[]`（`SubjectRow`）：`id`（**年度专属**）、`code`、`name`、`category`、
`category_name`、`direction`、`level`、`is_leaf`、`is_cash`、`active`、`year`、
`parent_code`。

查空时看 `hint` —— 例如「该编码在 2025 年也存在，请显式指定 year」。

### accounting_context

参数：`book_id`（必填）。

返回账套「现在是什么情况」：`book`、`years[]`（有科目表的年度）、`current_accounting_year`
（**当前在记的那一年**，不是服务器日历年）、`current_period`、`latest_closed_period`、
`subject_count`、`voucher_count`、`cash_subjects[]`（现金类科目 1001/1002/1012）。

---

## 明细组

### search_vouchers

参数：`book_id`（必填）、`year`、`subject_ids[]`（≤60）、`keyword`、`status`、
`include_unposted`、`date_from`、`date_to`、`min_amount`、`max_amount`、`sort`、`group_by`、
`exclude_carry_forward`、`page`、`page_size`（默认 50，上限 200）。

- `status`：`POSTED`（默认）/ `UNPOSTED` / `ALL`。`include_unposted=true` 且 `status=POSTED`
  时实际不过滤状态。
- `keyword` 只搜**凭证号、备注、分录摘要**，**不搜科目名** —— 找科目用 `find_subjects`。
- `min_amount` / `max_amount` 筛的是**单条分录**金额，不是整张凭证合计。
- `sort`：`date_desc`（默认）/ `date_asc` / `no_asc` / `no_desc`。
- `group_by`：`summary` / `subject` / `period` / `counterparty` / `direction`。
- `exclude_carry_forward=true` 剔掉结转凭证（结转会把损益余额重复计入）。

返回 `items[]`（`VoucherRow`）：`voucher_no`、`voucher_date`、`status`、`source`
（`MANUAL` / `CARRY_FORWARD` / …）、`remark`、`period_code`、`total_debit`、`total_credit`、
`entries[]`（`VoucherEntryRow`：`subject_code`、`subject_name`、`direction`、`amount`、
`summary`、`counterparty_cash`）、`is_carry_forward`。

**字段名是 `voucher_date`，不是 `date`** —— `date` 是 `latest_payment` / `check_period_payments`
那边 `PaymentRecord` 的字段名，两边不一样。

另有 `total`（命中总数，非本页条数）、`page`、`page_size`、`matched_entries`
（**命中过滤条件的分录条数** —— 凭证是按分录筛的，它与凭证张数不是一回事）、
`groups[]`（`group_by` 透视：`key`、`label`、`vouchers`、`entries`、`debit`、`credit`、`net`）、
`group_totals`。

### latest_payment

查「最近一次**真的**付出去」是什么时候。

参数：`book_id`（必填）、`subject_keyword` 或 `subject_id`、`as_of`、`lookback_months`
（默认 12）、`year`、`include_unposted`。

返回：

| 字段 | 说明 |
|---|---|
| `found` | 有没有找到真付出去的记录。**`found=false` 时金额、日期、凭证号都是 `null`** |
| `payment_date` / `amount` / `voucher_no` | 付款日期 / 金额 / 凭证号 |
| `amount_basis` | 恒为 `voucher_focus_side` = 这张凭证里该科目所有命中分录的带符号合计（与 `check_period_payments.paid_amount` 同口径） |
| `entry_amount` | 命中那**一条**分录自身的金额 |
| `voucher_cash_amount` | 这张凭证现金侧的带符号合计 |
| `subject` / `counterparty` | 目标科目 / 对方科目（`SubjectRef`） |
| `cash_flow_kind` | `RECEIPT` 收 / `PAYMENT` 付 / `TRANSFER` 转账 |
| `status` / `period_code` / `voucher_id` | 凭证状态 / 期间 / 凭证 id |
| `alternatives[]` | 其它候选记录，每条带 `kind`（`PAID` 已付 / `ACCRUED` 仅计提未付 / `RECEIVED` 已收款 / `OTHER` 其它往来）与 `kind_name` |
| `note` | 一句话结论（人话） |

命中多个科目时借贷两侧都会给候选 —— 用来区分「付出去的租金」与「收到的租金」。
现金类科目按编码前缀判定（1001 库存现金、1002 银行存款、1012 其他货币资金）。

### check_period_payments

核对某期间某科目**付了没有**。

参数：`book_id`（必填）、`period_code` 或（`year` + `month`）、`subject_keyword` 或
`subject_id`、`include_unposted`。

返回 `year`、`period`、`locked`、`paid`、`paid_amount`、`paid_dates[]`、`accrued_only`、
`unposted_only`、`records[]`（`PaymentRecord`）、`note`。

**`paid` / `accrued_only` / `unposted_only` 三个标志互斥** —— 「已付」「仅计提未付」
「有凭证但未记账」是三件不同的事。

### ledger_balance

科目余额表。与界面上的「科目余额表」是**同一份实现**，同一期间同一行数字必然一致。

参数：`book_id`（必填）、`period_code` 或 `period_id`、`from_period_code`（给区间时用）、
`subject_scope`（`all` / `top` / `leaf`）、`include_unposted`。

返回 `rows[]`（`BalanceRow`）：`code`、`name`、`category`、`category_name`、`direction`、
`opening`、`period_debit`、`period_credit`、`ytd_debit`、`ytd_credit`、`closing`、
`side`（借 / 贷 / 平，**按符号判定**，不是按科目自然方向反推）、`included_in_totals`。

`included_in_totals=false` ⇒ 这一行不计入 `check` 合计（合计恒按末级科目算，父科目行
不计入）—— 所以「显示行之和 ≠ 合计」是正常的。

`check`（`BalanceCheck`）：`opening_balanced`、`period_balanced`、`closing_balanced` 及
对应的借贷方合计。

`totals_scope` 说明 check 里的合计按哪个口径算（`leaf` 恒定，不随 `subject_scope` 变）。

区间查询时**期初 = 起始期自己的期初**（= 上一期期末），与单期查询同一口径。

---

## 分析组

### income_expense_trend

参数：`book_id`（必填）、`year`（**必填**，会计年度）、`granularity`
（`month` 默认 / `quarter` / `year`）、`categories[]`（科目类别过滤，英文码或中文名）、
`include_unposted`。

返回 `year`、`series[]`（`TrendPoint`：`period`（`2026-03` / `2026-Q2` / `2026`）、
`income`、`expense`、`net`、`income_by_category`、`expense_by_category`）、
`yoy`（`TrendYoy`：`income_change_pct`、`expense_change_pct`）、
`carry_forward_excluded`（报表口径恒为 `true`）。

同比在「本年没有已记账的损益、但有未记账被排除」时返回 **`null`**，而不是一个由记账
状态造出来的 `-100%`。

### render_report

参数：`book_id`（必填）、`template_code` 或 `template_id`、`period_code` 或 `period_id`、
`include_unposted`。

- `template_code`：`BALANCE_SHEET`（资产负债表）/ `INCOME_SHEET`（利润表）。
- 账套 fork 过模板后，副本与内置基线**同一个 code 两行**；**账套自己的副本优先**。
- `period_code` / `period_id` 都不传时取「当前在记的那一期」。

返回 `template`（`ReportTemplateRef`：`id`、`code`、`name`、`type`、`builtin`）、
`period`（`ReportPeriod`）、`rows[]`（`ReportRow`：`row_no`、`label`、`indent`、`bold`、
`hidden`、`style`、`cells[]`）、`carry_forward_excluded`、`carry_forward_period_amount`、
`notices[]`（`ReportNotice`：`code`、`level`、`text`、`cols`、`codes`、`amount`）、
`errors[]`、`include_unposted`。

`ReportCell`（`col_no`、`label`、`kind` 取数口径如 `YTD_AMOUNT`、`formula`、`value`、
`error`）—— 单元格级报错在 `error` 里，报表级报错在 `errors[]` 里。

**内置模板可能与账套科目表对不上**：内置基线引用的是一套通用科目编码（如 5001 主营业务
利润、5401 营业成本），账套用的是另一套时（如 6001/6401），单元格会报「科目不存在: 5001」，
`errors[]` 里也会列出来。这是模板与科目表体系不匹配，不是数据坏了 —— 换
`income_expense_trend` 或 `ledger_balance` 取数。
