# 云财务记账系统

公司内部使用的多账套财务记账系统。FastAPI + PostgreSQL + Vue3 + Element Plus。

一句话架构：**凭证和分录是唯一事实来源，余额、账簿、报表、打印全部由同一套聚合引擎派生。**
因此不存在「某个报表和总账对不上」这种事 —— 它们读的是同一份数据。

---

## 快速开始

### 0. 依赖装好之后，日常起停用 `./dev.sh`

```bash
./dev.sh            # 起数据库 + 后端 8000 + 前端 5173，等到两个端口都应答了才算好
./dev.sh status     # 看在不在跑
./dev.sh stop       # 停掉
```

日志在 `.run/api.log`、`.run/vite.log`。

> 后端/前端是用 Python 的 `os.setsid()` 双 fork 甩出当前进程组的 —— macOS 没有
> `setsid`（Linux 才有）。这样关掉终端、或者进程组被回收，服务都不会跟着没，
> 也就不会再撞上「127.0.0.1 拒绝建立连接」。真要前台跑（要挂 --reload 边改边生效）
> 就还是按下面 2、3 两步手动起。

下面三步是首次安装。

### 1. 起数据库

```bash
docker run -d --name cwq-pg -p 15432:5432 \
  -e POSTGRES_USER=cwq -e POSTGRES_PASSWORD=cwq123 \
  -e POSTGRES_DB=cwq postgres:16
```

首次启动**不会**自动建表：数据库起好后要执行一次迁移（见下面第 2 步的
`alembic upgrade head`）。标准科目表与演示账号由 `scripts/init_db.py` 装载，
同样不是启动时自动做的。

### 2. 起后端

```bash
cd backend
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 首次安装：执行迁移建表（schema 只由这里负责）
.venv/bin/alembic upgrade head

.venv/bin/uvicorn app.main:app --reload --port 8000
```

- 接口文档：http://localhost.8000/docs
- **启动不会建表**：服务启动时只校验 schema 版本，对不上就拒绝启动并打印
  该执行哪条命令。表结构的变更一律写成 `backend/migrations/versions/` 下的
  revision 走评审，不再靠应用启动时「把缺的补上」。

  > 存量库（引入 Alembic 之前建的、且没有 `alembic_version` 的库）需要人工确认后
  > 执行 `alembic stamp 0001_baseline`，不会自动标记 —— 自动 stamp 等于替人断言
  > 「这个库恰好等于 baseline」，而那个断言无法验证。

- 演示数据（标准科目表、演示账号）：`python scripts/init_db.py --reset`

### 3. 起前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

演示账号：`admin / 123456`（管理员）、`zhangsan / 123456`（普通用户）

> 连接串里的端口是 **15432**，与上面 `docker run -p 15432:5432` 一致；
> `backend/.env.example` 里也是这个端口。

登录页的**注册**页签默认**不显示**：自助注册默认是关的，
`config.py` 里 `registration_enabled: bool = False`，`REGISTRATION_ENABLED` 没配或
配成 false 时，调注册接口直接 **403**。

开关有三个来源，**库里优先、env 兜底**（与 OCR、OSS 备份同一套约定）：

| 来源 | 用途 |
|---|---|
| 「系统管理 → 用户与授权」页的**允许自助注册**开关 | 日常开关，管理员点一下即生效 |
| `sys_setting.registration_enabled` | 上面那个开关落库的那一行 |
| `backend/.env` 的 `REGISTRATION_ENABLED` | 库里没有那一行时的兜底默认值 |

关着的时候登录页会把「注册」页签整个去掉，**不只是点了撞 403**。库里没有那一行时读 env，
所以服务器上原有的 `REGISTRATION_ENABLED=false` 会继续生效，升级不会把注册意外打开。

> 兜底默认值仍然是 `False`，它与 `backend/.env.example`、`ARCHITECTURE.md` §9.5
> 必须保持一致，改任何一处都要同步另两处。**不要为了「让文档说得通」去改成 True** ——
> 那等于把刚收掉的口子重新打开。

注册只创建账号，**不授予任何账套**，注册成功也不签发 token ——
需要管理员在「用户与授权」里分配账套。用户名限 3-32 位字母/数字/下划线，密码至少 8 位。

### 4. 生成演示账套（推荐）

```bash
cd backend
.venv/bin/python scripts/seed_demo.py          # 生成
.venv/bin/python scripts/seed_demo.py --reset  # 重建（会先删掉旧的）
.venv/bin/python scripts/seed_demo.py --clean  # 只清理历史测试残留账套
```

生成「启明物业管理有限公司 · 启明物业 2026」账套，内容是**能直接上手看**的真实场景：

- 期初余额 12 条（银行存款 185 万、固定资产 240 万、累计折旧 48 万…）
- 全年 **199 张凭证**：租金收入、物业管理费、代收水电、保洁外包、计提工资、
  办公差旅、计提折旧、缴纳税费、银行结算
- **1~11 月已结转损益 + 已结账**，22 张结转凭证都在，可以直接看结转长什么样
- **12 月业务已录但未结转未结账** —— 正好用来上手试「结转损益 → 结账 → 年度结转」
- 金额全部写死，不随机，每次生成的数据完全一致，便于对着核

运行完会自检并打印勾稽结果：

```
2026-01 试算平衡 ✓  借=1,117,812.00 贷=1,117,812.00
...
资产总计                        4,950,188.00
负债合计                        1,353,129.60
所有者权益合计                   3,534,314.80
  其中 本年利润(1~11月已结转)         522,314.80
加 期末未结转损益(12月)                 62,743.60
勾稽 资产(4,950,188.00) = 负债+权益+期末损益(4,950,188.00)  ✓
```

> **12 月的资产负债表故意不平**，差额恰好等于 12 月当月损益。
> 这是正确行为 —— 没结转的损益还没进本年利润。把 12 月结转损益并记账后即归零。

### 演示动线

| 页面 | 看什么 |
|---|---|
| `/books` | 新建账套、切账套、看每个账套结了几期 |
| `/openings` | **年度**期初录入（跟着顶栏的年度走，切到哪个月都不变）：方向可逐行翻转，试算平衡实时提示 |
| `/vouchers` | 199 张凭证；填了筛选条件就按全部期间筛，分录金额筛的是「某一行」 |
| `/balance` | 科目余额表，顶部绿色「试算平衡 ✓」，借/贷方向正确 |
| `/reports` → 资产负债表 | 年初余额 = 2026 期初，与期初余额录入逐条对得上 |
| `/reports` → 利润表 | 本年累计 585,058.40，与本年利润科目一致 |
| `/periods` | 1~11 月已结账、12 月未结账；四个期末操作都能点 |
| `/vouchers/organize` | 断号体检与重编号（先预览差异再确认） |
| `/print-templates` | 改纸型/页边距，右侧实时预览 |


---

## 功能清单

| 模块 | 路径 | 已完成 |
|---|---|---|
| 凭证 | `/vouchers` | 新增 / 修改 / 删除 / 查询 / 批量 / **拆分**（按分录边界拆成两张）。**记账与反记账不在这里** —— 走「期末结账」的结账与反结账 |
| 凭证整理 | `/vouchers/organize` | 断号体检、重编号、按日期排序重排 |
| 凭证导入 | `/vouchers/import` | Excel 三段式：暂存 → 预览 → 确认入库 |
| 账簿 | `/balance` `/general-ledger` `/detail-ledger` | 科目余额表 / 总账 / 明细账，含试算平衡 |
| 报表 | `/reports` | 资产负债表 / 利润表，**每格公式可配 + 行列可增删改** |
| 打印 | `/print-templates` | 自由模板、纸型方向可调、实时预览 |
| 账套管理 | `/books` | 新建账套、切换账套、看结账进度 |
| 期初余额 | `/openings` | **一个会计年度一份**，跟着顶栏的年度走；按科目分组录入、**借贷实时试算平衡**、Excel 导入导出 |
| 期末结账 | `/periods` | 结转损益 / 结账 / **批量反结账** / 年度结转 |
| 科目表 | `/subjects` | 树形维护，**每个会计年度一份**，跟着顶栏的年度走 |
| 系统 | `/users` `/audit` | 用户授权（OWNER / ACCOUNTANT / VIEWER）、审计日志、**锁定账号的提前解除**。仅管理员可见 |
| 扩展 | `/plugins` / `/plugin/{id}` | 管理员安装签名插件、重启生效、指定使用用户；能力与用户授权分别检查。默认关闭 |
| 数据安全 | `/data-security` | 阿里云 OSS 备份：配置 / 连通性测试 / 立即备份 / 运行历史。**仅管理员可见** |
| 邮件通知 | `/notify-settings` | SMTP 配置 / 测试发送；账号锁定、定时任务失败、备份失败时告警到管理员邮箱。**仅管理员可见** |

---

## 受信任插件（默认关闭）

系统支持签名 `.cwp` 全栈扩展，包括后端 API、预编译前端和独立迁移。插件与宿主同进程、同操作系统权限，**没有沙箱**；只安装经人工审阅、受信任发布者签名的代码。扫描用于拦截明显危险行为，不保证任意代码安全。规范、限制和验收记录分别见 [插件规范](docs/plugin-spec.md)、[风险记录](docs/plugin-risk.md)、[验证记录](docs/plugin-verification.md)。

首次启用前先执行主迁移 `alembic upgrade head`（当前 head 为 `0030_admin_email_mfa`，包含注册表、导入批次和计划任务），在服务环境中配置 `PLUGIN_ENABLED=true`、`PLUGIN_TRUSTED_KEYS`（`key_id → base64 Ed25519 公钥` 的 JSON），并设置持久化的 `PLUGIN_ROOT`。systemd 默认工作目录下为 `backend/data/plugins`，Docker 为 `/app/data/plugins`；部署产物不包含、也不覆盖这个运行目录。Docker 应持久化挂载 `/app/data`。配置项及配额见根目录 `.env.example`。

从仓库根目录可生成仅供本地演示的签名包；生产私钥应保存在独立签名环境，不能复制进服务器或插件目录：

```bash
mkdir -p /tmp/cwq-plugin-demo
backend/.venv/bin/python backend/scripts/plugin_package.py keygen \
  --key-id demo-local --private /tmp/cwq-plugin-demo/signing.pem \
  > /tmp/cwq-plugin-demo/public-keys.json
backend/.venv/bin/python backend/scripts/plugin_package.py build docs/plugin-example \
  --private /tmp/cwq-plugin-demo/signing.pem --key-id demo-local \
  --output /tmp/cwq-plugin-demo/periodnote.cwp
backend/.venv/bin/python backend/scripts/plugin_package.py verify \
  /tmp/cwq-plugin-demo/periodnote.cwp \
  --public-keys /tmp/cwq-plugin-demo/public-keys.json
```

启用插件时必须使用 supervisor 入口，使同一批 worker 继承同一个随机 generation。不要为每个 worker 各自设置 generation。systemd 和 Docker 已接入，手动演练可在 `backend/` 下运行：

```bash
CWQ_API_WORKERS=2 .venv/bin/python scripts/start_server.py --port 8000
# 在另一个终端检查整个 worker 屏障；失败返回非零
.venv/bin/python scripts/plugin_status.py --wait 30
```

管理员在「系统管理 → 扩展管理」上传包，安装结果是“等待重启”。**安装后默认无人获得使用授权，安装者和管理员也不例外**；通过“管理用户”搜索并选择使用者。重启整个服务并通过屏障后，已授权用户才会看到扩展。授权和撤销无需重启，后端按每次请求的当前数据库记录检查；账套读写仍检查原有账套角色。

停用也需要重启。升级、卸载和清理表之前，先停用并重启全部 worker，确认没有旧 lease。升级前生成校验过的本地数据快照，成功后保持停用并保留授权；卸载撤销授权、保留业务表。表清理是单独确认操作。代码/数据库/文件系统没有跨资源原子事务，未完成操作由 journal + reconcile 检测和恢复。恢复必须保留匹配版本的签名包、公钥、registry/迁移现场及数据备份，不能把新版本快照强灌到旧版本。

前端代码资产为不含业务数据、带 digest 的同源只读资源，原生 `import()` 不带 Bearer；manifest 和业务 API 则实时鉴权。不通过 query string 传 token，也不放宽现有 CSP。插件模块加载失败会展示降级页，核心记账页面仍可使用。

SDK 1.2.0 支持由用户在插件页面选择目标账套、保存账套配置、上传文件、持久预览、人工确认和整批原子制证，并增加由宿主调度器按当前有效授权逐用户执行的插件定时任务。可签名运行的示例为 `docs/plugin-import-example/`；契约见 [插件规范](docs/plugin-spec.md) 和 [数据接口](docs/plugin-data.md)。

---

## MCP 只读数据服务

给外部 AI agent（Claude Desktop / Cherry Studio / Dify / 自研 client）一个**查询**账套数据的入口。端点 `/api/mcp`，传输是 Streamable HTTP。agent 问「最近一笔租金是什么时候付出去的」「这个月电费付了吗」「今年收入费用变化」，答案里的数字来自本系统的账套数据，不是模型编的。

**只读。** 工具集里**不存在**任何写工具 —— 建凭证、记账、结账、改科目、导入一个都没有；令牌自身也只有读语义。想让 agent 改数据，这条路走不通。

### 怎么用

1. **签发令牌**（管理员）。令牌绑定一个真实用户，**这个用户能访问哪些账套，agent 就只能访问哪些账套** —— 可见性完全来自用户的账套授权，不是令牌自己带的。签发响应里会回带该用户当前可访问的账套清单，签完当场就能确认 agent 将看到什么。
   > 建议给外部 agent 建一个**只授权单个账套的 VIEWER 用户**，而不是复用 OWNER 账号 —— 绑定 OWNER 意味着 agent 能读该用户名下**所有**账套。
2. **配到 MCP client**。令牌明文**只在签发那一次响应里返回**，库里只存 sha256 摘要，丢了只能吊销重签。Claude Desktop 的配置长这样：

```json
{
  "mcpServers": {
    "cwq": {
      "type": "http",
      "url": "http://<host>/api/mcp",
      "headers": { "Authorization": "Bearer mcp_..." }
    }
  }
}
```

3. **连 `http://<host>/api/mcp`**。端点**必须落在 `/api/` 前缀下** —— 否则会被前端回退（`try_files … /index.html`）吞成 HTML，client 报出来的是「协议解析失败」而不是 404，排查方向整个跑偏。`deploy/nginx/cwq.conf` 里已有专门的 `location ~ ^/api/mcp` 块（SSE 回包不缓冲、读超时 360s）。

⚠️ 生产建议配 `MCP_ALLOWED_HOSTS=你的域名`（如 `acct.example.com:*`）。它开启传输层的 **DNS 重绑定防护**：Host 头不匹配的请求在**传输层**就被挡下（400/421），与「令牌不对」的 401 完全不同 —— 两者要分清。留空 = 关掉这层防护（端点仍然要令牌才能进，防护是第二层），这时外部 agent 能正常连上。

### 工具

| 工具 | 干什么 | 典型问法 |
|---|---|---|
| `list_books` | 列出可访问的账套与年度 | 「我有哪些账套」 |
| `list_periods` | 列出账套的期间与结账状态 | 「哪几个月已经封账了」 |
| `find_subjects` | 按名称 / 类别 / 方向找科目 | 「房屋租赁费是哪个科目」 |
| `search_vouchers` | 凭证检索（分页，带分录明细） | 「去年有笔 12800 的租金凭证」 |
| `latest_payment` | 最近一笔**真付出去**的钱 | 「最近一笔租金是什么时候付出去的」 |
| `check_period_payments` | 核对某期间某类科目的付款情况 | 「这个月电费付了吗」 |
| `ledger_balance` | 科目余额表（含试算平衡） | 「银行存款现在多少」 |
| `income_expense_trend` | 收入 / 费用趋势（损益口径） | 「今年收入费用变化」 |
| `render_report` | 渲染内置报表（资产负债表 / 利润表） | 「今年的利润表长什么样」 |
| `accounting_context` | 账套上下文：当前会计年度、当前期间、科目与凭证数 | 「现在是哪年哪个月」 |

`latest_payment` 与 `check_period_payments` 会把四种状态**分开说**：已付 / 仅计提未付（对方是应付账款）/ 有凭证但未记账 / 本月无记录 —— 「计提了」和「付了」是两回事，混起来会给出错误答案。

每个工具的出参都带 `caliber`（口径标识）与 `notes[]`（人话解释）—— agent 据此知道自己手里的数是在哪个口径下取出来的：

| `caliber` | 是什么口径 | 出现在 |
|---|---|---|
| `navigation` | 账套与年度进度（结账、初始化） | `list_books` |
| `period` | 会计期间与结账状态 | `list_periods` |
| `subject` | 科目表（**年度显式**，跨年 id 不通用） | `find_subjects` |
| `context` | 账套当前上下文（在记的那一年、当前期） | `accounting_context` |
| `voucher` | 凭证明细（默认**只读已记账**） | `search_vouchers` |
| `payment` | 收付判定（对方是不是现金类科目 1001/1002/1012） | `latest_payment` / `check_period_payments` |
| `ledger` | 账簿口径（**含**结转凭证） | `ledger_balance` |
| `report` | 报表口径（**排除**结转凭证） | `income_expense_trend` / `render_report` |

金额一律是**字符串**（两位小数、元、CNY），红字保留负号 —— 二进制浮点会把 `12800.00` 变成 `12800.000000000002`，与界面逐字对不上。

出参有**正式的 JSON Schema**：每个工具在 `tools/list` 里都带 `outputSchema`（字段名英文、描述中文），返回值同时以 `structuredContent` 回传。写 agent 的人靠它知道 `total` 是命中总数还是本页条数、`paid` 与 `accrued_only` 为什么互斥 —— 不用去猜字段的含义。契约的唯一来源是 `backend/app/mcp/schemas.py`。

### 令牌永不过期，所以要定期吊销

这是产品决定：令牌没有到期时间，不会因为时间自动失效。失效开关只有两个 —— **显式吊销**，与**用户侧状态变化**（停用账号、删账号）。

代价是**泄漏窗口是无限的**，所以吊销不是「出问题再说」，是常规动作：

- 签发时**必须写明用途**（`name` 必填），一个 agent 一个令牌 —— 这样能**按 agent 单独吊销**，不用一刀切；
- 每次成功调用都更新 `last_used_at`，列表接口同时给出 `unused_days`，能一眼看出哪些是僵尸令牌、哪些还在被外部 agent 用着；
- 每次工具调用都写 `mcp.read` 审计（记「谁、查了哪个账套、调了哪个工具、结果多少行、耗时」，**不记数据本体**）。现有 HTTP 读接口一律不写审计，而 agent 批量拉数据在现有审计体系里**完全不可见** —— 这是新增的可观测面；
- 改口令**不会**自动作废 MCP 令牌（否则「永不过期」的承诺就落空了）。标准处置是走「批量吊销该用户的全部令牌」这个显式动作；
- 疑似泄漏时的标准动作：立刻吊销该令牌，必要时批量吊销该用户全部令牌。吊销**下一次调用立即生效**（每次调用都重新查库，不做跨请求缓存）。

### 管理接口

都在 `/api/system/mcp-tokens` 下，全部要求管理员：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system/mcp-tokens` | 列出令牌（**不返回明文**）：名称、绑定用户、该用户可访问的账套清单与角色、启用状态、`last_used_at`、`unused_days`、吊销与宽限期 |
| POST | `/api/system/mcp-tokens` | 签发。入参 `name`（必填）/ `user_id`；**明文只在这一个响应里返回一次** |
| DELETE | `/api/system/mcp-tokens/{id}` | **立即吊销**（`revoked_at=now`、宽限期清空、停用） |
| POST | `/api/system/mcp-tokens/{id}/rotate` | 轮换：签发新令牌，旧的进入宽限期（`MCP_TOKEN_ROTATE_GRACE_DAYS`，默认 7 天），**宽限期内旧令牌仍可用** |
| DELETE | `/api/system/mcp-tokens?user_id=...` | 批量吊销某用户的全部令牌（改密、离职、疑似泄漏时的标准处置） |

签发 / 吊销 / 轮换都写审计（`mcp.token.*`），因为这是权限面变更。

上限与开关见 `.env.example` 的 `MCP_*` 段：`MCP_MAX_PAGE_SIZE`（单页上限）、`MCP_MAX_RANGE_MONTHS`（单次查询最多跨几个月）、`MCP_MAX_CALLS_PER_HOUR`（单令牌每小时调用数）、`MCP_AUDIT_READS`（是否记读取审计）、`MCP_STATEMENT_TIMEOUT_MS`（单次调用的库语句超时）。

---

## 业务口径（已定稿，代码按此实现）

这几条是需求的硬约束，改动前请先看 `ARCHITECTURE.md`。

1. **月度结转 = 自动生成结转损益凭证**，一张收入类、一张成本费用类，`source=CARRY_FORWARD`
2. **报表每格公式自由可配**（柠檬云式），不是写死在代码里的模板
3. **不做辅助核算**（无核算项目/客户/供应商维度）
4. **不审核凭证**，只有 `未记账 UNPOSTED` / `已记账 POSTED` 两态
5. **凭证号作废不留号** —— 删除后序号可复用，改完凭证重新打印
6. **批量反结账** —— 从任意期间起一次解锁到最后一个已锁期间，区间凭证自动回未记账
7. **一个账套就是一个完整的记账单元**，公司名只是账套的属性；同一家公司可以开多个账套，每个账套有独立的期间
8. **科目表与期初余额是「每个会计年度一份」** —— 2026 和 2027 各有各的科目表、各有各的期初，
   就像挂在同一个账套名下的两个独立账套。往某一年导入科目表不再受别的年份的凭证影响；
   年度结转会把这张科目表复制到新年度，并按**编码**把期初余额承接过去。
   编码对不上时**拒绝结转并列出差异** —— 宁可报错，也不让钱凭空消失。
9. **期初余额一律「借正贷负」存储**，与账内其它地方同一口径。
   科目属性只是「通常方向」，不是强约束 —— 应收账款出现贷方余额（客户预付挂账）、
   应交税费为负（留抵）都是真实业务，界面按行给方向开关，反方向的行标「反」。
   存、取、Excel 导出再导入三个环节都按实际方位走，往返一次不会把方向洗掉
10. **凭证可以按分录边界拆成两张** —— 在凭证编辑页点「拆分凭证」，选一个分界处，
   后半段分录另起一张新凭证。两张**各自借贷平衡**，有一张不平就拆不成
   （会明确报出差多少，不引入「未平凭证」这种状态）。
   **原凭证继承它自己的号**，新凭证往后拿一个号（后面同期的号顺延 +1）；
   新凭证沿用原凭证的日期与摘要，附单据数留在原凭证。
   已记账、已结账期间、结转损益凭证都不能拆

---

## 数据安全：备份到阿里云 OSS

界面在 **系统管理 → 数据安全**（`/data-security`，仅管理员可见）。填 OSS 参数并点「测试连接」验通后，由应用级调度器执行备份。外部调度服务每分钟调用一次 tick 接口，应用再按数据库中的计划领取到期任务。

### 备份的是什么

每个账套一份 **gzip JSON 逻辑快照**，外加一份系统级快照（用户、授权、审计日志、
系统参数等不属于任何账套的表）。不用 `pg_dump` 分表，因为要的是「能把某一本书原样
搬走」，不是「把整个库搬走」。

产物长这样：

```json
{"header": {"format": "cwq-book-backup", "version": 1,
            "scope": "book", "book_id": 3, "book_label": "某某公司/2026 年度",
            "exported_at": "2026-10-17T03:47:12+08:00"},
 "tables": {"entry": {"columns": [...], "rows": [{...}, ...]}, ...}}
```

对应的对象键是 `acct-backup/monthly/book-3/20261001-031700.json.gz` ——
**档位必须是第一层目录**，因为按档位清理和按目录清理是同一件事。
键里刻意不含中文（公司名写在归档头和运行记录里，界面上看得到），
非 ASCII 键在 `ossutil` 和各种第三方工具的日志里反复出问题。

取的是哪几张表不是「有 book_id 列就算」，而是**沿外键递归到闭包**
（`app/backup/export.py::build_table_scopes`）。原因：`report_row` / `report_col` /
`report_cell` 自己没有 `book_id`，只有 `template_id → report_template.id`。按
book_id 筛表的话，用户设计的那套报表版式**不在任何一个备份里，而且不报错**。
现在账套级 = book + direct + indirect 共 19 张表，系统级 = 其余全部
（direct 取 `book_id IS NULL`，global 取全表）。

`sys_backup_run` 在排除表里（`EXCLUDED_TABLES`），避免运行记录递归膨胀。插件 registry、安装 journal、cohort 和 worker lease 也不作为可移植业务行恢复；用户插件授权仍属于 SYSTEM 快照。

**备份制品里不带凭据。** 制品会离开这台机器——传到 OSS、落在运维的笔记本上、
塞进别人手里。而 `sys_setting` 是整表导出的，于是那几个「拿起来就能用」的值
（OSS AccessKey、OCR token、SMTP 口令）本来会跟着每一份备份走一遍：带着
AccessKey 的备份等于把凭据抄送给了每个拿到备份的人（审计 P0-1）。

所以导出时按白名单把这几个键的值**写空**（行还在，页面上看得到那个键，
一眼就知道"恢复后要重填"）。代价是**恢复之后这几个凭据要重新配一遍**——
`scripts/backup_to_oss.py --restore` 会在输出里明确列出还要重填哪几项。
调度密钥不在此列：它存的是摘要，且必须跟着恢复走，否则恢复后所有定时任务
会静默停摆。

没有插件元数据时继续写 v1；包含插件元数据/表时写 v2，增加 digest、独立迁移 head 和实时表结构。v1 reader 保留。插件数据只在目标库已安装完全匹配的代码、迁移和表结构时允许恢复，未知插件、卸载状态及任何漂移均在写入前拒绝。详见[插件备份约束](docs/plugin-spec.md#备份与恢复)。

### 分级：够格的最高一档

```
这个月还没成功过月度  ->  monthly（永久保留）
本周里还没成功过周备份 ->  weekly （保留 365 天）
其余                 ->  daily  （保留 30 天）
```

判重只看 `status='success'` —— **失败不算占位**。一次失败的月度备份如果算成
「本月已留档」，接下来一个月都不会再补，月度档就悄悄空了一个月。

判重用 `Asia/Shanghai`（`tier.BUSINESS_TZ`），不依赖部署机的 TZ：机器在 UTC 上
跑，周一 00:00 UTC 是周日的 8 点，按机器时区判会整周错位。

### 保留：月度是结构性的，不是配出来的

`.env` 里只有 `BACKUP_KEEP_DAILY_DAYS` / `BACKUP_KEEP_WEEKLY_DAYS`，
**月度备份没有对应配置项**。`app/backup/retention.py` 对 `tier == monthly`
直接 `continue`（连对象列表都不列），`plan_deletions` 入口再挡一次 ——
将来谁新写一条清理路径，也碰不到月度档。

做成「保留天数 = 一个很大的数」是危险的：哪天有人觉得 3650 太大改成 1095，
「永久」就悄悄变成了 3 年，而且没人会发现。

清理按 `daily/` `weekly/` 前缀 + 对象修改时间判定，**跑完当轮备份立即执行**
（不单独排一个清理任务）。systemd unit 里那个 `--no-prune` 不是可选的 ——
留着过期产物不只是浪费存储费，还让人以为回溯窗口比实际更长。

### 凭据只写不读

`GET /system/backup` 永远不返回 AccessKey 明文，只回 `…EYID` / `…0000`
这样的 mask 提示。改密钥只能整份重填，没有「回显」这条路
（和「系统管理 → 外部服务」里的 OCR token 同一套规矩）。
`OSS_PUBLIC_KEYS` 里根本没有那两个键。

### 出事了怎么恢复

```bash
# 1. 先干跑：只校验归档并报行数，一个字节都不写库
.venv/bin/python scripts/backup_to_oss.py \
    --restore /tmp/book-3-20261001.json.gz --restore-dry-run

# 2. 确认无误再灌（整批一个事务，中途失败整体回滚）
.venv/bin/python scripts/backup_to_oss.py \
    --restore /tmp/book-3-20261001.json.gz
```

`read_snapshot` 先验后跑：format / version 不认、tables 为空，一律拒绝。
恢复是**灾难恢复，不是覆盖写** —— 往一个有数据的库里灌会撞主键并整体回滚
（有测试锁住这个行为）。也就是说「恢复」永远是「恢复到一个空库/新环境」。

恢复完 `_reset_sequence` 会把序列推到当前最大值之后，不然恢复之后第一张
新凭证就会撞主键；`_ordered_rows` 会对自引用外键（`subject.parent_id`）
做表内拓扑排序，否则多行 INSERT 逐行校验 FK 时父行还没插进去。

### 启用定时调度

```bash
# 先把主迁移升级到 0030_admin_email_mfa
cd backend
.venv/bin/alembic upgrade head
```

然后用管理员令牌调用 `POST /api/system/scheduler/secret/rotate`，将响应中的明文密钥只保存到外部调度服务的密钥配置中。外部服务每分钟请求：

```text
GET https://<域名>/api/scheduler/tick?key=<调度密钥>
```

接口只接受当前密钥或轮换期间保留的上一把密钥；数据库只保存 SHA-256 摘要，轮换完成后用 `DELETE /api/system/scheduler/secret/previous` 关闭旧密钥。提供的 Nginx 配置会关闭该路径及其末尾斜杠形式的访问日志，但云负载均衡、CDN、外部调度平台仍可能记录 URL，必须在这些位置开启查询参数脱敏。不要把密钥放进普通脚本、聊天或监控标签。

tick 返回 `202` 表示本次任务领取已接受，最终结果通过 `GET /api/system/scheduler/runs` 查看。内置 OSS 备份每天北京时间 03:47 执行，沿用数据安全页面的 `backup_enabled` 开关、OSS 参数和保留策略；插件导入过期文件每小时清理一次。每次最多领取 20 个任务，同一任务租约有效时不会重复领取，错过的周期合并执行。

插件任务默认关闭，管理员可通过 `GET /api/system/scheduler/tasks` 查看，并用 `PUT /api/system/scheduler/tasks/{plugin_id}/{task_name}` 传入 `{"enabled": true}` 启用。启用是全局开关；每次运行按当时仍 active 且未撤销插件授权的用户逐个执行。旧的 `cwq-oss-backup.timer` 不应再启用，否则会产生重复备份；旧的本地 pg_dump timer 仍保持禁用。

### 调度看门狗：谁来看门狗

上面的 tick 由**站外的调度服务**驱动。它一停（配额到期、配置丢了、平台故障），
tick 就不来，任务不被领取，**所有定时任务静默停摆**——而这件事**应用自己发现不了**：
它只在被 tick 到的时候才醒着，那天夜里服务器安静、进程健康、日志干净，
一切正常，除了备份没产出。

所以有一个独立于应用的看门狗（`cwq-scheduler-watchdog.timer`，每 15 分钟一次）：
它查「有任务到期却没人领」，有就发告警邮件，并留一条 `SCHEDULER_TASK_MISSED`
审计记录（「操作日志」页可见）。发布脚本默认会装上并启用它：

```bash
systemctl status cwq-scheduler-watchdog.timer
journalctl -u cwq-scheduler-watchdog.service     # 看它查到了什么
```

不想让它发信就放标记文件 `/opt/cwq/.watchdog-timer-off`（发布脚本认得这个状态，
下次发版不会把它拉回来）。容忍期用 `CWQ_WATCHDOG_GRACE_MINUTES` 调（默认 30 分钟：
tick 每分钟调一次，短暂维护不该被误报，而真挂了半小时内就会被发现）。

**看门狗只管"压根没跑"**。跑了但失败的事，调度器自己会发告警（`TASK_FAILED`）。

### 手动跑一轮

```bash
cd backend
.venv/bin/python scripts/backup_to_oss.py --dry-run    # 只打印会传什么、会删什么
.venv/bin/python scripts/backup_to_oss.py              # 真跑
.venv/bin/python scripts/backup_to_oss.py --book 3 --tier monthly   # 补一份永久档
```

退出码：`0` 至少一本成功；`2` 配置不全 / 没东西可备；`4` 一本都没成功。
一轮全灭必须是非零 —— 返回 0 的表现就是「定时任务每天都是绿的」。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system/backup` | 读配置（**无 AccessKey 明文**）+ 开关 + 保留天数 |
| PUT | `/api/system/backup` | 改配置（凭据留空 = 不动已存的值） |
| POST | `/api/system/backup/test` | 连通性测试：上传一个探针对象再删掉 |
| POST | `/api/system/backup/run` | 立即备份（后台任务 + 轮询历史，页面不靠超时猜） |
| GET | `/api/scheduler/tick?key=...` | 外部每分钟触发应用级计划任务 |
| GET | `/api/system/scheduler/tasks` | 管理员查看内置与插件计划任务 |
| PUT | `/api/system/scheduler/tasks/{plugin_id}/{task_name}` | 管理员启用/停用插件计划任务 |
| GET | `/api/system/scheduler/runs` | 管理员查看计划任务运行记录与逐用户结果 |
| GET | `/api/system/backup/runs` | 运行历史（状态 / OSS key / 字节数 / SHA256 / 行数 / 错误全文） |

五个都要求管理员。

「测试连接」是**写探针再删**，不是 `head_bucket`：只读权限的 Key 在
`head_bucket` 上是绿的，真备份时却写不进去 —— 那才是最难在事后发现的形态。

---

## 登录锁定与告警通知

### 递进式锁定

连续登录失败 **3 次**锁定账号，**首次锁 10 分钟**；之后每多撞一轮锁定
翻一档（10 → 20 → 30 …，上限 `LOGIN_LOCK_MAX_MINUTES`，默认一天）。
登录成功或管理员提前解封后，轮次清零。

```
LOGIN_MAX_FAILURES=3
LOGIN_LOCK_MINUTES=10
LOGIN_LOCK_MAX_MINUTES=1440
```

**为什么要递进。** 恒定时长挡不住两种攻击：**慢速爆破**（每隔 9 分钟
试一次、每次只错一次，永远不触发阈值）和**故意撞锁**（把每次尝试安排
在锁定窗口之外，让账号被无限期挡在门外）。递进让「持续在撞」的代价
逐轮上升。

**为什么有上限。** 没有上限的递进就是**永久锁定** —— 等于给攻击者一个
禁用任意账号的开关。上限让它停在「很久但有限」。

**代价要知道**：阈值收得紧意味着正常用户输错三次就被挡 10 分钟。
管理员在「用户与授权」页点「解锁」即可提前放行，不必等满 —— 这条
出口同时也是递进式锁定可被用来拒绝服务时的对冲手段。

锁定状态在用户列表里可见（第几轮 / 还剩多久），解封会留审计
（`UNLOCK_USER`）。

### 邮件通知

界面在 **系统管理 → 邮件通知**（`/notify-settings`，仅管理员可见）。
配好 SMTP 后，以下事件会发信到管理员指定的收件邮箱：

| 事件 | 内容 |
|---|---|
| 账号锁定 | 账号名、来源 IP、锁多久、第几轮 |
| 定时任务失败 | 任务名、错误码、错误信息 |
| 计划任务没跑 | 任务名、本该运行时间、逾期多久（tick 没人来领） |
| 备份失败 | 范围、错误信息 |
| 备份脚本失败 | systemd 定时备份（`.sql` / OSS）的失败原因与退出码 |

**两条备份路径，告警各挂各的。** 账套数据备份有两条独立的路：本机
`.sql` 备份（`cwq-backup.timer` → `scripts/db_backup.py`）与 OSS 备份
（应用内调度器 `system:oss_backup`）。前者**不经过应用**，所以告警由脚本
自己发 —— 只在调度器里挂告警，那条路径失败时就是一声不吭。两条路径各断
各的，告警也各用各的限流桶：一边刷屏不会把另一边一起压掉。

**审计表是证据通道，邮件是检测通道。** 这几件事也都会写进「操作日志」，
区别在于操作日志要人去翻，邮件是当时就到。邮件发不出去**不影响业务
本身**（只会在服务端日志里留一条记录）。

收件人填多个时，第一个进 `To` 头、其余走密送 —— 收件人之间**互相看不见
对方的地址**。收件人清单以原始文本存储，保存时才逐个校验，**写错一个
不会连累其他地址**（只跳过那一个）。

**告警限流。** 邮件通道和「操作日志」最本质的区别是：后者只有管理员能写，
前者**任何人都能触发**。攻击者对一批账号各试三次错误口令，就能让系统发出
成百上千封「账号锁定」告警 —— 管理员的邮箱被灌满（真告警淹没在里面看不
见），邮件服务商那边这个发件域名也会被判成垃圾邮件源，于是连**真正的**
告警都发不出去了。所以同一类告警在一个窗口内最多单独发 `NOTIFY_RATE_LIMIT`
封（默认 20 封 / 10 分钟，按**事件类型**分桶，锁定告警刷屏不会连带压掉
「备份失败」）。

**「测试发送」会直接告诉你哪里配错了**，而不是笼统的"发送失败"：端口与
加密方式不匹配（465 要 SSL、587 要 STARTTLS、25 不加密）、服务器不提供本
客户端支持的认证方式、口令错、连不上——各给各的处置方向。配 SMTP 的人多半
不是邮件专家，笼统的报错等于让人瞎试。

超出的告警不发信，但**计数留着** —— 下一封真能发出去时，正文里会补一句
「另有 N 条同类告警未单独通知」。全量明细始终在「操作日志」里。

SMTP 口令与 OCR token、OSS AccessKey 同一套规矩：**只写不读**，界面上
改完再也读不回来，只能覆盖重填。

| 接口 | 说明 |
|---|---|
| GET | `/api/system/notify` | 读配置（**没有口令明文**，只有 `password_set` 与末 4 位） |
| PUT | `/api/system/notify` | 改配置（口令留空 = 不动已存的值） |
| POST | `/api/system/notify/test` | 发一封测试信到真实收件地址 |
| POST | `/api/system/users/{id}/unlock` | 提前解除锁定（管理员） |

---

## 测试

```bash
cd backend

# 日常开发：只跑「你这次改动影响到的用例」（按真实导入关系算，不是猜）
.venv/bin/python scripts/test_changed.py         # 打印受影响的测试文件 + 建议命令
.venv/bin/python scripts/test_changed.py --run   # 直接跑
.venv/bin/python scripts/test_changed.py --base main   # 跟 main 比

# 提交前：全量（默认 -n 6 并行，本机 10 核实测 60~80 秒）
.venv/bin/python -m pytest

.venv/bin/python scripts/demo_e2e.py            # 69 项端到端流程
.venv/bin/python scripts/demo_modules.py        # 53 项模块冒烟
node onboard.mjs                                # 19 步浏览器开户全流程（需前后端都起着）
node report_design.mjs                          # 7 步浏览器报表设计器（需前后端都起着）

cd ../frontend && npm run test                  # 全量前端测试（node --test）
```

- 插件专项及完整回归命令见 [验证记录](docs/plugin-verification.md)。真实浏览器演练使用专用 `*_plugin_browser_test` 库和临时签名密钥，不能传业务库连接串。
- pytest 用**独立测试库** `cwq_test`，每个用例一个全新账套，绝不碰开发库
- **每条用例之间会清一次库**（`conftest` 的 `_clean_slate`）：让每条用例都从同一个干净状态出发，
  也让插件备份这类「按库里账套数线性变慢」的操作不再拖垮整轮。
  迁移种下的官方基线数据（内置报表模板、打印模板）会被保留。
  想看「不清理」的样子：`CWQ_TEST_KEEP_DATA=1 pytest`
- **命令行上不要再加 `-q`**：`pytest.ini` 的 `addopts` 里已经有一个 `-q`，
  再加一个就变成 `-qq`，pytest 9 会把结尾「N passed in Xs」那行**整个吞掉** ——
  跑完一轮看不到结论
- 两个 demo 脚本每次运行自建 uuid 账套，可以反复跑
- `onboard.mjs` 跑真实浏览器：新建账套 → 初始化两步向导 → 录期初（含反方向余额）
  → 试算平衡 → 保存回读 → 导出再导入 → 下载文件鉴权 → 拖拽磁贴排序并验证刷新后仍在
- 跑完用 `seed_demo.py --clean` 收掉测试账套

---

## 目录结构

```
backend/
  app/
    accounting/     # 业务服务：凭证、期间、结转、整理、导入
      vouchers.py   #   凭证 CRUD + 记账/反记账 + 查询
      periods.py    #   结账、批量反结账、结转损益、年度结转
      organize.py   #   整理凭证（两阶段改号）
      importer.py   #   Excel 导入（三段式）
    ledger/         # 账簿聚合引擎 —— 全系统唯一的算账逻辑
      engine.py     #   四原语 + 上卷
      queries.py    #   总账 / 明细账 / 余额表 / 试算平衡
    reports/        # 报表公式引擎（自由配公式）
    printing/       # 打印模板引擎（HTML+CSS @page）
    backup/         # 账套快照 → OSS：分级 / 保留清理 / v1、v2 导出与恢复
    plugins/        # 签名、安装 journal、worker 屏障、SDK、授权、独立迁移
    api/            # HTTP 层，只做参数转换和错误翻译
    models/         # 宿主 ORM 和插件注册/授权/运行记录
    core/           # 配置、连接、鉴权、初始化 SQL
    seed/           # 标准科目表、内置报表模板
  scripts/          # seed_demo.py 演示数据 + demo_e2e/demo_modules 端到端脚本
                   # + db_backup.py（pg_dump）+ backup_to_oss.py（账套快照）
  tests/            # pytest
frontend/
  src/views/        # 按业务模块分目录
  src/api/          # axios 封装，token 拦截器
```

## 技术选型理由

- **PostgreSQL**：会计护栏（结账写保护、借贷平衡、冗余列同步）全部落在触发器里。
  绕过 API 直接改库也改不动，这是财务系统最需要的性质。
- **Decimal 而非 float**：金额绝不能用浮点。
- **期初余额是推导值**：平时不物化任何快照，改上游数据余额自动跟随。
  - **快照只有两处**：账套启用时用户录入的期初、年度结转时结转到次年 1 月的期初。
  - **结账不会冻结期初快照，反结账也不会删快照**：反结账只做三件事 —— 解锁期间、
    把区间内已记账凭证退回未记账、写一条审计。所以「批量反结账」能一键完成，
    不是因为它会「清掉快照」，而是因为**期间级快照压根不存在**。
