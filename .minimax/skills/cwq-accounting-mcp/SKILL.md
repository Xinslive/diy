---
name: cwq-accounting-mcp
description: |
  Query the cwq accounting system's read-only MCP service (10 tools) to answer
  bookkeeping questions: which books/periods/subjects exist, whether a payment
  actually went out, voucher detail, account balances, income-expense trends,
  and balance/income statements. Use this skill whenever the user asks about
  ledger data — 查账, 账套, 会计期间, 科目余额表, 凭证明细, 报表, 「这个月电费付了吗」,
  「最近一笔租金是什么时候付出去的」「今年收入费用怎么变」— or whenever an agent
  must call this project's /api/mcp endpoint. Do NOT use it to create, post or
  close vouchers (this service is read-only), and do NOT use it to develop or
  debug the MCP server itself.
descriptions:
  zh-Hans: "用本项目只读 MCP 服务查账：账套、期间、科目、凭证、付款判定、科目余额、收支趋势与报表。"
displayNames:
  zh-Hans: "记账系统 MCP 查询"
---

# CWQ 记账系统 MCP 查询

本项目对外暴露一个**只读** MCP 服务（服务名 `cwq-accounting`，10 个工具），外部 AI
agent 通过它查账。它**没有任何写能力**：不能建凭证、不能记账、不能结账、不能改科目、
不能导入，也不提供任意 SQL 入口。

## 接入

服务挂载在 `<base>/api/mcp`，传输是 **Streamable HTTP（stateless）**，认证用 bearer
令牌（明文形如 `mcp_<token>`，在 `/api/system/mcp-tokens` 签发）。

若当前会话里还没注册这个服务，先注册再调用：

```bash
mavis mcp create --name cwq --transport http \
  --url https://<host>/api/mcp \
  --headers '{"Authorization":"Bearer mcp_<token>"}'
```

注册后工具会以 `mcp__cwq__<tool_name>` 的形态出现在工具列表里（实例编号可能带后缀，
形如 `mcp__cwq__2__list_books`）。如果工具列表里没有，用 `tool_search` 按工具名检索，
再用 `mcp_invoke` 带 `tool_name` + `arguments` 调用。

令牌可见的账套范围＝令牌绑定那个用户被授权的账套范围。用户的权限变了（撤授权、
停用、换角色），**下一次调用就变**，不需要重启也不需要换令牌。

## Inputs to collect

优先用工具解析，不要靠猜：

- **哪个账套** —— 调 `list_books` 拿到授权范围内的账套。只有一个就直接用；有多个
  且用户没说清是哪一个，才需要问用户。
- **哪个会计年度** —— 调 `accounting_context` 看「当前在记的那一年」。
  **不要用服务器日历年代替**：连接池把会话时区钉死成 UTC，而且一本 2026 年建的账
  补录 2025 年分录时「本年」指的是 2026。
- **问题属于哪一类** —— 见下面的工具决策表。

## Procedure

1. **先定位账套与年度**（`list_books` → `accounting_context`）。
   原因：后面每一个工具都要 `book_id`，而 `book_id` 选错时**不会报错、只会静默返回
   空**或返回另一个账套的数。

2. **按问题类型选工具**。不要拿一个大而全的查询硬套所有问题：

   | 用户在问 | 用这个工具 |
   |---|---|
   | 我有哪些账套 / 记到哪个月了 | `list_books`、`list_periods`、`accounting_context` |
   | 「电费」「房屋租赁费」是哪个科目 | `find_subjects` |
   | 有哪些凭证 / 某科目发生了什么 | `search_vouchers` |
   | 「这个月电费付了吗」「付了多少钱」 | `check_period_payments` |
   | 「最近一笔租金是什么时候付出去的」 | `latest_payment` |
   | 科目余额表 / 期末余额 | `ledger_balance` |
   | 收入费用怎么变 / 同比 | `income_expense_trend` |
   | 资产负债表 / 利润表 | `render_report` |

3. **涉及金额或凭证的工具，输出后先读 `coverage` 再读数字**（见下节）。
   原因：这个服务最危险的返回不是报错，是**在数据缺失时给出一份结构完整的零值**。

4. **跨年问题显式传 `year`**。科目表每个会计年度各有一份：同一编码在不同年度是
   **两行、两个 id**。把上一年的科目 id 传进来**不报错，只静默返回空**。
   要按科目过滤时，先用 `find_subjects(year=...)` 拿到本年度的科目 id；
   命中多个科目时先分清哪边是收、哪边是付（收入侧 vs 成本侧同名科目很常见），
   问「付出去」就锁成本侧那一个。

5. **金额口径核对到分录层**。`latest_payment` 的 `amount` 是整张凭证上该科目命中
   分录的带符号合计，不是单条分录的金额。一张凭证上有好几条同科目分录时两者差很多
   —— 用 `search_vouchers` 把那张凭证的分录列出来看清楚，再决定报哪个数。

6. **交叉校验关键数字**。单个工具给的是单一路径的数；用第二个工具对一遍再开口：
   收入 / 费用的全年数用 `ledger_balance` 的本年累计发生额核对，付款金额用
   `search_vouchers` 的分录合计核对。差值出现了就是有一方的口径没读对，
   这时候先查清差异来源，不要挑一个顺眼的报。

7. **组装答案时把口径一起说出来**（见「输出契约」）。

## 读数纪律

**这一节是内部读法** —— 字段名、枚举值都是给你（模型）看的，用来判断手里的数可不可信。
读完要翻译成大白话给用户，**不要照抄字段名**（见「输出契约：说人话」）。

所有工具的返回体都带 `caliber`（这份数在哪套口径下）与 `notes[]`（人话说明）。
这两个字段不是装饰 —— 它们是判断「这个数能不能直接念给用户听」的唯一依据。

**先读 `coverage`：**

| 字段 | 含义 |
|---|---|
| `posting` | 本次取数的记账口径：`POSTED_ONLY` / `POSTED_AND_UNPOSTED` / `UNPOSTED_ONLY` |
| `posted_vouchers` / `unposted_vouchers` | 实际纳入统计的已记账 / 未记账凭证张数 |
| `excluded_unposted_vouchers` | **因口径被排除**的未记账凭证张数 |
| `periods_with_data` / `periods_total` | 有凭证的期间数 / 期间总数 |
| `completeness` | `FULL`（完整）/ `PARTIAL`（有东西没算进来）/ `NONE`（区间内一张凭证都没有） |

判读方式：

- `excluded_unposted_vouchers > 0` ⇒ **账还没记账**，看到的 0 是「还没记」不是「没有业务」。
  要看全量就传 `include_unposted=true`。
- `completeness == "NONE"` ⇒ 这个区间确实一张凭证都没有，0 是真的 0。
- `completeness == "PARTIAL"` ⇒ 0 里缺的是未记账的部分。

**金额约定：**

- 单位是**元**，两位小数，以**字符串**承载（避免浮点误差），带 `currency: "CNY"`。
  比较前先转成数值，别拿字符串直接比。
- **负数是红字**（冲销），不是负数金额。别把符号抹掉。
- 默认只读**已记账**（`status="POSTED"`）的凭证；要包含未记账必须显式传
  `include_unposted=true`。
- **损益取数用报表口径**（排除结转凭证），**余额取数用账簿口径**（含结转）。两者差额
  恰是结转凭证金额，`notes[]` 会写明差多少。同比（`yoy`）在「本年没有已记账的损益、
  但有未记账被排除」时返回 `null`，而不是一个由记账状态造出来的 `-100%`。

**「付了没有」是三个互斥结论，不要合并成一句话：**

- `paid=true` ⇒ 已付（对方是现金类科目 1001/1002/1012）；
- `accrued_only=true` ⇒ 仅计提未付（对方是应付账款等负债类科目，钱还没出去）；
- `unposted_only=true` ⇒ 有凭证但未记账（**不算已付**）。

对方不是现金类科目时，只是计提，不是「付出去」。

**两个容易问出口径的地方：**

- `latest_payment` 的 `amount` 是**整张凭证**上该科目命中分录的带符号合计
  （`amount_basis="voucher_focus_side"`），不是最近那一条分录的金额 ——
  一张凭证上有好几条同科目分录时两者差很多。单条分录的金额在 `entry_amount`。
- `ledger_balance` 给区间（`from_period_code` + `period_code`）时，**期初 = 起始期
  自己的期初**（= 上一期期末），与单期查询同一口径。

## 输出契约：说人话

提问的是财务人员，不是工程师。**回答里只出现业务事实，不出现系统内部的东西。**

参数、字段名、id 是**你和工具之间的事**，不该出现在给用户的回答里。用户在界面上
看得到的是账套名、科目编码、凭证号、金额、日期 —— 回答里也只该有这些。

**不要写进回答里的：**

- 工具名（`check_period_payments`、`latest_payment`……）
- 参数名与字段名（`book_id`、`period_code`、`found`、`paid`、`unposted_only`、
  `paid_amount`、`amount_basis`、`coverage`、`caliber`、`completeness`、`notes`、
  `include_unposted`……）
- 内部 id：账套 id、科目 id、凭证 id、期间 id
- 程序值：`true` / `false` / `null`、JSON 片段、接口路径、错误码
- 内部枚举：`POSTED_ONLY`、`PARTIAL`、`YTD_AMOUNT`、`voucher_focus_side`……

**该写进回答里的：** 账套名与公司名、期间（「2026 年 11 月」）、科目编码与名称、
凭证号（记0007）、金额、笔数、日期、结论，以及结论的前提。

### 口径要用大白话讲

口径**必须讲**，但要用用户能听懂的话讲，不能把字段值原样甩出去。

| 内部读到 | 对用户这么说 |
|---|---|
| 有未记账凭证被排除 | 「这本账有 223 张凭证还没记账，所以默认看到的数不完整 —— 下面给的是含未记账的全量」 |
| 区间里一张凭证都没有 | 「这个区间确实一笔业务都没有」 |
| 已付 | 「已付 12,800 元，付款日期 10 月 8 日」 |
| 仅计提未付 | 「只是记了这笔应付，钱还没付出去」 |
| 有凭证但未记账 | 「有凭证，但还没记账，所以账面上还不能算已付」 |
| 没找到已记账的付款 | 「没找到已经记账的付款记录」—— **不要**说成「没付过」 |
| 同比算不出来 | 「同比算不出来，因为上一年的数据这本账里没有」 |
| 整张凭证上该科目的合计 | 「这笔凭证上这个科目的合计金额」（口径记在心里，嘴上不念这个词） |

拿不准某个字段怎么说时，用 `notes[]` 里的中文说明改写 —— 它本来就是写给人看的。

### 语气

- **先给结论，再给前提。** 「付了，8 万元，11 月 12 日付的」比「该期间该科目在现金类
  对方科目下有匹配记录」有用得多。
- **「还没记账」这件事一定要讲**，但要讲成「这批凭证还没记账，所以账面上还看不到」，
  不是讲成口径参数。用户据此才会去催记账。
- 金额原样引用服务返回的值，别自行四舍五入或改格式。
- 有异常就直说，并给出下一步建议（先记账、再复跑；先核对发票是否重复入账）。

## 失败处理

出错时返回的 `structuredContent` **仍是一份符合该工具 `outputSchema` 的结构体**，
错误本身在 `error` 里：`{"code": ..., "message": ..., "detail": {...}}`。`message` 是中文、
可直接读；`detail` 是原始异常摘要 —— **出错先看 `detail`**，别只猜 `code`。

| `error.code` | 含义与对策 |
|---|---|
| `unauthorized` | 令牌无效 / 已吊销 / 用户被停用。让用户去 `/api/system/mcp-tokens` 轮换 |
| `forbidden` | 令牌绑定用户没有该账套的授权。换账套或让管理员授权 |
| `not_found` | 账套 / 期间 / 科目 / 模板不存在。先用 `list_books`、`list_periods` 核对 id 与期间码 |
| `bad_request` | 入参不合法（枚举值错、区间超限、科目跨年）。`detail` 里会列出合法取值 |
| `constraint_violation` | 业务约束冲突（如科目已被占用） |
| `query_timeout` | 查询超时。缩小区间或加过滤条件重试 |
| `rate_limited` | 超过每小时调用上限（默认 600 次/进程）。退避后重试，不要密集重试 |
| `internal_error` | 服务端异常。把 `detail` 原样回报给用户 |

**降级路径**：某个工具因账套数据缺失报 `not_found` 时，退回上一层工具
（`accounting_context` 看现状、`list_periods` 看期间码、`find_subjects` 重新解析科目），
而不是换个工具硬试同一组参数。

**`render_report` 渲不出来时先看是不是模板跟账套对不上**。内置基线模板引用的是一套
通用科目编码（如 5001 主营业务利润、5401 营业成本），而账套用的可能是另一套
（如 6001/6401）。对不上时**单元格级会报「科目不存在: 5001」**，`errors[]` 里也会列
出来 —— 这不是账坏了，是模板与科目表体系不匹配。此时改用 `income_expense_trend` 或
`ledger_balance` 取数，不要反复重试同一张报表。

**给用户看报错时同样说人话**：不要甩 `bad_request` 或 JSON。要说「这个期间不存在」、
「查的范围太长了，拆成两次查」这类话；只有当用户明确要排查接口时才给错误码。

## 硬约束

- `page_size` 上限 200（超了会被**夹到上限**而不是报错），`find_subjects` 强制分页。
- `search_vouchers` 的 `subject_ids` 最多 60 个（schema 上写了 `maxItems`，服务端还会再判一次）。
- 单次查询区间上限 60 个月；超出要拆开查再合并。
- 语句超时 15 秒；调用频次上限 600 次/小时（多进程下按进程计）。
- 枚举参数大小写不敏感、非法值会回 `bad_request` 并列出合法取值：
  `status` = `POSTED|UNPOSTED|ALL`；`sort` = `date_desc|date_asc|no_asc|no_desc`；
  `group_by` = `summary|subject|period|counterparty|direction`；
  `subject_scope` = `all|top|leaf`；`granularity` = `month|quarter|year`；
  `category` = `ASSET|LIABILITY|EQUITY|COST|PROFIT`（中文名也收）；
  `direction` = `DEBIT|CREDIT`；`template_code` = `BALANCE_SHEET|INCOME_SHEET`。

## Examples

**「这个月电费付了吗」**

内部调用：`accounting_context` 定当前期间与年度 → `find_subjects(year=..., keyword="电费")`
拿本年度科目 id → `check_period_payments(period_code=..., subject_id=...)` → 读三个互斥
标志与 `coverage`。

给用户的回答（照这个语气写）：

> 10 月的电费**已付 12,800 元**，付款日期是 10 月 8 日，对方的银行存款。

其它三种情形分别这么说：

> 10 月的电费**只是记了这笔应付，钱还没付出去**（对方挂的是应付账款）。
>
> 10 月的电费**有凭证，但还没记账**，所以账面上暂时还看不到已付金额 —— 记账之后这里就会显示。
>
> 10 月**确实没有这笔电费**，这个月一张凭证都没有。

**「今年收入费用怎么变」**

内部调用：`accounting_context` 定年度 → `income_expense_trend(year=..., granularity="month")`
→ 读 `coverage`。

给用户的回答：

> 2026 年每月收入与费用如下（单位元，已排除每月月末的损益结转）：
>
> | 月份 | 收入 | 费用 | 净额 |
> | --- | --- | --- | --- |
> | 1 月 | 244,980.00 | 206,186.00 | 38,794.00 |
> | … | | | |
>
> 全年合计收入 3,257,760.00 元、费用 2,914,550.44 元、净利润 343,209.56 元。
>
> **同比算不出来** —— 这本账 2026 年才开账，2025 年既没有会计期间也没有科目表，
> 没有可比基期。想看同比，得先把 2025 年的数据导进来。

如果 `coverage` 显示有凭证未记账，答复里要补一句：

> 先说明一下：这本书 2026 年的 223 张凭证**一张都还没记账**，所以账面上看到的数是不完整的。
> 上面的数字是**含未记账**的全量，记账之后以账面为准。

最后这句是本次回答里最要紧的一句 —— 它决定用户会不会去催记账。

## Windows (win32) platform notes

注册 MCP 服务那条命令用的是 bash 续行符，PowerShell 下改成单行 + 单引号 JSON：

```powershell
mavis mcp create --name cwq --transport http --url https://<host>/api/mcp --headers '{"Authorization":"Bearer mcp_<token>"}'
```

本 skill 的其余部分是纯流程（调用 MCP 工具并解读返回体），不依赖 shell，Windows 与
macOS/Linux 行为一致。

## 参考

- 10 个工具的完整参数与返回字段：`references/tools.md`
- 按问题类型组织的调用配方与出参解读：`references/recipes.md`
