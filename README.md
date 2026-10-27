# 财务记账系统

> 面向中小企业财务人员的在线会计系统。
>
> 一套账从期初建账、凭证记账、月末结转损益、期末结账，一路走到总账、明细账、资产负债表与利润表，全链路在同一个系统里闭环；在此之上提供受信任插件扩展，以及给外部 AI 用的只读 MCP 数据服务。

---

## 目录

- [它能做什么](#一它能做什么)
- [技术架构](#二技术架构)
- [快速开始](#三快速开始)
- [常用命令](#四常用命令)
- [仓库结构](#五仓库结构)
- [HTTP API](#六http-api)
- [配置](#七配置)
- [插件系统](#八插件系统)
- [MCP 只读数据服务](#九mcp-只读数据服务)
- [计划任务](#十计划任务)
- [测试](#十一测试)
- [CI](#十二ci)
- [部署](#十三部署)
- [桌面客户端](#十四桌面客户端)
- [文档索引](#十五文档索引)

---

## 一、它能做什么

### 会计主线

系统以 **(账套, 会计年度)** 为记账单位，而不是笼统的「一个账」。同一个账套的科目表按年度各有一份，跨年要年度结转，期初余额在启用和结转两处落地。

| 环节 | 能做什么 |
| --- | --- |
| **建账** | 新建账套、设定启用年月、生成全年会计期间、安装科目表 |
| **期初** | 手工录入期初余额，或从用友 U8 / 畅捷通导出的 `.xls` 导入 |
| **凭证** | 增删改查、记账 / 反记账、批量删除、拆分、导入 Excel、发票批量录入 |
| **整理** | 按规则批量整理凭证（两阶段 plan / apply，先看方案再落库） |
| **结账** | 结转损益 → 结账、反结账（支持批量）、年度结转 |
| **账簿** | 科目余额表、总账、明细账、试算平衡 |
| **报表** | 资产负债表、利润表模板 + 自定义模板设计与公式编辑 |

**结转损益**是这套系统里最讲究的一处：

- 「哪些科目参与结转」只有一份定义（`app/accounting/periods.py`），结转和结平校验共用它。写成两份的直接后果是 —— 本年利润一旦被标成结转科目，那个期间就永远结不了账。
- 只有损益类科目能结转，且**永远排除本年利润** —— 它是结转的目的科目，不是被结转的对象。
- 每个损益科目都能单独关掉结转（`carry_forward`），允许「故意不结转」。
- 本年利润科目可以手工指定，不靠编码去猜。

### 数据进账

| 来源 | 方式 |
| --- | --- |
| Excel 凭证 | `.xlsx` 导入，上限 20MB / 5000 张凭证 |
| 期初余额 | 手工录入，或用友 U8 / 畅捷通 `.xls` 导入 |
| 报表模板 | 用友 UFO 5.10 / 8.00 的 `.rep` / `.rbk` 模板迁移 |
| 发票 | PDF 批量上传 → 文本层提取（可选 OCR）→ 字段解析 → 生成凭证草稿 |
| 插件 | 插件自己的导入通道，走宿主统一的上传与审批流程 |

### 报表与打印

- **报表**：内置资产负债表、利润表；支持模板复制、改行列、改公式、手工改单元格、导出 Excel。
- **公式引擎**：手写的递归下降解析器，**刻意不用 `eval`** —— 报表公式来自用户输入，`eval` 在这里等于一个可执行注入面。
- **打印**：HTML + CSS 渲染，屏幕预览即打印结果。导出 `.html` 与 `.xlsx` 两条通道，共用同一份「账簿 → 打印行」适配与合计/试算逻辑。PDF 走浏览器「另存为 PDF」，服务器上不维护字体包和排版引擎。

### 平台能力

| 能力 | 说明 |
| --- | --- |
| **多账套隔离** | 用户按账套授权，角色分 OWNER / ACCOUNTANT / VIEWER，写操作统一在 `require_book_write` 处拦截 |
| **审计日志** | 关键操作全量留痕，管理员可查 |
| **登录防护** | 账号渐进锁定（前两轮 5 → 10 秒**递增延迟**，第 3 轮起转入**分钟级硬锁**：30 → 40 → 50 …，上限 `LOGIN_LOCK_MAX_MINUTES`）+ IP 节流 + 可信代理解析 |
| **二次验证** | 管理员邮箱 MFA：验证、轮询、重发、重新认证 |
| **数据安全** | 账套数据导出 → gzip → SHA256 → 阿里云 OSS，按日/周档位保留。制品的**静态加密由 OSS 服务端加密（SSE）承担**（桶上的默认加密规则，2026-10-26 起本项目不再在客户端加密）；SHA256 只是**校验和**，不提供机密性 |
| **邮件通知** | SMTP 事件通知，带发送频率限制 |
| **计划任务** | 外部 cron 驱动，租约领取 + 咨询锁，支持插件注册自己的周期任务 |
| **插件扩展** | 受信任插件：自带后端接口、前端页面、数据表、账套级配置、hook、定时任务 |
| **MCP 服务** | 只读数据服务，10 个工具，给外部 AI agent 查账 |

---

## 二、技术架构

| 层 | 选型 |
| --- | --- |
| 前端 | Vue 3.5 + Vite 6 + Element Plus 2.9 + Pinia 2 + vue-router 4（hash 模式） |
| 后端 | FastAPI + SQLAlchemy 2.0 声明式（`Mapped[]`）+ Pydantic Settings |
| 数据库 | PostgreSQL 16 + Alembic（35 个迁移版本） |
| 认证 | JWT Bearer + `pbkdf2_sha256` 口令散列 + 可选管理员 MFA |
| 插件 | 自研 SDK（`API_VERSION = 1.2.0`），Ed25519 全包签名 |
| AI 集成 | MCP 2.3（Streamable HTTP，只读） |
| 桌面端 | PyInstaller 打包的 Windows 壳，零第三方运行时依赖 |

### 两个值得知道的设计决定

**账簿聚合只有一份实现。** `app/ledger/engine.py` 是全系统唯一的账簿引擎，抽象成三个原语：期初、借方累计、贷方累计，期末 = 期初 + 借 − 贷。余额表、总账、明细账、试算平衡、报表取数全部从这里出，所以这几种账不可能互相打架。

**报表取数有两个口径，差异恰好是结转凭证。** `for_ledger` 与 `for_report` 刻意分开 —— 结转损益生成的凭证是系统生成的，不属于用户的日常业务，所以在报表口径里要剔掉，否则利润表会被自己的结转凭证算两次。

---

## 三、快速开始

### 环境要求

| 依赖 | 版本 |
| --- | --- |
| Python | 3.11+ |
| Node.js | 22+ |
| PostgreSQL | 16 |
| Docker | 需要（本地跑数据库容器 `cwq-pg`） |

### 拉起开发环境

```bash
# 1) 后端依赖
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cd ..

# 2) 准备配置
cp .env.example backend/.env
#    至少把 DATABASE_URL 指到本地 PostgreSQL，
#    并把 JWT_SECRET 换成一个真实随机值

# 3) 建表 + 护栏 + 内置报表 + 演示数据
cd backend
.venv/bin/python scripts/init_db.py --reset
cd ..

# 4) 起环境（数据库 + 后端 :8000 + 前端 :5173）
./dev.sh
```

打开 <http://127.0.0.1:5173>。

`init_db.py` 会播种两个账号：`admin`（管理员）与 `zhangsan`（普通用户），密码均为 `123456`。**上线前必须改掉。**

想先看一份填满数据的账套（1~11 月已结转已结账、12 月未结转未结账，正好可以上手试结转损益 → 结账 → 年度结转）：

```bash
cd backend && .venv/bin/python scripts/seed_demo.py
```

### 关于 `dev.sh`

四个子命令：

```bash
./dev.sh          # 启动（默认）
./dev.sh stop     # 停掉
./dev.sh status   # 看状态（真的发请求，不靠 pid 文件猜）
./dev.sh doctor   # 诊断并修复「端口在监听但没人应答」
```

几个刻意为之的行为：

- **预检在进程起来之前。** schema 版本对不上、数据库连不上，直接报错退出，不留一个「端口在监听但没人应答」的僵尸进程。
- **启动成功以探到应答为准。** 探不到就清理、返回非零，绝不打印「好了」。
- **停是 TERM → 等 → KILL 整个进程组。** uvicorn `--reload` 的 reloader 父进程阻塞在 `os_waitpid` 时，单发 SIGTERM 杀不掉。
- **开了插件就不热重载。** `PLUGIN_ENABLED` 为真时改走 `scripts/start_server.py`（supervisor），因为插件的 worker 屏障依赖启动代号，`--reload` 每次重载都换 worker，屏障永远凑不齐。改后端代码就 `./dev.sh stop && ./dev.sh`。

---

## 四、常用命令

### 后端

```bash
cd backend

# 全量测试（xdist 并行，默认 -n 6）
.venv/bin/pytest

# 只跑改动相关的用例
.venv/bin/python scripts/test_changed.py

# 数据库迁移
.venv/bin/alembic upgrade head

# 启动（开发）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app

# 启动（多 worker，与生产同构）
.venv/bin/python scripts/start_server.py --host 127.0.0.1 --port 8000 --workers 2
```

### 前端

```bash
cd frontend

npm run dev      # 开发服务器 :5173（/api 代理到 :8000）
npm run build    # 构建到 dist/
npm run preview  # 预览构建产物
npm test         # node --test tests/*.test.mjs
```

### 其他

```bash
# 插件状态检查（等待 worker 屏障）
cd backend && .venv/bin/python scripts/plugin_status.py --wait 30

# 计划任务看门狗
cd backend && .venv/bin/python scripts/scheduler_watchdog.py

# 账套数据备份到阿里云 OSS
cd backend && .venv/bin/python scripts/backup_to_oss.py
```

### 审计修复的取证命令

2026-10-25 那次审计的修复状态**不靠文档说了算**，靠这几条命令（退出码即结论）：

```bash
# ① 审计报告 §7 路线图 50 项：逐项找代码证据，找不到就是 FAIL
#    退出码：0 全过 / 1 有项目 FAIL（这一条已经接进 CI 的 backend job）
python3 tools/audit_roadmap_check.py

# ② 插件沙箱的四条复核主张：不成立 = 0；--mutate 加跑变异证明
#    （把防线改回旧行为，确认对应用例会红）
python3 tools/audit_plugin_sandbox_recheck.py --mutate

# ③ 上线前的存量数据体检（**只读**，读 .env 里那个库）
#    退出码：0 通过 / 2 阻断（重复分录行号，不能上线）/ 3 提示 / 1 体检自己没跑成
cd backend && .venv/bin/python scripts/data_health_check.py
```

逐条落点与机制偏离见 [`docs/audit-remediation-2026-10-25.md`](docs/audit-remediation-2026-10-25.md)，
存量数据体检的用法与退出码含义见该文档 §1.1。

---

## 五、仓库结构

```
.
├── backend/                  FastAPI 后端
│   ├── app/
│   │   ├── api/              路由层（12 个 router + schemas）
│   │   ├── accounting/       会计业务服务（凭证、期间、年度、初始化、整理、导入）
│   │   ├── ledger/           账簿聚合引擎与查询
│   │   ├── reports/          报表公式引擎、求值、模板导入
│   │   ├── printing/         打印模板、Excel 生成、预览适配
│   │   ├── invoice/          发票解析、OCR、存储
│   │   ├── plugins/          插件加载、安装、签名、权限、hook、worker
│   │   ├── mcp/              MCP 服务与 10 个工具
│   │   ├── scheduler/        计划任务调度
│   │   ├── backup/           备份、保留策略、OSS 客户端
│   │   ├── notify/           邮件通知与 MFA
│   │   ├── seed/             内置报表、用友 422 科目表、凭证号
│   │   ├── models/           SQLAlchemy 模型
│   │   └── core/             配置、数据库、鉴权、锁定、schema 版本、审计
│   ├── migrations/           Alembic 迁移（35 版）
│   ├── scripts/              初始化、播种、备份、预检、supervisor、插件工具
│   └── tests/                pytest 用例
├── frontend/                 Vue 3 前端
│   └── src/
│       ├── views/            页面组件
│       ├── components/       通用组件（含打印相关）
│       ├── router/           路由与导航结构
│       ├── stores/           Pinia（session / tabs）
│       ├── composables/      组合式逻辑
│       ├── api/              接口封装
│       ├── utils/            金额、纸张、打印模板、拼音搜索等
│       └── plugins/          插件前端运行时
├── plugins/                  内置插件源码
│   ├── com.cwq.periodic/     周期记账
│   └── com.cwq.reimburse/   民宿报销
├── .minimax/skills/          给 AI agent 用的项目级 Skill
│   └── cwq-accounting-mcp/   本项目 MCP 的查询口径与调用纪律
├── deploy/                   发布脚本、systemd 单元、nginx 配置、CDN 部署指南
├── tools/
│   ├── uitest/               Playwright 一次性 UI 探测脚本（不是回归测试）
│   └── desktop-shell/        Windows 桌面壳
├── docs/                     插件文档、模板导入样例
├── dev.sh                    开发环境一键脚本
└── ARCHITECTURE.md           架构说明（细节级，长文档）
```

---

## 六、HTTP API

所有业务接口以 `/api` 为前缀，用 JWT Bearer 认证。`{book_id}` 是账套 ID。

| 前缀 | 业务域 |
| --- | --- |
| `/api/auth` | 登录、注册、`/me`、改密码、MFA（verify / poll / resend / reauth） |
| `/api/system` | 用户与账套授权、审计日志、MFA、登录防护、注册开关、OCR、邮件通知、备份、计划任务 |
| `/api/system/mcp-tokens` | MCP 令牌签发 / 轮换 / 吊销 |
| `/api/system/plugins`、`/api/plugins/*` | 插件安装、启用、升级、卸载、用户授权、清单与资产 |
| `/api/books` | 账套、初始化、期初余额、期间、结转损益、结账 / 反结账、年度结转 |
| `/api/books/{book_id}/subjects` | 科目树、扁平列表、常用科目、增改删、科目导入 |
| `/api/books/{book_id}/vouchers` | 凭证增删改查、记账 / 反记账、批量删除、整理、导入、拆分、期间检查 |
| `/api/books/{book_id}/invoices` | 发票配置与规则、批次上传 / 预览 / 提交 / 作废 |
| `/api/books/{book_id}/ledger` | 余额表、总账、明细账、凭证余额 |
| `/api/books/{book_id}/print` | 打印模板、凭证 / 余额表 / 总账 / 明细账 / 报表的 `.html` 与 `.xlsx`、预览 |
| `/api/reports` | 报表模板、设计与渲染、单元格、行列增删改、复制、模板导入 |
| `/api/scheduler/tick` | 外部 cron 触发入口（HMAC 密钥校验） |
| `/api/mcp` | MCP 只读服务（Streamable HTTP，stateless） |
| `/api/health`、`/api/ready` | 存活 / 就绪探针 |

**接口文档在生产环境默认关闭** —— `/openapi.json` 不是一张无害的静态图，它是一份完整的路由表加每个字段的约束，等于主动告诉未授权方「这个系统有哪些入口、各自要什么」。开发环境可用 `/docs` 查看。

`/api/health` 只证明进程活着，不碰数据库；`/api/ready` 才会真查库并校验 schema 版本。

---

## 七、配置

配置来自环境变量与 `backend/.env`，完整清单见 `.env.example`（103 个键）。按类别摘录：

| 类别 | 关键变量 |
| --- | --- |
| 运行环境 | `APP_ENV`（dev / prod）、`LOG_LEVEL`、`DATABASE_URL`、`DB_POOL_*` |
| 认证 | `JWT_SECRET`、`JWT_ALGORITHM`、`ACCESS_TOKEN_MINUTES`、`ADMIN_TOKEN_MINUTES` |
| 登录防护 | `LOGIN_MAX_FAILURES`、`LOGIN_LOCK_MINUTES`、`LOGIN_LOCK_MAX_MINUTES`、`LOGIN_IP_*`、`TRUSTED_PROXIES` |
| 插件 | `PLUGIN_ENABLED`、`PLUGIN_ROOT`、`PLUGIN_TRUSTED_KEYS`、`PLUGIN_REVOKED_KEYS`、`PLUGIN_MAX_*`、`PLUGIN_LEASE_SECONDS`、`PLUGIN_ISOLATION` |
| 导入与发票 | `IMPORT_MAX_BYTES`、`IMPORT_MAX_VOUCHERS`、`INVOICE_STORAGE_DIR`、`INVOICE_OCR_*` |
| 通知与 MFA | `SMTP_*`、`NOTIFY_*`、`ADMIN_MFA_*` |
| 备份 | `OSS_*`、`BACKUP_ENABLED`、`BACKUP_KEEP_DAILY_DAYS`、`BACKUP_KEEP_WEEKLY_DAYS` |
| MCP | `MCP_ENABLED`、`MCP_MAX_PAGE_SIZE`、`MCP_MAX_RANGE_MONTHS`、`MCP_MAX_CALLS_PER_HOUR` |
| 跨源（动静分离） | `CORS_ALLOW_ORIGINS`（**默认空 = 不开 CORS**；只有前端与 API 不同源时才配） |

生产环境的配置校验是 **fail-closed**：缺关键项或仍在用默认密钥就拒绝启动。开发环境用 `APP_ENV=dev` 跳过。

`DATABASE_URL` 一律由应用自己的 settings 读取 —— `alembic.ini` 里**故意不写** `sqlalchemy.url`。写死一份就多出一个可能和实际配置对不上的地方，而「迁移打在哪个库上」对不上，破坏性变更打错库的后果无法挽回。

---

## 八、插件系统

### 契约

当前公开契约为 **SDK 1.2.0 / CWP 1**。

| 部件 | 必需 | 用途 |
| --- | --- | --- |
| `plugin.json` | 是 | 声明身份、兼容的 SDK 版本、权限、入口、菜单、数据表 |
| `backend/main.py` | 是 | 导出 `register(registrar, ctx)`，注册自己的接口与 hook |
| `frontend/index.js` | 否 | 导出接收 `host` 的组件工厂 |
| `migrations/versions/*.py` | 按需 | 创建或升级自己的数据表 |
| `frontend/style.css` 等 | 按需 | 前端静态资源，必须随包签名 |

源码目录用于开发，`.cwp` 是上传格式。签名工具根据源目录生成文件清单与签名字段，**源目录里的 `plugin.json` 不要手工填 `files` 和 `signature`**。

### 权限

10 项白名单：`ledger:read`、`book:read`、`book:write`、`subject:read`、`voucher:write`、`plugin_data:rw`、`audit:write`、`settings:rw`、`upload:read`、`import:rw`。

插件不复制宿主的登录逻辑，也不直接操作宿主数据库 —— 它拿到的 `ctx` 是一个窄 SDK：只返回已序列化的结果，复用宿主的账套、角色与凭证护栏。过滤 DSL 不允许拼 SQL。

数据表名强制 `plugin_<hash>_` 前缀，`scope` 分 `book`（账套级）与 `global`。

### 安装与信任

`.cwp` 包必须通过 Ed25519 签名，签名密钥在 `PLUGIN_TRUSTED_KEYS` 里。安装时**与加载时**各做一次静态扫描（import 白名单 + 能力面判定），**插件不能引入 `mcp` / `fastmcp`** —— MCP 是主应用内建模块，不是插件形态。

> **信任边界要说清楚（别把签名当成了"代码无害"的证明）。**
>
> 签名只证明「这个包来自持有 trusted key 的一方」，**不证明包内代码无害** —— 所以静态扫描是独立的第二道闸，它拒掉的是「拿到宿主能力」的写法：非白名单模块、内建能力名（`eval` / `exec` / `open` / `__import__`，**换别名同样拒**）、双下划线名字（`__builtins__` / `__loader__` / `__spec__` / `__dict__` 等，它们能把内建能力重新拿回来）。
>
> 但扫描**不是沙箱**：插件代码在宿主 worker 进程内执行，没有进程隔离。**插件被攻陷或供应链投毒时，它能读到的就是宿主能读到的一切**（含 `.env` 里的凭据）。因此「只安装管理员信任的插件」是这套机制的前提，静态扫描是**防手滑**的那一层，不是**防投毒**的那一层。

多 worker 部署下插件有 **worker 屏障**：所有 worker 必须携带同一个启动代号就绪，缺一个就 fail-closed（503）。主服务活着不代表插件可用。

### Hook 与任务

- **Hook**：目前只有 `on_period_close`（期间结账后触发）。
- **任务**：`plugin.json` 的 `tasks` 支持 `interval` 与 `cron` 两种调度。

### 内置插件

| 插件 | 版本 | 做什么 |
| --- | --- | --- |
| `com.cwq.periodic` 周期记账 | 1.5.3 | 为每个账套维护每月固定支出的规则（日期、分录、科目、摘要、金额），按月生成凭证。可手动执行；宿主每日扫描当月自动生成，结账时补记漏掉的月份 |
| `com.cwq.reimburse` 民宿报销 | 1.3.0 | 上传门店报销明细工作簿，按表页名关联账套、关键词映射科目，整批生成凭证。科目规则全账套共用一份，少数账套科目不一样时可单独覆盖 |

---

## 九、MCP 只读数据服务

挂载在 `/api/mcp`，Streamable HTTP（stateless），供外部 AI agent 查账。

| 分组 | 工具 |
| --- | --- |
| 导航 | `list_books`、`list_periods`、`find_subjects`、`accounting_context` |
| 明细 | `search_vouchers`、`latest_payment`、`check_period_payments`、`ledger_balance` |
| 分析 | `income_expense_trend`、`render_report` |

服务是**只读**的：令牌在 `/api/system/mcp-tokens` 签发与轮换，有调用频次上限、单次最大页数、最大查询月数、语句超时，并记录审计日志。

MCP SDK **定版安装**（`mcp==2.3.0`），不写 `>=` —— 它的传输、工具注册方式与错误语义在 2.x 与 1.x 之间不兼容，而 MCP 的出参是给 AI agent 读的，行为漂移的表现是「agent 某天开始答错」，没有报错、没有堆栈。

### 给 agent 用的 Skill

`.minimax/skills/cwq-accounting-mcp/` 是配套的项目级 Skill，讲清楚**怎么用这个服务才算查对账**：

- 十个工具各自对应哪一类问题、调用顺序（先定位账套与年度，再选工具）；
- 每个返回体都带 `caliber` / `notes[]` / `coverage`，**读数之前先读 `coverage`** —— 这个服务最危险的返回不是报错，是**在账没记完时给出一份结构完整的零值**；
- 「已付 / 仅计提未付 / 有凭证但未记账」三个互斥标志不能合并成一句话；
- 金额是字符串、负数是红字、损益走报表口径而余额走账簿口径；
- 出错时 `error.code` 的分支处理与降级路径。

Skill 里的口径全部来自本仓实现，不是外部约定 —— 改了这边的口径，记得同步改它。

---

## 十、计划任务

由外部 cron 每分钟调 `/api/scheduler/tick` 驱动（HMAC 触发密钥校验，成功返回 202）。

领取机制：租约领取（默认 600 秒）+ PostgreSQL 咨询锁，保证同一任务不会被多个进程重复执行。

内置系统任务：

| 任务 | 调度 | 做什么 |
| --- | --- | --- |
| `oss_backup` | `47 3 * * *` | 逐账套导出 → gzip → SHA256 → 传 OSS → 按档位清理。单账套失败不阻断，全灭则非零退出 |
| `plugin_import_cleanup` | 每 60 分钟 | 清理过期的插件导入批次 |

插件任务由各自的 `plugin.json` 声明，宿主按当前有效授权逐用户执行。

看门狗 `scripts/scheduler_watchdog.py` 每 15 分钟检查任务是否漏跑，异常时告警邮件。

---

## 十一、测试

### 后端

```bash
cd backend
.venv/bin/pytest                        # 全量，xdist 并行
.venv/bin/python scripts/test_changed.py # 只跑改动相关的
```

`pytest.ini` 默认 `-n 6` 并行，全量基线约 60~80 秒、2500+ 用例。

**测试库护栏**是这套测试的地基：`conftest.py` 会对测试库执行 `DROP SCHEMA public CASCADE`，`tests/test_test_db_guardrail.py` 是专门盯这层护栏本身的用例。CI 里显式断言护栏用例是 `PASSED` 而不是 `SKIPPED`，并断言收集到的用例数不低于 900 —— 谁想用 `-k` 悄悄把范围收窄，这一步会先红。

### 前端

```bash
cd frontend && npm test
```

用 Node 内置 test runner（`node --test`），没有 vitest / jest。覆盖金额格、账簿、插件、路由、标签页、打印等。

### UI 探测

`tools/uitest/` 下约 40 个 Playwright 脚本（`ui_voucher.mjs`、`ui_print_tpl.mjs`、`plugin_e2e.mjs` 等）用于看真实界面状态。**它们是一次性探针，不是回归测试，不进 CI。** 正式前端回归测试在 `frontend/tests/`。

---

## 十二、CI

`.github/workflows/ci.yml` 六条 job：

| Job | 内容 |
| --- | --- |
| `backend` | 全量 pytest（PostgreSQL 16 服务 + 真实 `pg_dump` / `psql`） |
| `frontend` | `node --test` 与 `vite build` |
| `docker` | 镜像必须真的构建得出来，且 `alembic.ini` / `migrations/` 在镜像里 |
| `deploy` | 发布脚本沙箱演练 |
| `plugin-browser` | 生产构建 / CSP、签名安装、两个 worker 重启、用户授权闭环 |
| `build-artifacts` | 出部署产物，依赖上面全部成功 |

`pg_dump` 必须与 `postgres:16` 同主版本，否则转储会带版本特有参数 —— CI 里显式装了 `postgresql-client-16` 并校验 `pg_dump` / `psql` 的实际版本。

`.github/workflows/desktop-shell.yml` 单独跑桌面壳：单元测试在 ubuntu（几十秒出结果），Windows 构建与真跑 exe 在 `windows-latest`。PyInstaller 的失败方式很安静 —— 它不报错，产出一个「双击没反应」的 exe，所以必须真跑。

---

## 十三、部署

### 架构

```
浏览器 / 桌面壳
      │
   nginx (TLS、限流、静态前端)
      │
   systemd: cwq-api.service  →  FastAPI (gunicorn/uvicorn workers)
      │                              │
   PostgreSQL 16            阿里云 OSS（账套备份）

动静分离后（可选，见「动静分离与 CDN 部署」）：
浏览器 → CDN（/assets/*，带哈希的 JS/CSS）→ 源站（入口 HTML + /api/）
```

### 发布铁律

`deploy/deploy.sh` 的六条：

1. **先验证再切流量** —— 产物取不到就不重启。
2. **备份上一版**，回滚是改回来，不是重新编译。
3. **表结构变更只由 Alembic 负责**，迁移前必须先备份数据库。
4. **备份失败就不迁移。**
5. **依赖与迁移的依据必须是这一版代码**，不是上一版留在现网的文件。
6. **切换必须原子，被打断必须能退回去** —— 换产物是一次 rename，全程有 trap。

生产机**不再当构建机**。CI 的 `build-artifacts` job 出产物，生产机只取产物 → 解包 → 装依赖 → 迁移 → 切产物 → 重启 → 探活。原来的「生产机跑 pytest + pnpm build」是发版尖峰里最重的一步，还要求生产机常驻 Node.js / pnpm / pytest 三套只为发版存在的工具链。

### 部署目标机：**只有一台**

本项目**只在 `43.138.178.174` 上部署**（Debian 13 + nginx 1.26.3 + PG 16.15，
应用根 `/www/wwwroot/acct.xins.live`）。

历史上部署过三台机：`8.155.171.57`（宝塔，与 tax/work/ai 等 9 个子域共用）、
`110.42.44.53`（`/opt/cwq` 布局，与 19+ 站点共用）、
以及现在的 `43.138.178.174`。**前两台已于 2026-10-24 清理完毕** ——
代码、数据库、systemd 单元、nginx 站点配置、调度 tick、`cwq` 系统用户全部移除，
两台机上本项目的其余站点**照常服务，未受影响**。

> **为什么必须只有一台**：两台机同时跑着这个项目时，两个库各自推进、
> 调度任务被领两遍，数据分叉之后无论回滚到哪台都是错的。
> 「旧机已经清干净」解决不了「有人又往旧机上发一次版」。

所以 `deploy/deploy.sh` 里有一道**目标机守卫**（`check_deploy_target`），
在发布开始前就核对本机身份，对不上直接退出：

| 判据 | 说明 |
| --- | --- |
| `/etc/cwq-deploy-target` | 目标机上的授权标记文件，内容是本机被授权的 IP（现为 `43.138.178.174`）。**「谁是生产机」写在目标机自己的磁盘上**，换机器时改这一个文件即可 |
| 下线主机名单 | `8.155.171.57` / `110.42.44.53`。**这一条永远先查** —— 授权不能推翻禁令，防止把标记文件拷到老机上就放行 |
| 内置名单 | 没有标记文件时，只有出口 IP 是 `43.138.178.174` 才放行 |

判据用**公网出口 IP**（不是 hostname、不是网卡地址）：云主机普遍是 NAT 布局，
网卡上只有内网地址（当前生产机是 `10.1.0.15`），而它对外的身份是弹性公网 IP。

沙箱演练可以用 `CWQ_ALLOW_ANY_HOST=1 deploy.sh` 跳过检查 ——
**生产环境不要设这个变量**。

### systemd 单元

| 单元 | 职责 |
| --- | --- |
| `cwq-api.service` | FastAPI 主服务，走 `scripts/start_server.py` supervisor |
| `cwq-deploy.service` | 发布 |
| `cwq-backup.service` / `.timer` | 数据库定时备份 |
| `cwq-oss-backup.service` / `.timer` | 账套数据 → 阿里云 OSS |
| `cwq-scheduler-watchdog.service` / `.timer` | 计划任务漏跑检查与告警 |

### nginx

- `cwq.conf` —— upstream + keepalive、`/api/` 与 `/api/mcp` 反代、前端静态目录、`/api/scheduler/tick` 独立限流。
- `acct.xins.live.conf` —— 生产站点 server_name、ACME 证书、限流。
- 两份都带 **ESA 回源双向校验（mTLS）** 的配置：信任 ESA 根证书
  （`/etc/nginx/ssl/esa/esa-client-ca.pem`）、校验客户端证书，并把
  `$ssl_client_verify` 记进访问日志。切换强制/过渡态用
  `deploy/esa-mtls.sh`（服务器上是 `/usr/local/sbin/cwq-esa-mtls.sh`）。
  详见 [`deploy/esa-mtls.md`](deploy/esa-mtls.md)。

### 动静分离与 CDN 部署

带宽紧张的小服务器上，**流量的大头不是业务数据，是前端静态资源**：
首屏要拉一个 ~300 KB 的入口 JS 和 ~57 KB 的 CSS（未压缩；gzip 后约 110 KB + 20 KB），
再叠上一堆按需加载的页面 chunk。把这一坨挪到 CDN，源站就只剩「一个 1.5 KB 的
入口 HTML + 业务 JSON」—— 那才是小带宽服务器扛得住的量。

仓库里已经为这件事准备好了三样东西：

| 准备 | 在哪 | 作用 |
| --- | --- | --- |
| 静态资源带长缓存 | `deploy/nginx/*.conf` 的 `location /assets/` | `Cache-Control: public, max-age=31536000, immutable` |
| 入口页与运行时配置**不缓存** | 同上，`location /` 与 `location = /runtime-config.js` | `Cache-Control: no-cache` —— 改了立刻生效 |
| 静态资源带跨源头 | 同上 | `Access-Control-Allow-Origin: *` |

加上**资源文件名带内容哈希**（`index-CUpwOnfx.js`），缓存一年不会出错，
**发布后也不需要刷新 CDN 缓存**：新 HTML 指向的是新文件名。

#### 形态一：Cloudflare 这类反向代理（推荐）

```
浏览器 ──HTTPS──▶ CDN 边缘
                   ├─ /assets/*   命中边缘缓存（不回源）
                   └─ 其它         回源 ──▶ nginx ──▶ FastAPI / 静态目录
```

**URL 不变**（还是 `https://acct.example.com/assets/xxx.js`），CDN 是反向
代理，按路径决定缓不缓存。所以**不用改任何前端代码、不用开 CORS** ——
页面与 API 仍然同源。

要做的事：

1. 域名接入 CDN，**SSL 模式选 `Full (strict)`**（`Flexible` 下源站只听
   443，回源会被拒 → 525/522）。
2. **安全组只放行 CDN 的网段**（`cloudflare.com/ips-v4`、`ips-v6`）——
   不然谁都能绕过 CDN 直接打源站，带宽照样吃满。
3. **装 nginx 的真实 IP 还原配置**（否则登录节流按 CDN 边缘 IP 分桶，
   审计日志里也全是 CDN 地址，**且不报错**）：

   ```bash
   cp deploy/nginx/cwq-cloudflare-realip.conf /etc/nginx/cwq-include/
   nginx -t && nginx -s reload
   ```

4. **配 Cache Rule**（Caching → Cache Rules；多条命中时**靠后的一条赢**，
   这几条互斥，顺序随意）：
   - `/api/*` → **Bypass cache**（缓存住业务响应 = 把一个人的数据发给另一个人；
     这一条同时挡住了插件资产被缓存一年，以及 `.xlsx` 导出被缓存 120 分钟）；
   - `/runtime-config.js` → **Bypass cache**（它决定「API 在哪」，
     缓存住了就是「改了配置不生效」）；
   - `/assets/*` → **Eligible for cache**，Edge TTL 选「有 cache-control 就按它来，
     没有就不缓存」。

   > 表达式写 `starts_with(http.request.uri.path, "/api/")`，或
   > `http.request.uri.path wildcard "/api/*"`；正则形式
   > `http.request.uri.path matches "^/api/"` **仅 Business / Enterprise 套餐可用**。
   >
   > Edge TTL 选「**遵循源站的 Cache-Control**」是不够的：它管的是「缓存多久」，
   > 管不了「哪些 URL 有资格被缓存」，而且源站没给 `Cache-Control` 时它会回落到
   > 「200 响应缓存 120 分钟」。逐项说明见 [`deploy/cloudflare.md`](deploy/cloudflare.md) 第 4 节。
5. **关掉 Rocket Loader 与 Auto Minify**（前者会改写 `<script>` 标签，
   本项目是 ES module；后者会重新压缩产物，与 CSP 对不上时浏览器直接拒绝执行）。
6. 发布后自检：

   ```bash
   bash deploy/verify_cdn.sh https://acct.example.com
   ```

**已知限制**：CDN 的回源读超时是 **125 秒**（免费/专业/商业版改不了），
超了就是 524。报表导出、MCP 的 SSE 长连接要留意（MCP 建议走 DNS-only
子域）。上传上限免费版 100 MB —— 本系统发票批量上传上限 50 MB，够用。

完整步骤、必关开关与排障对照表见 **[`deploy/cloudflare.md`](deploy/cloudflare.md)**。

#### 形态一之变体：阿里云 ESA（生产在用）

生产 `acct.xins.live` 现在前置是**阿里云 ESA**（响应头 `server: ESA`、
`via: ens-cache20.*`、`x-site-cache-status`；`deploy/verify_cdn.sh` 三家都认）。

ESA 比上面那套多两件事：

1. **回源双向校验（mTLS）** —— ESA 回源时带上自己的客户端证书，源站验签。
   源站侧已就位（`/etc/nginx/ssl/esa/esa-client-ca.pem` +
   `ssl_verify_client`），控制台侧开关与切换步骤见
   **[`deploy/esa-mtls.md`](deploy/esa-mtls.md)**。
2. **真实 IP 还原要单独装** —— ESA 的回源网段**不是静态公开列表**，得从
   控制台「安全防护 → 源站防护」复制出来，再用
   `deploy/esa-realip-update.sh` 生成并安装。不装的话登录节流会按 ESA
   边缘节点分桶（**且不报错**）—— 详见 [`deploy/esa-mtls.md`](deploy/esa-mtls.md) 第 6 节。

#### 形态二：静态资源放到另一个 CDN 域名

页面在源站、资源在 CDN 域名时，资源的 URL 就成了**跨域**的。于是需要两样
额外的东西：

- **运行时配置**：告诉前端「API 在哪」「资源在哪」。这两个值**不能烤进
  构建产物**（CI 每个 commit 只构建一份产物，要能装到不同环境），所以走
  `dist/runtime-config.js`（一个 `window.__CWQ_RUNTIME__`），**发布时**由
  发布脚本写进去：

  ```bash
  CWQ_ASSET_BASE=https://cdn.example.com/   # 静态资源基址
  CWQ_ORIGIN=https://acct.example.com       # 源站（配置脚本从这儿取）
  ```

  产物里的 `index.html` 会被改成指向 CDN。**不配这两个变量就是原来的同源
  部署，行为逐字不变。**

- **后端 CORS**（页面与 API 不同源时才需要）：

  ```bash
  CORS_ALLOW_ORIGINS=https://cdn.example.com
  ```

  **不要带结尾斜杠** —— 浏览器的 `Origin` 头没有斜杠，带了就永远匹配不上，
  而且不报错（症状是「配了白名单还是跨源失败」）。不配就是**不开 CORS**
  （默认行为）。

  后端会暴露 `Content-Disposition` 响应头 —— 不然跨域时导出文件名会全变成
  「导出.xlsx」（后端给的好名字被丢掉，且不报错）。

跨域加载 **ES module 一律走 CORS 校验**（浏览器没有「不校验」的选项），
所以 CDN 上的静态资源必须带 `Access-Control-Allow-Origin`。
实在加不了响应头的 CDN，可以用 `VITE_FORMAT=iife` 构建成普通脚本
（普通脚本默认不走 CORS 校验，代价是不能代码分割，首屏多下一些代码）。

#### 静态资源托管在 OSS（源站一个字节都不出）

```bash
CWQ_CDN_UPLOAD=1   # 发布时把 dist/assets/ 传到 OSS
```

配合 CDN 以 OSS 为源站，源站连静态资源都不存。
需要 `OSS_ENDPOINT` / `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` / `OSS_BUCKET`
（与后端备份用的是同一套凭据）。**注意 OSS bucket 要配 CORS 规则**
（允许 GET/HEAD、来源 `*`），否则 CDN 取到的资源浏览器一样不认。

不用这个开关也完全可以：多数形态用「CDN 回源」就行，源站每个文件每个 CDN
节点只回源一次，带宽已经省下来了。

---
## 十四、桌面客户端

`tools/desktop-shell/` 把线上站点包成一个独立窗口的 Windows 程序（`CWQ.exe`）。

| 是 | 不是 |
| --- | --- |
| 一个 exe，双击打开线上站点 | 桌面版（不带离线数据） |
| 后端、数据库、部署全在服务器上 | 改了前端逻辑的东西 |
| 线上站点更新了，壳里的内容跟着更新 | 需要用户自己装浏览器的安装包 |
| 零运行时第三方依赖（只用标准库） | 一个需要长期维护的分支 |

它**不碰业务代码**，`backend/` 和 `frontend/` 一行都不用改。详见 [`tools/desktop-shell/README.md`](tools/desktop-shell/README.md)。

---

## 十五、文档索引

| 文档 | 内容 |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 架构细节（很长，按模块逐层展开） |
| [`docs/audit-remediation-2026-10-25.md`](docs/audit-remediation-2026-10-25.md) | **2026-10-25 审计报告的修复验收记录**：45 条发现 + 路线图 50 项的逐条落点、机制偏离、明确未做项，以及两条可复现的取证命令 |
| [`deploy/cloudflare.md`](deploy/cloudflare.md) | **部署到 CDN（Cloudflare 形态）**：缓存规则、必关开关、真实 IP 还原、已知限制 |
| [`deploy/esa-mtls.md`](deploy/esa-mtls.md) | **阿里云 ESA 回源双向校验（mTLS）**：源站侧配置、控制台步骤、切换与回滚、真实 IP 还原、实测结论 |
| [`docs/plugin-overview.md`](docs/plugin-overview.md) | 插件开发总览与快速开始 |
| [`docs/plugin-spec.md`](docs/plugin-spec.md) | 插件清单、权限、运行时参考 |
| [`docs/plugin-guide.md`](docs/plugin-guide.md) | 插件开发指南 |
| [`docs/plugin-data.md`](docs/plugin-data.md) | 插件数据与表约定 |
| [`docs/plugin-risk.md`](docs/plugin-risk.md) | 插件安全模型与风险 |
| [`docs/plugin-verification.md`](docs/plugin-verification.md) | 签名与验证 |
| [`docs/plugin-troubleshooting.md`](docs/plugin-troubleshooting.md) | 插件排障 |
| [`docs/plugin-roadmap.md`](docs/plugin-roadmap.md) | 插件路线图 |
| [`.minimax/skills/cwq-accounting-mcp/SKILL.md`](.minimax/skills/cwq-accounting-mcp/SKILL.md) | **agent 怎么用本项目的 MCP 查账**：工具选择、口径纪律、错误分支 |
| [`tools/desktop-shell/README.md`](tools/desktop-shell/README.md) | 桌面壳说明 |
| [`tools/uitest/README.md`](tools/uitest/README.md) | UI 探测脚本说明 |
