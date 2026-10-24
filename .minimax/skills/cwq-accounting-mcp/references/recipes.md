# 问答配方

每个配方给出**调用序列**与**出参解读**。通用前提：先 `list_books` 拿到 `book_id`，
再 `accounting_context` 拿到当前年度与期间 —— 后者是「当前在记的那一年」，
不是服务器日历年。

---

## 1. 「这个月电费付了吗」/「这笔钱付出去没有」

```
1. accounting_context(book_id)                    → 当前期间 current_period（如 "2026-10"）
2. find_subjects(book_id, year=<当前年度>, keyword="电费")   → 本年度的科目 id
3. check_period_payments(book_id, period_code=<当前期间>, subject_id=<上一步 id>)
```

出参解读 —— 三个互斥标志只会有一个为真：

| 标志 | 含义 | 该怎么说 |
|---|---|---|
| `paid=true` | 已付，对方是现金类科目 | 「已付 X 元，付款日期 …」 |
| `accrued_only=true` | 仅计提未付，对方是应付账款等负债类 | 「计提了但钱还没付出去」 |
| `unposted_only=true` | 有凭证但未记账 | 「有凭证但还没记账，已记账口径下算已付 0 元」 |

三个都是 false 时，看 `coverage`：区间内确实一张凭证都没有才是「本月没有这笔业务」。

**不要只报金额**：对方科目不是现金类时，计提不等于付出去。

## 2. 「最近一笔租金是什么时候付出去的」

```
1. find_subjects(book_id, year, keyword="租金")   → 可能命中多个科目
2. latest_payment(book_id, subject_keyword="租金")  → 或直接传 subject_id
```

出参解读：

- `found=false` ⇒ 在 `lookback_months`（默认 12）窗口内**没有真付出去**的记录。
  金额、日期、凭证号都是 `null` —— 不要把空日期当成「付过但没填日期」。
- `found=true` ⇒ 看 `payment_date`、`amount`、`voucher_no`、`counterparty`、
  `cash_flow_kind`。
- `amount` 是**整张凭证上该科目命中分录的合计**（`amount_basis="voucher_focus_side"`），
  不是单条分录的金额。单条分录在 `entry_amount`。
- `alternatives[]` 里有其它候选（`ACCRUED` 仅计提、`RECEIVED` 收到的、`OTHER` 其它往来）。
  命中多个科目时借方与贷方两侧都有候选 —— 靠 `kind` 与 `cash_flow_kind` 区分
  「付出去的」与「收到的」。
- `lookback_months` 不够时加大它重查；用户问「历史上最近一次」时把它设大（如 60）。

## 3. 「今年收入费用怎么变」/「同比涨了多少」

```
1. accounting_context(book_id)                    → current_accounting_year
2. income_expense_trend(book_id, year=<该年度>, granularity="month")
```

出参解读：

- `series[]` 逐期给 `income` / `expense` / `net`（`net = income − expense`）。
- `yoy` 可能是 **`null`** —— 这不是缺数据，而是「本年没有已记账的损益、但有未记账被排除」。
  把它说成「同比 -100%」是错的。
- `carry_forward_excluded=true`：损益口径已排除结转凭证。
- 先读 `coverage`：`excluded_unposted_vouchers > 0` 时，接近 0 的收入是**记账状态**造成的，
  不是「今年没收入」。这时可传 `include_unposted=true` 再查一次对比。

按季 / 按年用 `granularity="quarter"` / `"year"`；`categories` 可按科目类别过滤。

## 4. 「给我资产负债表 / 利润表」

```
1. render_report(book_id, template_code="BALANCE_SHEET")        # 或 INCOME_SHEET
2. 需要指定期间时：render_report(book_id, template_code=..., period_code="2026-10")
```

出参解读：

- `rows[].cells[]` 逐格给 `label` / `kind`（取数口径，如 `YTD_AMOUNT`）/ `value` /
  `formula` / `error`。
- **单元格级报错**在 `cells[].error`，**报表级报错**在 `errors[]`。
- `notices[]` 是人话提示（如结转口径差异），`level` 为 `info` / `warning`。
- 账套 fork 过模板时副本优先于内置基线；`template.builtin` 可看出用的是哪一份。
- 先读 `coverage`：报表在账没记完时**照样能渲染出一份结构完整、平衡校验为 0 的表** ——
  那时看到的数只是期初快照。

## 5. 「科目余额表」/「某科目期末余额多少」

```
1. ledger_balance(book_id, period_code=<期间>)                  # 单期
2. ledger_balance(book_id, from_period_code=<起>, period_code=<止>)  # 区间
```

出参解读：

- `rows[]` 每行给 `opening` / `period_debit` / `period_credit` / `ytd_debit` /
  `ytd_credit` / `closing`，全部带符号。
- `side`（借 / 贷 / 平）**按符号判定**，不是按科目自然方向反推 —— 资产类科目也可能
  出现贷方余额（红字或调整）。
- `included_in_totals=false` 的行不计入 `check` 合计（合计恒按末级科目算）。
  **「显示行之和 ≠ 合计」是正常的**，别当成 bug。
- 区间查询的**期初 = 起始期自己的期初**（= 上一期期末），与单期查询同口径。
- `subject_scope`：`all` 全部 / `top` 只顶级 / `leaf` 只末级。
- 与界面上的「科目余额表」是同一份实现，同期间同行数字必然一致。

## 6. 「找某个科目」/「电费是哪个科目 id」

```
1. find_subjects(book_id, year, keyword="电费")   → 可命中多个
2. 需要更窄时：加 category="EXPENSE" / direction="DEBIT" / only_leaf=true
```

出参解读：

- `keyword` 同时匹配编码与科目名。查不到时看 `hint`（例如提示该编码在别的年度也有，
  请显式指定 year）。
- `id` 是**年度专属**的 —— 跨年必须用对应年度的 id，混用**不报错、只静默返回空**。
- `is_cash=true` 的科目就是现金类（1001/1002/1012），「真付出去」只认与这些科目的对手方。
- 凭证只能挂**末级**科目（`is_leaf`）。

## 7. 「这个月有哪些凭证」/「某科目发生了什么」

```
1. search_vouchers(book_id, year=<年度>, subject_ids=[<id>], date_from="2026-10-01", date_to="2026-10-31")
2. 按摘要汇总：search_vouchers(..., group_by="summary")
```

出参解读：

- `items[]` 是凭证，每张带 `entries[]` 分录明细；`counterparty_cash` 标出该分录对方
  是不是现金类。
- `matched_entries` 是**命中过滤条件的分录条数**，与返回的凭证张数不是一回事。
- `total` 是命中总数，`page` / `page_size` 是分页状态；翻页用 `page` 递增。
- `group_by="summary"` 一次拿到按摘要汇总；`exclude_carry_forward=true` 把结转凭证剔掉
  （结转会把损益余额重复计入）。
- `keyword` **不搜科目名** —— 找科目用 `find_subjects`。
- `min_amount` / `max_amount` 筛的是**单条分录**金额。

## 8. 「哪些账套 / 记到哪了 / 今年账记完了吗」

```
1. list_books()                    → 账套清单 + 各年度进度
2. list_periods(book_id)            → 全部期间 + 每期凭证数/已记账数/结账状态
3. accounting_context(book_id)      → 当前年度、当前期间、最近已结账期间、现金类科目
```

出参解读：

- `years[].unposted_vouchers > 0` ⇒ 这一年**账还没记完**，余额与报表只含已记账那部分。
- `years[].carried=false` ⇒ 损益还没结转，损益类取数（报表口径）会与余额口径差一个
  结转金额。
- `init.step` / `subjects_ready` / `openings_ready` ⇒ 账套还在初始化阶段时，取数多半是空的。
- `periods[].has_carry_forward=true` ⇒ 该期有结转凭证，读聚合数时注意排除。

---

## 9. 交叉校验：拿到数之后对一遍

单个工具给的是单一路径的数。关键数字用第二个工具对一遍，差值出现了就是有一方口径没读对。

```
1. 收入 / 费用全年数  ↔  ledger_balance(book_id, period_code) 的本年累计发生额（ytd_debit / ytd_credit）
2. 某笔付款金额        ↔  search_vouchers(subject_ids=[<id>]) 里那张凭证的分录合计
3. 逐月合计           ↔  income_expense_trend(granularity="year") 的年度桶
```

对账时注意口径是否一致：`ledger_balance` 的本年累计**含结转**，`income_expense_trend`
**排除结转**（报表口径）—— 所以损益类科目的两者差额恰是结转凭证金额，不是错误。
排除结转后再比：`search_vouchers` 加 `exclude_carry_forward=true`。

---

## 通用收尾

每个答案落地前核对三件事：

1. **年度对不对** —— 说的是哪个会计年度、当前在记的那一年是多少。
2. **记账口径对不对** —— 含不含未记账（`coverage.posting`），排除的未记账有几张。
3. **业务口径对不对** —— 损益（报表口径，排除结转）还是余额（账簿口径，含结转）。

## 对外怎么说

上面这些核对都是**内部**的。给用户的回答里只留业务事实：

- **不出现**：工具名、参数名、字段名、内部 id、`true`/`false`/`null`、JSON、错误码。
- **出现**：账套名、公司名、期间、科目编码与名称、凭证号、金额、笔数、日期、结论。
- **口径要讲，但用大白话**：讲成「这批凭证还没记账，所以账面还看不到」，不是讲成
  `coverage.completeness=PARTIAL`。
- **先结论后前提**：「付了，8 万元，11 月 12 日付的」比字段罗列有用得多。
- **有异常就直说并给下一步**：「先把这 223 张凭证记了，再复跑一次」比「数据不可信」有用。

对照表见 SKILL.md 的「输出契约：说人话」。
