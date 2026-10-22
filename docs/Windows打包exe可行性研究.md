# 把 acct.xins.live 封装成 Windows exe —— 技术方案

> 研究日期：2026-10-23
>
> **范围界定**：本文只讨论「怎么把现有这套 web 程序原封不动地封进 exe」，
> 不涉及桌面 UI 设计、不涉及产品形态重新定义。
> 前端那套 Vue SPA **一行都不用改**——这正是「封装」而非「重做」的意义。

---

## 1. 结论

**封装成「本地实例 + 浏览器壳」，不要做内嵌 WebView 窗口。**

```
CWQ.exe 双击
   ├─ 起本地 PostgreSQL 16（首次运行 initdb）
   ├─ 起 uvicorn（127.0.0.1:<随机端口>），同时直供 SPA 与 API
   ├─ 首启自动跑 alembic upgrade head
   └─ 拉起浏览器窗口 → http://127.0.0.1:<port>
```

**为什么不是内嵌 WebView 窗口（pywebview / Electron / Tauri）**：
本项目的凭证打印走的是 `window.open()` + `w.print()`
（`frontend/src/api/print.js:68,81`），
而 **WebView2 里 `window.print()` 不弹打印对话框**
（浏览器闪一下就结束，见 §3），`window.open` 也默认被拦截。
换句话说，**换成 WebView 壳，凭证打印这个核心功能直接废掉**。
详见 §3，这是本次研究里最硬的一条约束。

**形态上的建议**：不要用裸的 `webbrowser.open()`（用户会看到浏览器地址栏，
觉得「这不就是个快捷方式」），而是用 Edge/Chrome 的 **应用模式**
`--app=http://127.0.0.1:<port>`：无地址栏、无标签页，像一个独立程序，
**而且仍然是完整浏览器内核，打印照常工作**。

**工作量**：约 **10–16 人日**（单人），其中
「Windows 平台适配」只占约三分之一，其余是 PostgreSQL 内嵌与打包分发。
详见 §7。

---

## 2. 「封装」有三个层次，代价差很多

| 层次 | 做什么 | 前端改动 | 打印 | 新增依赖 | 人日 |
|---|---|---|---|---|---|
| **L1 本地实例 + 浏览器壳** | exe 带后端 + PG，起本地服务，打开浏览器 | **零** | ✅ 正常 | 无 | 10–16 |
| **L2 本地实例 + 内嵌 WebView** | 同上，但用 pywebview/Electron 开自己的窗口 | 零 | ❌ **坏** | pywebview/Node | 12–18 |
| **L3 纯壳** | exe 只带一个窗口/快捷方式，指向线上 acct.xins.live | 零 | ✅ | 无 | 1–2 |

**L3 值得单独说**：如果真实诉求只是「给客户一个桌面图标，双击打开网站」，
那它 1–2 人日就能出，零风险。但它不满足「离线可用」，
本机没网就什么都干不了。**是否需要 L3，取决于目标用户有没有网络**——
这个我不清楚，需要你确认。

**L2 看着像 L1 的升级版，实际是降级版**：它换来一个「更像桌面软件」的窗口，
赔上打印能力、加上 Electron 的 150 MB 体积和一条 Node 构建链。**不划算。**

本文后续按 **L1** 展开。

---

## 3. 决定性约束：打印决定了壳的选型

### 3.1 本项目的打印实现

```javascript
// frontend/src/api/print.js:68
export function openPrintWindow(html) {
  const w = window.open('', '_blank')   // ← 新窗口
  ...
}

// frontend/src/api/print.js:81
export function printWindow(w) {
  setTimeout(() => { w.focus(); w.print() }, 600)   // ← 调用浏览器打印
}
```

预览用 `iframe` + `srcdoc` 渲染（`frontend/src/components/print/PrintStage.vue:50`），
这层在 WebView2 里是**能**工作的；**真正断掉的是最后那步 `w.print()`**。

### 3.2 WebView2 里的打印现状（外部证据）

| 能力 | 状态 |
|---|---|
| `window.print()` | **不弹打印对话框**。StackOverflow 的实测描述：浏览器闪一秒就没了 |
| `window.open()` | 默认**拦截新窗口**，必须监听 `NewWindowRequested` 自己处理 |
| `ShowPrintUI(System)` | 能弹系统打印框，但**没有预览**——预览区显示 "This app doesn't support print preview"（WebView2Feedback #5744，2026-10） |
| `ShowPrintUI(Browser)` | 有预览的浏览器打印框，需要 WebView2 SDK ≥ 1.0.1518.46 / Edge ≥ 109 |
| `PrintAsync` / `PrintToPdfAsync` | 静默打印/导 PDF，不经过对话框 |

**结论**：
- 在 L2（WebView 壳）里，要保住打印就必须额外做一层桥接
  （拦截新窗口 + 调 `ShowPrintUI`），而 pywebview **没有暴露** `ShowPrintUI`，
  意味着要么改用 Electron 自己写 WebView2 桥接，要么放弃。
- 在 L1（真浏览器）里，**这些都不是问题**——它本来就是浏览器。

> 这条约束也顺带否掉了「Electron 壳」：Electron 的 `webContents.print()` / `printToPDF()`
> 是能用的，但当前打印流程是**前端页面自己调 `window.print()`**，
> 要在 Electron 里保住它，得把整条打印链路改成 IPC 调主进程——
> 那已经是「重新设计」了，正好是你要排除的。

---

## 4. 前端：一行都不用改

这是「封装」最大的收益，原因是当前前端对部署环境几乎零假设：

| 特性 | 现状 | 封装后 |
|---|---|---|
| API 地址 | 相对路径 `/api`（`frontend/src/`） | ✅ 不用改 |
| 路由 | hash 路由（`/#/docs`） | ✅ 不需要服务端 rewrite |
| 登录态 | `localStorage`（`frontend/src/stores/session.js:10`） | ✅ 真浏览器/localStorage 照旧 |
| 静态资源 | Vite 默认 `base: '/'` | ⚠️ 必须由后端挂载到站点根（不是 `file://`） |
| 打印 | `window.open` + `print()` | ✅ 真浏览器下完全正常 |
| 文档页 | `@docs` 别名读仓库 `docs/`（`frontend/vite.config.js:26`） | ✅ 打包时把 `docs/` 一起带上 |

**唯一的前端侧动作**：构建产物 `frontend/dist`（2.4 MB）要随 exe 分发，
由 FastAPI 挂到站点根。

**要顺带修的一处**：nginx 现在把 `/docs` 302 到 `/#/docs`
（`deploy/nginx/acct.xins.live.conf`），因为 SPA 的 hash 路由要靠这个跳转。
没有 nginx 了，这条跳转得在 FastAPI 里补一个路由，否则「文档链接打不开、
跳到登录」。

---

## 5. 后端必须改的 5 处（Windows 化）

其余部分**后端代码不用动**。以下是实测出来的、确实会出问题的地方：

### 5.1 依赖装不上：`requirements.lock` 没有平台标记

`backend/requirements.lock` 里 **0 处 `sys_platform` 标记**，
但锁了 `uvloop==0.23.0`（第 1503 行）。

**uvloop 明确不支持 Windows**：`setup.py` 对 `win32`/`cygwin`/`cli`
直接拒绝安装；即便装上，运行时抛
`RuntimeError('uvloop does not support Windows at the moment')`。

> 这份 lock 是用 `--generate-hashes` 在 macOS 上生成的，**天生不是跨平台锁**。

**改法（二选一）**：
1. 给 uvloop 加 `; sys_platform != "win32"` 标记（改动最小）——
   httptools 与 watchfiles 都有 Windows wheel，不用动；
2. 另出一份 `requirements-win.lock`，在 Windows 上用
   `pip-compile --generate-hashes` 重新生成。更干净，
   且符合本仓「runtime / dev 两份清单」的既有原则。

### 5.2 路径全是「相对当前工作目录」

| 位置 | 当前值 | 后果 |
|---|---|---|
| `backend/app/core/config.py:72` | `env_file=".env"` | 双击 exe 时 CWD 不可控 → 读不到配置 |
| `backend/app/core/config.py:338` | `plugin_root = "data/plugins"` | 同上 |
| `backend/app/core/config.py:414` | `invoice_storage_dir = "data/invoices"` | 同上 |

**改法**：加一个统一的运行时根目录解析器，Windows 上取
`%LOCALAPPDATA%\CWQ\`（用户数据目录；**不能放 Program Files**——
那里需要管理员权限且默认只读）：

```
%LOCALAPPDATA%\CWQ\
  ├─ .env            配置
  ├─ data\plugins\   插件
  ├─ data\invoices\  发票原件
  ├─ pgdata\         PostgreSQL 数据目录
  └─ logs\
```

`config.py` 用的是 pydantic-settings，`env_file` 接受绝对路径，
改造点很小；但 `plugin_root` / `invoice_storage_dir` 是普通字符串字段，
用到的地方要统一走同一个解析器。

### 5.3 schema 启动闸门：应用只校验版本，不建表

`backend/app/core/schema_version.py:84` 的 `assert_schema_ready()`：
版本对不上就抛 `SchemaVersionError` 拒绝启动。
**所以 exe 首启必须先跑 `alembic upgrade head`**，否则永远报
「数据库是空的，且没有 alembic_version」。

**路径逻辑恰好兼容 PyInstaller**：

```python
BACKEND_ROOT = Path(__file__).resolve().parents[2]   # schema_version.py:35
```

- `--onedir`：`<dist>\CWQ\app\core\schema_version.py` → `parents[2]` = `<dist>\CWQ`
  → 把 `alembic.ini` + `migrations/` 放**产物根目录**即可
- `--onefile`：解包到 `_MEIPASS\` → `parents[2]` = `_MEIPASS` → 同样成立

**要改的是错误提示**，它现在是 Linux 专属的：

```python
# schema_version.py:108 / 117 / 155
f"  执行：cd {BACKEND_ROOT} && .venv/bin/alembic upgrade head\n"
```

`.venv/bin/alembic` 在 Windows 上不存在，对终端用户也无意义，
要改成平台中立的说法。

**首启耗时**：initdb 最慢，预期 30–60 秒，**必须有首启引导页**。

### 5.4 nginx 的职责要搬回应用内

| nginx 做什么 | 桌面版怎么办 |
|---|---|
| 提供 SPA 静态文件（`root /opt/cwq/frontend/dist` + `try_files`） | `app.mount("/", StaticFiles(directory=dist))` |
| 安全响应头（CSP / HSTS / nosniff / Referrer-Policy / X-Frame-Options） | 加 FastAPI 中间件，**按 nginx 那份逐条复制** |
| `/docs` → `/#/docs` 302 | 加路由重定向 |
| 限流（`limit_req`） | 127.0.0.1 单 IP，限了没意义；应用内已有登录失败锁定（`app/core/lockout.py`），不受影响 |
| TLS | 本地 http，不需要 |
| `client_max_body_size 21m` | 应用已有 `import_max_bytes = 20MB`，够用 |

> **安全头不能忘。** 现在 CSP 由 nginx 下发；这个仓库对安全头有明确要求
> （nginx 配置里逐条写全，注释说「add_header 是逐层替换不是合并」）。
> 桌面版漏了，就是「本地应用无 CSP」。

### 5.5 gunicorn 在 Windows 上不可用

`backend/scripts/start_server.py:22` 的 `launch_command()` 有 gunicorn 分支，
那是 POSIX 信号模型。Windows 上只能走 uvicorn。
**桌面版本来就该是 1 worker**（生产的 2 worker 是为多用户并发设的，
单机场景没必要）。

---

## 6. PostgreSQL：只能内嵌，这是最大的工作量

### 6.1 为什么换 SQLite 这条路被排除

- 34 条迁移里有 **33 个 plpgsql 触发器**（`0005`/`0007`/`0015`/`0027`/`0028`/`0030`/`0034` 等）
- 用到 JSONB、SERIAL、`CREATE INDEX CONCURRENTLY`
- 连接池建立时执行 `SET TIME ZONE 'UTC'`（`backend/app/core/db.py:67-70`）
  —— PostgreSQL 的会话级 GUC，SQLite 没有对应物
- 备份用 `pg_get_serial_sequence`

> 换 SQLite 是一次重写，不是一次打包。**别在这条路上耗时间。**

### 6.2 内嵌方案

| 方案 | 说明 | 备注 |
|---|---|---|
| **vendor EDB 官方 PG 16 便携二进制** | 解压即用，约 150 MB | **推荐**：与生产同主版本 16，不绑 Python 版本 |
| `celerp-postgres` | PyPI，`win_amd64` wheel，含 `pg_dump`/`psql`/`initdb`/`pg_ctl`，CPython ≥ 3.9 | PG 是 17.x，与生产主版本不同 |
| `pgserver` | PyPI，Windows wheels，自带启停与端口管理 | 同上，需确认 Python 版本要求 |
| `pgembed` | PyPI，Windows wheels，自带 pgvector | **要求 Python ≥ 3.12**，本项目是 3.11 → 得先升 Python |
| `postgresql-binaries`（Rust crate） | 上游二进制，可直接 vendor | 不绑 Python 版本，最干净 |

**推荐直接 vendor PostgreSQL 16 的 Windows 便携二进制**，
把 `initdb.exe` / `pg_ctl.exe` / `postgres.exe` / `pg_dump.exe` / `psql.exe`
+ `lib/` + `share/` 放进产物的 `pgsql/` 目录。

> **必须保持 PG 16**：与生产同主版本，备份/恢复与迁移行为才一致。
> 跨大版本只能走 `pg_dump`/`pg_restore`，不能直接搬数据目录。

**现成的参考实现**：`AudioMuse-AI/native-build/windows/embedded_pg.py`
—— `initdb` → `pg_ctl start` → `pg_isready` 轮询 → 半成品数据目录自愈重置，
这套流程直接抄。

### 6.3 首启序列

```
exe 双击
 1. 检查 %LOCALAPPDATA%\CWQ\pgdata\global\pg_control
      存在 → 已有集群，跳过 initdb
      不存在 → initdb（首次 30–60 秒）
 2. pg_ctl start -o "-p <port> -h 127.0.0.1"
 3. pg_isready 轮询，最长等 30 秒
 4. 建库/建角色（幂等）
 5. alembic upgrade head
 6. uvicorn 起在 127.0.0.1:<port>
 7. 打开浏览器 --app=http://127.0.0.1:<port>
```

### 6.4 端口占用

生产用固定 8000，桌面版**必须用随机端口**（用户机器上 8000 可能被别人占着）。
后端已经支持 `--port`（`scripts/start_server.py:44`），前端走相对路径，
**换端口前端完全无感**。

---

## 7. 打包与分发

### 7.1 PyInstaller：用 `--onedir`，不用 `--onefile`

- onefile 每次启动解压到 `%TEMP%`（启动慢，且触发杀软内存写入启发式）
- onedir 把文件摊在磁盘上，命中概率明显更低
- onedir 下 `alembic.ini` / `migrations/` / `frontend/dist/` / `pgsql/`
  的位置直观得多

**spec 文件要点**：

```python
datas = [
    ('../backend/alembic.ini', '.'),          # 产物根 → parents[2] 正好命中
    ('../backend/migrations', 'migrations'),
    ('../frontend/dist', 'frontend/dist'),    # SPA
    ('pgsql', 'pgsql'),                      # PG 16 便携二进制
]
hiddenimports = [
    'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
    'alembic', 'alembic.runtime.migration',
    'sqlalchemy.dialects.postgresql', 'psycopg',
    'mcp', 'mcp.server.fastmcp',              # mcp==2.3.0 动态注册
    'pypdfium2', 'rfc8785', 'croniter',
]
```

- **关 UPX**（`upx=False`）：UPX 壳特征是杀软规则库的重点命中项
- `console=False`，但**日志必须落文件**——否则用户报错时什么都看不到
- 用多 worker 时必须 `multiprocessing.freeze_support()`（本方案是单 worker，不涉及）

### 7.2 安装包：Inno Setup

结构可以直接参考 ODO 的 Windows 安装器（Python 运行时 + PG 便携版 + Inno Setup，
标准安装约 325 MB）。

### 7.3 代码签名 —— 有一个 2024 年后的变化必须知道

**微软官方已明确：EV 证书不再提供 SmartScreen 立即放行。**
2024 年起微软从受信任根程序要求中移除了 EV 专属 OID，
SmartScreen 现在基于**文件哈希 + 签名者（publisher）信誉**累积，
**没有任何证书类型（OV 或 EV）能立刻绕过**。

> 如果原计划是「买 EV 就不弹窗了」，现在**不成立**。
> OV 证书 + 长期固定发布者身份才是现实解。

```powershell
signtool sign /sha1 <THUMBPRINT> /s My /fd SHA256 `
  /tr http://timestamp.digicert.com /td SHA256 ".\dist\CWQ.exe"
# 安装包同样要签
signtool sign /sha1 <THUMBPRINT> /s My /fd SHA256 `
  /tr http://timestamp.digicert.com /td SHA256 ".\dist\CWQ Setup.exe"
```

- **RFC 3161 时间戳必加**（证书过期后签名依然被认）
- **exe 和安装包都要签**
- **填 VERSIONINFO**：`CompanyName` / `ProductName` / `FileDescription` / `FileVersion`
  —— 杀软误报申诉会检查这些字段，缺了影响通过率
- 发布地址固定用**官方 HTTPS 域名**，不要走网盘/第三方下载站
- 证书 2026-03-01 起最长有效期约 460 天，按短周期续签

### 7.4 杀软误报（预期会发生）

PyInstaller 产物被误报是常态（内存加载 + UPX 壳特征 + 内置 `eval`/`base64` 字符串）。
提前准备 Defender WDSI / Avast / ESET / Symantec 的申诉通道；
灰度发布 + 干净 Win10/11 VM 上**从浏览器下载**实测
（内网共享目录不触发 SmartScreen 判定）。

### 7.5 体积预估

| 组件 | 约 |
|---|---|
| Python 运行时 + 依赖（SQLAlchemy/FastAPI/psycopg/cryptography/lxml/pypdfium2/mcp…） | 120–200 MB |
| PostgreSQL 16 便携二进制（`bin/` + `lib/` + `share/`） | 120–150 MB |
| 前端 `dist` | 2.4 MB |
| **安装包（Inno Setup 压缩后）** | **约 250–350 MB** |

> 这是**推算，不是实测**。我本机是 macOS，没法产出 Windows 产物。

---

## 8. 工作量拆解（单人，人日）

| 阶段 | 内容 | 人日 |
|---|---|---|
| **P0 先在 macOS 上跑通本地实例** | 见 §9 的建议：验证产品逻辑，不碰 Windows | 2–3 |
| P1 Windows 依赖与路径 | uvloop 标记 / win lock、`config.py` 路径改造、schema 闸门提示、gunicorn 分支 | 2–3 |
| P2 内嵌 PG | vendor PG 16、initdb/启停/自愈、首启迁移、pg_dump 备份 | 3–5 |
| P3 nginx 职责迁移 | SPA 挂载、安全头中间件、`/docs` 重定向 | 1–2 |
| P4 运行时细节 | 随机端口、时区（Windows 无 `TZ` 环境变量）、任务调度、窗口关闭时停 PG | 1–2 |
| P5 打包分发 | spec、Inno Setup、签名、干净机验证 | 3–4 |
| **合计** | | **12–19** |

---

## 9. 强烈建议的第一步：在 macOS 上先验证「本地实例」

**不要一上来就碰 Windows。** 这套东西最容易翻车的不是平台差异，
而是「本地实例这套产品逻辑到底成不成立」——首启迁移、插件目录、
备份、打印、离线发票……这些在 macOS 上花 2–3 天就能验证掉。

具体做法：
1. 起一个本机 PostgreSQL 16（仓库 README 里就有现成命令：
   `docker run -d --name cwq-pg -p 15432:5432 postgres:16`）
2. 把 `DATABASE_URL` 指过去，`alembic upgrade head`
3. 用 `uvicorn` 起后端，**直接用 FastAPI 挂 `frontend/dist`**
   （这是唯一一处需要新写的代码）
4. 浏览器打开，走一遍：登录 → 建账套 → 导凭证 → 打印 → 备份

这一步做完，剩下的就纯粹是「Windows 平台适配 + 打包」，
风险和工作量都掉一个档。

> 顺带一提：P3 里的「SPA 挂载 + 安全头中间件」是**平台无关**的，
> 在 macOS 上写完，Windows 直接复用。

---

## 10. 风险清单

| 级别 | 风险 | 应对 |
|---|---|---|
| 🔴 高 | 内嵌 PG 在国产 Windows / 杀毒环境下起不来 | 自愈逻辑 + 日志落盘；准备「连外部 PG」的逃生口（`DATABASE_URL` 直接改即可） |
| 🔴 高 | 杀软误报导致用户装不上 | 签名 + 提前申诉 + 灰度 |
| 🟠 中 | 首启 initdb + 迁移耗时 30–60 秒 | 引导页 + 明确进度文案 |
| 🟠 中 | 插件后端代码的额外依赖不在冻结环境里 | 插件 manifest 声明依赖 + 安装时校验（插件用 `importlib` 从磁盘加载，冻结后**仍能加载**，`app/plugins/loader.py:260`） |
| 🟡 低 | 断网时发票 OCR 不可用 | 提前定义降级形态（只存原件，稍后补识别） |
| 🟡 低 | 桌面版与生产版行为漂移 | 同一份代码 + 同一套测试；CI 加 Windows 矩阵 |

---

## 11. 我没有验证的部分（诚实标注）

1. **未在真实 Windows 上跑过任何一步。** 本机是 macOS。
2. **未实测内嵌 PG 在目标 Windows 版本上的启动行为**——
   `initdb` 在部分精简版 Windows（Server Core）上可能缺 DLL。
3. **未确认生产 PG 的确切小版本**（只确认到主版本 16）。
4. **未做体积实测**——§7.5 是推算。
5. **未验证 `celerp-postgres` / `pgserver` 在 Python 3.11 + Windows 上的兼容性**，
   它们是候选方案而非已验证方案。
6. **未评估商业分发成本**（证书年费、是否需要 EV）。
7. **未确认目标用户的 Windows 版本分布**——
   WebView2 只在 Win10 1809+ / Win11 上有，但 L1 方案不依赖 WebView2，
   所以这条不重要；真正要确认的是「目标机器能不能装 PG 便携版」。

---

## 12. 需要你确认的两件事

1. **目标用户有没有网络？**
   - 没有 → 必须内嵌 PG（本文主体，L1，约 12–19 人日）
   - 有，只需要个桌面图标 → L3，1–2 人日，零风险
2. **是「一个人一台机器」，还是「多个人共用一台 Windows 服务器」？**
   后者是私有化部署，nginx / TLS / 多用户并发都要重新考虑，
   不在本文范围。

---

## 附：相关文件索引

| 关注点 | 文件 |
|---|---|
| 打印实现（决定壳选型） | `frontend/src/api/print.js:68,81`、`frontend/src/components/print/PrintStage.vue` |
| schema 闸门 | `backend/app/core/schema_version.py` |
| 配置与数据目录 | `backend/app/core/config.py` |
| 数据库连接 | `backend/app/core/db.py` |
| 应用入口与 lifespan | `backend/app/main.py` |
| 启动脚本（worker/信号） | `backend/scripts/start_server.py` |
| 备份（pg_dump） | `backend/scripts/db_backup.py` |
| 插件加载 | `backend/app/plugins/loader.py`、`scan_policy.py` |
| nginx 生产配置（安全头来源） | `deploy/nginx/acct.xins.live.conf` |
| 依赖清单 | `backend/requirements.txt`、`requirements.lock` |
