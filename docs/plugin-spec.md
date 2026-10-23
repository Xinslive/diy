# Manifest、权限与运行时参考

本页是插件开发时需要反复查阅的契约。当前版本为 **SDK 1.2.0 / CWP 1**；示例包中的 `plugin.json`、后端入口、迁移和前端入口可以直接作为最小模板。SDK 1.1.x 插件仍可继续运行，使用定时任务时，`api_version` 范围必须包含 1.2.0。

## Manifest 结构

源目录根部必须有 `plugin.json`。签名打包后，工具会在 manifest 中补齐 `files` 和 `signature`；开发者只维护下面的源字段。

| 字段 | 必填 | 说明 |
|---|---:|---|
| `format_version` | 是 | 当前固定为 `1` |
| `id` | 是 | 小写分段标识，例如 `com.example.periodnote` |
| `name` | 是 | 展示名称 |
| `version` | 是 | SemVer 版本号 |
| `api_version` | 是 | SDK 兼容范围，例如 `>=1.1.0 <2.0.0` |
| `description` | 否 | 插件简介 |
| `permissions` | 是 | 只声明实际使用的 capability |
| `access` | 是 | 当前固定为 `{ "assignment": "admin_users", "default": "none" }` |
| `contributes` | 是 | 后端入口、可选前端入口和菜单 |
| `hooks` | 否 | 当前支持 `on_period_close` |
| `tasks` | 否 | 插件声明的定时任务；宿主管理员启用后才会执行 |
| `tables` | 是 | 插件迁移创建并由宿主登记的数据表 |

一个带页面的插件至少需要下面这些入口字段：

```json
{
  "contributes": {
    "backend_entry": "backend/main.py",
    "frontend_entry": "frontend/index.js",
    "menu": {
      "module": "extensions",
      "title": "期间工作笔记",
      "icon": "Document",
      "route": "/plugin/com.example.periodnote",
      "order": 10
    }
  }
}
```

`backend_entry` 必须位于 `backend/` 下；`frontend_entry` 必须位于 `frontend/` 下。配置了前端入口就必须配置菜单。菜单路由只能是 `/plugin/{id}` 或它的小写子路径，图标使用平台白名单中的 `Calendar`、`Grid`、`Document`、`DataAnalysis`、`Tools`、`Coin`、`Folder`。

## 权限 capability

未声明的能力不会出现在 `ctx` 或宿主服务中。权限应按最小需要声明：

| capability | 后端能力 | 典型用途 |
|---|---|---|
| `book:read` | `ctx.book_read()`、`ctx.book_list()` | 读取账套基本资料、列出用户可访问账套 |
| `book:write` | `ctx.book_write()` | 要求目标账套具有 OWNER 或 ACCOUNTANT 写权限 |
| `ledger:read` | `ctx.ledger_read()` | 读取指定账套和期间的余额数据 |
| `subject:read` | `ctx.subject_read()` | 读取科目并把外部科目编码映射为 `subject_id` |
| `voucher:write` | `ctx.voucher_create()`、`ctx.voucher_batch_create()` | 创建凭证；导入提交也需要此能力 |
| `plugin_data:rw` | `ctx.data.*`、`ctx.config.*`、前端 `services.config` | 访问插件自有表和账套级配置 |
| `audit:write` | `ctx.audit_write()` | 写入插件自定义审计事件 |
| `settings:rw` | `ctx.settings.*` | 管理员维护插件级短配置 |
| `upload:read` | `ctx.upload_read()` | 在当前请求中读取插件接口收到的文件 |
| `import:rw` | `ctx.imports.*`（**不含 `confirm`**）、前端 `services.imports` | 持久文件、预览、整批提交和来源追溯；**确认必须走前端 `services.imports.confirm` 的独立宿主请求**（详见 plugin-data） |

`import:rw` 只提供导入批次能力；正式提交还必须声明 `voucher:write`。`plugin_data:rw` 只允许访问 manifest 声明的插件表，不能用来读取宿主业务表。

## 定时任务

插件可以声明最多 32 个定时任务。任务默认关闭，必须由系统管理员通过宿主管理接口启用；插件升级后如果插件包摘要或 schedule 发生变化，宿主会自动关闭该任务，管理员确认后再启用。

```json
{
  "tasks": [
    {"name": "sync_remote", "schedule": {"type": "interval", "minutes": 15}}
  ]
}
```

`interval.minutes` 为 1 到 525600；cron 采用五字段和 `Asia/Shanghai` 时区。例如 `{"type": "cron", "expression": "10 2 * * *", "timezone": "Asia/Shanghai"}` 表示北京时间每天 02:10。任务名使用 `[a-z][a-z0-9_]{0,47}`，同插件内唯一。处理函数必须同步、只接收一个上下文参数，每个 manifest 声明都必须在 `register` 中注册：

```python
def register(registrar, ctx):
    def sync_remote(task):
        # 示例需要 audit:write；实际处理可使用 task.idempotency_key 做幂等键。
        ctx.audit_write("scheduled_sync")

    registrar.task("sync_remote", sync_remote)
```

管理员的启用决定任务是否参与调度。宿主在每次触发时读取 active 且拥有未撤销插件授权的用户，分别绑定用户身份调用处理函数；新增授权在下一周期自动纳入，撤权和停用会被排除，执行每个用户前再次检查授权。管理员身份也需要显式插件授权。没有有效用户时记录 `SKIPPED`；一个用户失败不阻断其他用户，整次运行记为 `FAILED` 并保存逐用户结果。

账套和业务权限继续通过原有 SDK 检查：`ctx.book_list()` 只返回当前用户获授权的账套，账套写入和凭证提交仍要求相应角色与 capability。插件级 `ctx.settings` 仍是管理员能力，不应作为普通用户任务的可写配置。多个用户可访问同一本账，插件必须额外按“账套 + 业务规则 + 期间”等业务键持久去重；用户幂等键不能替代共享账套的业务去重。

| TaskContext 字段 | 含义 |
|---|---|
| `plugin_id` / `task_name` | 插件和声明的任务名 |
| `run_id` | 本次计划运行的唯一编号；崩溃重领沿用 |
| `scheduled_for` | 本次原定执行时间，带 UTC 时区的 `datetime` |
| `user_id` | 本次调用对应的用户 |
| `attempt` | 租约领取次数，从 1 开始；崩溃重领递增 |
| `idempotency_key` | `run_id:user_id`；同次运行同用户的重领保持不变 |
| `scope` | 固定为 `per_user` |

外部每分钟请求一次宿主 tick，任务按分钟精度运行。首次启用 interval 任务时从启用时间计算间隔；cron 可领取当前分钟到期的任务。延迟或停机期间错过的周期合并为一次，不逐周期补跑；同一任务已有有效租约时不会再次领取。每次 tick 最多领取 20 个任务，余下任务等待后续 tick。运行记录保留 90 天。

普通失败记录为 `FAILED`，等待下一个计划周期。进程崩溃后，600 秒租约过期可重领，正常执行中每 60 秒续租；重领复用原 `run_id` 和用户幂等键，已经成功的用户也可能再次调用，处理函数必须按至少一次执行设计。重领时重新检查当前用户授权。已关闭、移除或升级的任务不重放旧版本运行，留下 `SKIPPED` 记录；worker 临时未就绪会延后领取并保留任务开关。关闭任务不会强制中断正在执行的函数。

处理函数运行在受信任插件 worker 的后台线程中，不接收 HTTP Request、数据库 Session 或宿主内部对象。宿主未提供对插件函数的强制超时或线程终止，插件应自行限制网络超时和单次工作量。管理员使用 `GET /api/system/scheduler/tasks` 查看任务，使用 `PUT /api/system/scheduler/tasks/{plugin_id}/{task_name}` 开关，使用 `GET /api/system/scheduler/runs` 查看结果。外部 tick 的 `202` 仅表示本次领取已被接受，最终结果以运行记录为准。

## 包与签名

`.cwp` 是一个 ZIP 包，根目录必须包含唯一的 `plugin.json`。所有 Python、JavaScript、迁移、样式和图片都会进入签名文件清单。打包工具使用 Ed25519 签名；服务端按文件路径、大小和 SHA-256 重新核验，任何成员被修改都会使验签失败。

签名工具负责：

- 生成并排序文件清单；
- 计算文件摘要和包摘要；
- 生成 `signature` 字段；
- 拒绝路径穿越、链接文件、重复路径和超出容量的包。

私钥只放在受控的打包环境中。可信公钥由运行环境配置，插件包本身不携带可替代可信配置的公钥。签名证明包来自可信发布者且内容未被修改，不代表业务逻辑已经正确，也不提供代码沙箱。

前端必须是随包提供的自包含 ESM。不能运行时加载 CDN、远程模块、SFC 编译器或未打包依赖。

## Python SDK

后端入口固定为：

```python
def register(registrar, ctx):
    ...
```

`registrar` 注册的是插件自己的相对路径，宿主会自动加上 `/api/plugins/{id}` 前缀：

| 方法 | 用途 |
|---|---|
| `registrar.get(path, handler)` | 注册 GET |
| `registrar.post(path, handler)` | 注册 POST |
| `registrar.add(path, handler, methods)` | 注册多个 HTTP 方法 |
| `registrar.route(router)` | 挂载受限路由 |
| `registrar.hook(name, handler)` | 注册已声明的 hook |
| `registrar.task(name, handler)` | 注册 manifest 声明的同步定时任务 |

`registrar.route(router)` 直接可用：`Registrar` 知道自己的插件 id，默认就用它作为命名空间。也可以显式写 `registrar.route(router, namespace="<插件 id>")`；传入的 `namespace` 与插件自己的 id 不一致时会被拒绝。router 必须是 `fastapi.APIRouter`，且不能带生命周期回调、依赖或自定义依赖项。

POST 处理函数按请求体收字段：签名里第一个 `dict` 形参就是整个请求体（`def save_note(payload: dict)`），其余标量形参仍可从查询串或请求体的同名字段取。**请求体是必填的**：载体形参没有默认值时，调用方必须给出 JSON 对象；缺 body 或 body 为空对象一律返回 `422`（`{"detail": "缺少请求体：payload"}`），不会把 `None` 塞进插件函数让它变成 500。

`ctx` 的能力由 manifest 决定。handler 内部使用普通类型、字典和宿主支持的上传对象；不要依赖宿主的 FastAPI app、Request、Response、Session 或 WebSocket。

凭证写入有两种事务语义：`voucher_create` 每次调用单独提交；`voucher_batch_create` 把整批放在同一个事务里，任意一张失败时整批回滚。需要文件、人工预览、幂等和来源追溯时，使用 `import:rw` 的导入流程，不要循环调用单张创建。

数据助手只操作插件自有表：

```python
rows = ctx.data.read(TABLE, book_id=book_id, limit=100)
created = ctx.data.insert(TABLE, {"label": "月末检查"}, book_id=book_id)
changed = ctx.data.update(TABLE, row_id, {"label": "已完成"}, book_id=book_id)
removed = ctx.data.delete(TABLE, row_id, book_id=book_id)
total = ctx.data.count(TABLE, book_id=book_id, where={"label": "已完成"})
```

`data.read` 最多返回 500 行；`update` 和 `delete` 要求表有 `id` 主键，不能修改 `id` 或 `book_id`。这些助手彼此不提供跨调用事务；跨步骤的文件导入应使用宿主导入服务。

### 读表的过滤、分页与计数

`data.read` 支持受限过滤、分页和计数，插件不必把整表读进内存自己筛：

```python
# 等值过滤
ctx.data.read(TABLE, book_id=book_id, where={"label": "已完成"})
# 比较操作符：eq / ne / lt / lte / gt / gte / in / like / is_null
ctx.data.read(TABLE, book_id=book_id, where={"amount": {"gte": 100}})
# 分页与排序（默认按主键）
ctx.data.read(TABLE, book_id=book_id, limit=50, offset=100, order_by="id desc")
# 计数
ctx.data.count(TABLE, book_id=book_id, where={"label": "已完成"})
```

规则：过滤字段必须是本表真实列，值只能是标量；`in` 最多 200 个元素；没有任何拼 SQL 的入口。**账套范围永远由宿主附加**，过滤条件改不掉它。`data.count` 与 `data.read` 用同一套过滤条件；`data.read` 的返回值仍是行列表（旧契约不变），计数单独走 `data.count`。

### 导入白名单

安装时的静态扫描按**白名单**收敛插件的 import：只有下列模块可以 import，其余一律拒绝安装。

| 用途 | 允许的模块 |
|---|---|
| 纯计算与容器 | `abc`、`collections`、`collections.abc`、`contextlib`、`csv`、`dataclasses`、`datetime`、`decimal`、`difflib`、`enum`、`fractions`、`functools`、`itertools`、`json`、`math`、`operator`、`re`、`statistics`、`string`、`textwrap`、`typing`、`unicodedata`、`uuid` |
| 内存缓冲 | `io`（`BytesIO`/`StringIO`；`io.open` 会被属性规则拒绝） |
| 框架类型 | `fastapi` 的 `HTTPException`、`File`、`UploadFile`、`Form`、`Body`、`Query`、`APIRouter`、`status` |
| 用户上传的 xlsx | `openpyxl.load_workbook`（需声明 `upload:read` 或 `import:rw`） |
| 插件迁移 | `alembic.op`、`sqlalchemy` 的列类型构造器、`sqlalchemy.dialects.postgresql` 的 `JSONB` 等 |

同时被拒绝的形态：非白名单模块 import、相对 import、`import *`；`eval`/`exec`/`compile`/`__import__`/`getattr`/`setattr`/`delattr`/`globals`/`locals`/`open` 的**任何引用**（不只是调用）；`input`/`breakpoint`/`vars` 的调用；下标取函数（`func["eval"](...)`）；`posix.execv`、`pty.spawn` 这类危险属性访问；迁移里的 `op.execute`、`op.get_bind` 与 `sa.create_engine`。

白名单是**第一道防线，不是沙箱**：它拦明显等价入口，不等价价（编码混淆、宿主依赖漏洞）仍可能绕过，插件与宿主同进程同权限。需要新模块时，由宿主评审后统一加入清单。

清单的唯一事实来源是仓库里的 `backend/app/plugins/scan_policy.py`：每个允许的模块都写明了**为什么**允许（纯计算？内存缓冲？用户上传文件解析？），宿主据此评审新增项。

## 前端运行时

前端模块默认导出组件工厂：

```javascript
export default function createPlugin(host) {
  return { name: 'MyPlugin', setup() { /* 返回渲染函数 */ } }
}
```

`host` 提供以下能力：

| 能力 | 说明 |
|---|---|
| `h`、`ref`、`computed`、`watch` | 使用宿主提供的 Vue 运行时 |
| `onMounted`、`onUnmounted` | 组件生命周期 |
| `bookId()`、`periodId()` | 当前宿主选择 |
| `books()` | 当前用户可访问账套及 `canWrite` 提示 |
| `api.get/post/put/patch/delete` | 请求当前插件命名空间 |
| `api.upload(url, files, params)` | 上传文件到当前插件接口 |
| `api.download(url)` | 获取当前插件命名空间下的 Blob |
| `services` | 标准账套、配置和导入服务 |

`host.api` 会自动携带登录凭据，插件不需要创建 Axios 实例或手工拼接 Bearer。`put(url, data, params)` 的第三个参数用于 query。前端可以根据 `canWrite` 调整界面，但后端提交时仍必须重新检查账套权限。

宿主不会因为顶部账套或期间切换而重新挂载插件页面。跟随顶部选择的插件可以监听 `host.bookId()`；需要用户独立选择目标账套的插件应使用 `host.services.books()` 并在自己的页面保存选择。

## 迁移与数据表

插件迁移位于 `migrations/versions/*.py`，必须是单根、单头的线性 revision。宿主为每个插件维护独立的版本记录；插件迁移不会改变宿主主迁移链。

- `scope: "book"` 的表必须有非空 `book_id`，并且外键指向 `book.id`；
- `scope: "global"` 只适合配置或参考数据，不能存账套业务行；
- 表名必须使用 `plugin_<sha256(id)[:12]>_` 前缀；例如 `com.example.periodnote` 的前缀是 `plugin_e3efc9984ace_`；
- 不能修改宿主表、其他插件的表或跨插件外键；
- 表结构、约束、索引和触发器必须与已登记的迁移结果一致。

把配置写入插件自己的 book-scoped 表，并通过 `ctx.config.get/put` 做版本控制。不要把账套配置写入全局 settings，也不要在服务启动时执行隐式 DDL。

## 安装、升级和卸载

安装会先校验 manifest、签名、文件和迁移，再把插件登记为待重启状态。安装完成不代表任何用户已经获得使用权；管理员还需要显式授权并按提示重启。

升级必须使用更高的插件版本。宿主会重新检查签名、迁移和已登记表结构；升级成功后插件通常保持禁用，启用新版本前需要再次重启。插件应通过新增 revision 迁移数据，不要依赖降级或重放旧 revision。

卸载会撤销用户授权并隔离插件代码，但默认保留插件数据表，避免把业务数据和卸载操作绑在一起。删除表是独立的管理员操作，只有在确认不再需要数据并完成备份后才执行。

## 管理 API

以下接口都属于管理员面（需要管理员登录；未登录 `401`，非管理员 `403`），路径前缀 `/api/system/plugins`。插件作者调试时可以对照这份清单。

| 方法与路径 | 请求 | 返回要点 |
|---|---|---|
| `GET /api/system/plugins` | — | `{enabled, plugins[]}`：每个插件的版本、digest、期望状态、运行状态、错误码、迁移 head、表数量、备份路径与授权人数 |
| `POST /api/system/plugins/install` | multipart：`file` | `{plugin_id, status, attempt_id}`；`status` 为 `pending_restart`（新装）或 `disabled`（升级后保持禁用） |
| `POST /api/system/plugins/{id}/upgrade` | multipart：`file` | 同上；版本必须更高，否则 `409` |
| `POST /api/system/plugins/{id}/enable` | — | `{status: "pending_restart", plugin_id}`；需重启全部 worker 才生效 |
| `POST /api/system/plugins/{id}/disable` | — | 同上；停用后再重启，已加载的插件才会真正退出 |
| `DELETE /api/system/plugins/{id}` | — | `{status: "orphaned", retained_tables[], backup_path}`；撤销授权、保留数据表、隔离代码 |
| `GET /api/system/plugins/{id}/tables` | — | 表结构与行数、`backup_path` |
| `POST /api/system/plugins/{id}/tables/cleanup` | `{confirmation: "<插件 id>"}` | `{status: "tables_removed", backup_path}`；只允许在保留表卸载后执行，确认串必须等于插件 id |
| `GET /api/system/plugins/{id}/users` | — | 当前授权名单 |
| `PUT /api/system/plugins/{id}/users` | `{user_ids: [...]}` | 全量替换授权名单 |
| `POST /api/system/plugins/{id}/users/{user_id}` | — | 追加一个授权 |
| `DELETE /api/system/plugins/{id}/users/{user_id}` | — | 撤销一个授权 |
| `GET /api/system/plugins/eligible-users` | query：`query`、`page` | 可被授权的启用用户 |
| `POST /api/system/plugins/reconcile` | — | `{completed[], quarantined[], unavailable[]}`：处理安装中断留下的现场 |
| `GET /api/system/plugins/attempts` | — | 最近 50 次安装尝试：状态、错误码、迁移 head、digest 与备份路径 |
| `POST /api/system/plugins/revoke-key` | `{key_id}` | `{status, key_id, revoked_keys[], disabled_plugins[], restart_required}`：封禁签名密钥并停用受影响的插件 |
| `POST /api/system/plugins/maintenance/prune` | `{force: bool}` | `{status, removed[]}`：清理不再被引用的备份与过期隔离目录 |
| `GET /api/plugins/manifest` | — | 当前用户可用插件的清单与**带票据**的前端资产地址 |

错误码以 `{"code": ..., "message": ...}` 返回。常见的有 `PLATFORM_DISABLED`（平台未开启）、`UNTRUSTED_SIGNATURE` / `KEY_REVOKED`（签名密钥不受信任或已撤销）、`STATIC_SCAN_REJECTED`（静态扫描拒绝，附 `findings` 列表）、`FILE_DIGEST_MISMATCH`（包或目录被改动）、`PLUGIN_ALREADY_EXISTS`、`DOWNGRADE_OR_REPLAY_REJECTED`、`PLUGIN_BUSY`（有未完成操作，稍后重试）、`DISABLE_AND_RESTART_REQUIRED`（必须先停用并重启全部 worker）。

## 备份与恢复

插件数据表与宿主数据一起备份。包含插件元数据或插件表的归档写为 v2（v1 仍可读）：归档头记录每个插件的 id、version、digest、迁移 head 和表结构。

- **备份**：账套备份只含该账套的 book-scoped 插件表，系统备份只含 global 插件表。
- **降级而不是整体失败**：某个插件的表结构漂移（被人工改过、被外部清理、跨版本恢复失败）时，备份**不会**让全部账套与系统备份一起失败；漂移的插件被跳过，并在归档头的 `plugins` 段留下一条带 `drifted: true` 与 `error_code` 的记录 —— 数据没有被静默省略，运维在归档里能看见少了谁。
- **恢复**：含漂移记录的归档**不能恢复**（写入前拒绝），必须先修复漂移再重新备份。恢复还要求目标库已安装完全匹配的插件代码、迁移与表结构；未知插件、卸载状态和任何漂移都在写入前拒绝。
- **备份前置**：升级、卸载与清理保留表之前，宿主会生成并校验一份备份（系统 + 全部账套）。备份写在插件运行目录的 `.backups/` 下，**不计入插件代码配额**，有独立额度与清理入口（`POST /api/system/plugins/maintenance/prune`）；仍被 registry 或未完成的安装尝试引用的备份一律不会被清理。
- **资产**：插件前端资源由 `GET /api/plugins/assets/{插件 id}/{digest}/frontend/...` 提供，凭**短期票据**访问（票据随 `/api/plugins/manifest` 一起发给已登录用户），匿名请求返回 `401`。

## 运行时权限结果

插件接口常见的结果如下：

| 情况 | 结果 |
|---|---|
| 未登录 | `401` |
| 已登录但没有插件使用权 | `404`，不暴露插件是否存在 |
| 有插件使用权但没有目标账套权限 | `403` |
| 插件 worker 尚未就绪 | `503` |
| 插件代码抛出未处理异常 | `500`，响应带请求标识 |

撤销授权会影响下一次请求；已经完成的响应不会被回溯撤回，正在进行的事务也不会被中途强行取消。
