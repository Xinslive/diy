# 架构说明

> 这份文档的目的是**让后续迭代不会推翻前面的设计**。
> 改代码前先在这里确认约束；要改约束，先改这份文档。

---

## 0. 三条铁律

1. **唯一事实来源是 `voucher` + `entry` 两张表。**
   余额、总账、明细账、科目余额表、资产负债表、利润表、打印 —— 全部是派生视图，
   不存第二份「算好的余额」。这是「总账和报表永远对得上」的根本原因。

2. **全系统只有一处算余额：`app/ledger/engine.py`。**
   任何新模块想拿数字，都必须走 `LedgerEngine`，不许自己写 SQL 求和。
   （曾经报表单独算过一次，两边口径漂移，改一个要改两处。）

3. **护栏写在数据库触发器里，不写在应用层。**
   应用层会被绕过（脚本、psql、未来的数据修复），数据库不会。
   `app/core/db_init.sql` 里的触发器是最后一道防线。

---

## 1. 分层

```
HTTP 层      app/api/*.py          参数校验、错误翻译，不含业务逻辑
业务服务层   app/accounting/*.py   事务边界在这一层
账簿引擎     app/ledger/           唯一的算账逻辑，无 HTTP 依赖
报表引擎     app/reports/          公式解析 + 求值，与账簿引擎解耦
打印引擎     app/printing/         模板渲染，与业务数据解耦
数据模型     app/models/           SQLAlchemy
数据库护栏   app/core/db_init.sql  触发器、约束
```

依赖方向是单向的：`api → accounting → ledger → models`。
`ledger` 不 import `accounting`，`reports` 不 import `api`。这条不许反。

---

## 2. 账簿引擎：四原语

所有账簿和报表的数字，最终都来自这四个原语：

```python
eng.opening(subject_id, ord)     # 期初
eng.debit_cum(subject_id, ord)   # 借方累计（账套起始月 .. 期间）
eng.credit_cum(subject_id, ord)  # 贷方累计
eng.closing(subject_id, ord)     # 期末
```

派生量在 `Figures` 里统一算：

```
期末  = 期初 + 本期借方 - 本期贷方        ← 只能用本期，不能用累计
本期借方 = 借方累计(P) - 借方累计(P-1)
```

> ⚠️ 这里曾经出过 200000 被算成 1980000 的 bug：用累计值推进期初会把年内发生额重复计入。
> `Figures.closing` 的 docstring 里写着原因，改动前先读。

### 上卷
凭证只挂在明细级科目上，但账簿要每一级父科目的数。
`LedgerEngine._cum_movements` 一次性把发生额上卷到所有父科目，
避免每个查询各写一遍递归。

### 方向约定
`signed = 借方合计 - 贷方合计`，即**借正贷负**。
因此负债、权益、收入类科目的余额天然是负数。

`display_side()` **只看符号、不看科目属性**，把 signed 翻成「方向 + 绝对金额」。
> 曾经按科目自然方向判断，导致 2001 短期借款期末 -1000 显示成「借 1000」，
> 而试算平衡是按符号累计的，同一行一边算贷方一边标借方。

---

## 3. 期初余额：推导值，不是快照

这是整个设计里最重要的一个决定。

```
期初(P) = 期末(P-1) = 期初(P-1) + 本期借方(P-1) - 本期贷方(P-1)
```

一路回推到账套启用月那笔录入的期初。

`period_opening` 表只存两处「真·期初」：
- 账套启用时录入
- 年度结转时结转过来

**收益：**
- 平时不需要物化任何数据，改上游数据余额自动跟随，不存在快照失效
- **反结账只要解锁就能自动恢复可写** —— 这是「批量反结账一键完成」的前提
- 试算平衡在任何时点都成立

`period_opening` 上有 `trg_opening_guard`：不可直接 UPDATE，必须走反结账流程。

---

## 4. 结账、反结账、结转

### 结账级联
不允许出现「3 月已结账、1 月还开着」——那会让已结账期间的累计数随上游改动漂移。
所以关闭第 N 期时，前面未结账的期间**一并结账**，返回值里明确告诉用户顺带结了什么：

```json
{"period": "2026-03", "auto_closed_periods": ["2026-01", "2026-02"], "auto_posted": 1}
```

### 批量反结账（用户最看重的功能）
从指定期间起，连续解锁到最后一个已锁期间，**区间内的已记账凭证自动回到未记账**。

> 为什么要回退记账状态：账套解锁了凭证还标「已记账」的话，默认口径下余额不会变；
> 用户改完凭证重新记账时数字会跳变。两边状态必须一致。

解锁必须**先于**回退凭证状态执行 —— 凭证的写保护触发器按期间锁判断，顺序反了会把自己锁在门外。

### 结转损益
月末生成一张收入类、一张成本费用类凭证，`source=CARRY_FORWARD`，日期取当月最后一天。

**幂等护栏**：本期若已有 `CARRY_FORWARD` 凭证，直接拒绝并提示先删除。
> 因为结转凭证默认是「未记账」，而账簿默认口径只统计已记账，
> 所以「损益是否已结平」在刚结转完是看不见的 —— 连点两次会生成两张凭证，本年利润翻倍。

### 报表必须排除结转凭证
`LedgerEngine(exclude_carry_forward=True)`。
否则月度结转之后损益类科目本期净额恒为 0，利润表全空。

### 年度结转
1. 12 期必须全部结账
2. 结转本年利润 → 未分配利润：
   无论盈亏都是 **借 本年利润 / 贷 未分配利润**，金额取 `|余额|`
   （盈利增加留存收益，亏损冲减留存收益）
   > 早期版本按符号把两个科目借贷对调了，盈利时会做成「借 未分配利润 / 贷 本年利润」。
3. 生成新账套：继承科目表 + 用户授权 + 各明细科目期末余额作为新账套首期期初

---

## 5. 两阶段写入模式

**凡是有唯一约束冲突风险的批量改号，都是两阶段。**

`uq_voucher_period_no` 是即时唯一约束，直接改号在交换（记0001 ⇄ 记0002）时会撞库。

```python
# 阶段一：整体挪到 ∥<id>，与任何真实凭证号都不冲突
for v in targets: v.voucher_no = f"∥{v.id}"
db.flush()
# 阶段二：写最终号，此时序列里已经没有旧号
for c in targets: db.get(Voucher, c.voucher_id).voucher_no = c.new_no
```

同样的模式用在报表行插入/删除（`reports.py::_shift_rows`，中间态先挪到 `+100000`）。

**新增类似功能时照抄这个模式，不要另发明一套。**

---

## 6. 分录冗余列

`entry.book_id` 和 `entry.period_ord` 是从 `voucher` 反规范化的，为了让账簿查询不用 JOIN。

同步靠两个触发器，**两个都必须存在**：

| 触发器 | 时机 | 覆盖场景 |
|---|---|---|
| `trg_entry_sync` | `BEFORE INSERT OR UPDATE OF voucher_id` | 新建分录 / 分录换凭证 |
| `trg_voucher_resync` | `AFTER UPDATE OF period_id` | **凭证改日期换月** |

> 只写第一个是最严重的一类 bug：凭证从 1 月改到 5 月，`entry.period_ord` 永远停在 1，
> 账簿会把金额算进错误月份，**而且不报错**。加第三个触发器时务必想清楚它在哪种写入路径上生效。

---

## 7. 报表：每格公式自由可配

模板表：`report_template` / `report_row` / `report_col` / `report_cell` / `report_result`。

**公式引用科目编码而不是 ID**，所以一份模板可以直接用于所有账套：

```
B2  = sum(1001, 1002, 1012)      货币资金
B8  = sum(1401, 1402, 1403, 1405, 1408, -1471)   存货扣跌价准备
B9  = 合计(B2:B8)                 流动资产合计
B17 = B9+B16                      资产总计
```

### 公式能力
- 算术、比较、单元格引用（A1 记法）、区域求和
- 函数：`subject()` `sum()` `合计()` `上一行()` `单元格()` `if()` `round()` `abs()` `max()` `min()`
- 期间占位符：`{{本期}}` `{{上期}}` `{{年初}}` `{{期末}}`
- 单元格引用带**环检测**，循环引用不会死循环

### 求值上下文
`ReportContext` 把账簿引擎和模板胶合起来，并向 `Evaluator` 暴露：
`subject_id(code)` / `opening` / `debit_cum` / `credit_cum` / `closing` /
`period_dr` / `period_cr` / `cell(row, col)` / `resolve_period(expr)` / `label(row)`

**求值上下文是接口**，`Evaluator` 不知道数据库存在。所以单元测试可以用 `FakeCtx` 跑，
不需要建库（`tests/test_formula.py` 39 条就是这么写的）。

---

## 8. 打印模板

数据源 → 模板行结构 的适配层在 `app/printing/preview.py`：
`VOUCHER` / `GENERAL_LEDGER` / `DETAIL_LEDGER` / `BALANCE` / `REPORT`

渲染器 `app/printing/template.py`：HTML + CSS `@page`，浏览器原生分页。
纸型、方向、页边距、字体、列宽、页眉页脚全部可配。

金额中文大写 `to_cn_capital` 在这里，有专门的边界测试。

### 前端打印的坑
token 在 localStorage 由 axios 拦截器注入，所以 `iframe.src` / `window.open(url)`
**吃不到 Authorization**，会 401。

正确做法：`fetchPrintHtml()` 取回 HTML → 预览用 `iframe.srcdoc`，打印用 `document.write` 到新窗口。
模板局部更新要 `_deep_merge(已存模板, 补丁)` 再套默认值，否则默认值会把用户已存的纸型覆盖掉。

---

## 9. 审计日志

凭证号可复用（作废不留号），所以删除之后凭证号可能被别的凭证占用。
**唯一能还原「原来那张是什么」的东西就是审计快照。**

`audit_helper.voucher_snapshot()` 在增删改时写入完整快照（分录逐条）。
凭证创建、修改、删除、记账、反记账、结账、反结账、整理、导入、年度结转全部留痕。

---

## 10. 数据模型（17 张表）

**账套与授权**
`sys_user` `sys_company` `book` `sys_user_book`（OWNER / ACCOUNTANT / VIEWER）

**账套内数据**（全部带 `book_id`，每个账套独立）
`subject`（树形 + `carry_forward` 标记损益类）`period` `period_opening`

**凭证**
`voucher`（含 `status`、`source`、`period_id`）`entry`

**导入**
`import_batch`（三段式暂存）

**报表**
`report_template` `report_row` `report_col` `report_cell` `report_result`

**打印**
`print_template`

**审计**
`audit_log`

---

## 11. 加新功能的标准动作

### 加一个新的账簿/报表
1. 只调 `LedgerEngine`，不要自己写求和 SQL
2. 报表的话，在 `ReportContext` 上加原语，不要动 `Evaluator`
3. 在 `tests/test_ledger_engine.py` 或 `tests/test_formula.py` 加用例

### 加一张新表
1. `app/models/` 建模型，`book_id` 必带
2. 需要护栏就在 `app/core/db_init.sql` 加触发器
3. `init_db.py` 里注册

### 加一个新打印模板类型
1. `app/printing/preview.py` 加一个 `Virtual` 实现
2. 模板引擎不用改

### 加一条业务规则
先回答三个问题：
1. 它能绕过 API 触发吗？→ 能，就写触发器
2. 它会让已结账的数据算错吗？→ 会，就挂在 `period.locked` 上
3. 它和已有规则冲突吗？→ 冲突的话，先更新这份文档

---

## 12. 测试策略

| 文件 | 覆盖 |
|---|---|
| `test_ledger_engine.py` | 四原语、上卷、方向、试算平衡、冗余列触发器 |
| `test_formula.py` | 公式解析/求值，用 `FakeCtx`，不建库 |
| `test_print_template.py` | 模板校验、渲染、大写金额 |
| `test_periods.py` | 结转/结账/反结账/年度结转（用户点名的四个功能） |
| `test_vouchers.py` | 编号、校验、两态、批量、查询 |
| `test_organize.py` | 断号、重排、两阶段改号 |
| `test_importer.py` | 三段式导入 |

**测试库物理隔离**：`conftest.py` 在 import 任何 `app.*` 之前把 `DATABASE_URL`
改写成 `cwq_test`。必须这么写 —— `app.core.db.engine` 在 import 期就建好连接池了。
每个用例一个全新 uuid 账套，用例之间不会互相污染。

---

## 13. 已知取舍

| 取舍 | 原因 |
|---|---|
| 不做辅助核算 | 需求明确排除。`entry.aux_ref` 字段预留着，需要时能接上 |
| 凭证只有两态 | 需求明确不审核 |
| 期初不物化 | 让反结账能一键完成（见 §3） |
| 结转凭证不参与重编号 | 系统凭证固定排在最后，避免月末重排打乱 |
| 金额用 `Decimal` | 浮点数在财务场景不可接受 |
