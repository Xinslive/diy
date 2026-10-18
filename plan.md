# 插件体系（Plugin Platform）实施计划

## Summary

**目标**：在不修改主程序业务代码的情况下，由管理员安装经过校验的全栈插件，并在后端重启后使用插件提供的后端 API、前端页面和插件数据。这里的“生效”明确是**安装、校验和登记完成后，重启后生效**，不是热加载或即时生效。

**已确认的架构决策**：

1. **全栈、进程内插件**：插件可以携带后端 Python 和预编译前端 JavaScript，后端在每个 worker 内用 `importlib` 加载。插件与账务引擎同进程、同操作系统权限；这是受信任管理员代码，不是沙箱。
2. **治理模型**：内部系统不做市场和审核流，安装者必须是现有 `require_admin` 管理员。但签名、包完整性、压缩包安全、静态扫描、权限白名单、路径保护和审计都是强制步骤，不能由管理员在接口上跳过。
3. **独立数据迁移轨**：允许插件建表，但每个插件使用自己的 Alembic `ScriptDirectory` 和确定性的独立 version table，绝不并入主迁移链。主 `alembic_version` 只由主程序迁移推进。
4. **运行时语义**：首版不做热重载。安装、启用和停用会返回 `pending_restart`；升级与卸载必须先停用并重启、确认没有旧 worker lease，完成后保持停用，重新启用才需要再次重启。只有重启后的 worker 代次完成一致性握手，插件才可能处于 `READY`。
5. **账务边界**：插件只能使用宿主发放的窄接口访问账套、凭证和 `LedgerEngine`，不得自行计算余额或绕过既有账务护栏。权限是能力发放和契约，不是 Python 沙箱。
6. **插件使用者授权**：插件安装成功后由管理员单独维护可使用用户名单；安装者/系统管理员不因此自动成为唯一或全部使用者。只有当前登录用户被授权且插件处于 `READY` 时，后端才返回该插件的 manifest，前端才注册菜单、路由和页面。

**关键非目标**：

- 不做插件市场、公开注册、多租户隔离或插件间依赖求解。
- 不改主程序三条账务铁律、不改已有 25 条主迁移、不改变主 `alembic_version` 的语义。
- 不把任意插件 `app`、数据库 engine、原始 `Session` 或任意宿主内部模块作为公开 SDK。
- 不把静态 AST 扫描宣传为安全边界；刻意恶意的同进程插件不能靠本计划阻止。
- 不允许通过 query string 传 Bearer token，不放宽到 CDN、跨域脚本或 `eval`。
- 不把“菜单隐藏”当作权限控制；未授权用户必须同时被后端拒绝 manifest、插件 API、插件页面入口和需要保护的插件资产。

**上线前的风险结论**：只要插件保持进程内执行，恶意或有缺陷的插件理论上可以读写宿主进程可访问的数据。签名、管理员限制、审计和扫描只能降低误装和供应链风险，不能消除这个事实。若这个风险不可接受，替代方案必须改为进程外插件（子进程或独立服务、进程间 API 和独立迁移），不能继续用本计划的“同进程但声称隔离”的表述。

---

## 0. 阶段 0：决策门与原型

在编写完整功能前，先完成六个最小原型。原型不进入生产，也不因为“能跑通一个 happy path”就视为可上线。

| 原型 | 必须证明的事实 | 未通过时的处理 |
|---|---|---|
| 全包签名 | 规范化 manifest、包内每个代码/资源/迁移文件的 `path`、`size`、`sha256` 都被同一个签名 payload 覆盖；替换任意文件都会拒绝 | 停止进程内方案，或回到进程外方案 |
| 多 worker | 两个 worker 在同一 generation 和 digest 下预加载；故意让一个 worker 失败时，所有 worker 对插件都返回同一个 `PLUGIN_UNAVAILABLE`，不存在请求交替成功/失败 | 在协议验证前只允许单 worker 开启插件，不能上线多 worker 插件 |
| 资产认证 | 浏览器在现有 `localStorage` + Axios Bearer 模式下能加载插件入口；插件业务 API 仍然鉴权；没有 token query、CORS 或 CSP 放宽 | 保持插件前端关闭，重新设计 asset delivery |
| 独立 Alembic 轨 | 插件迁移可在 staging 包中执行，使用每插件 version table，主 `alembic_version` 在安装前后完全不变；启动只读检查、不执行 DDL | 不进入正式安装阶段 |
| backup v2 | 含插件表的数据可导出、dry-run、恢复 round-trip；未知插件、digest/version 不兼容和表元数据漂移会在写入前拒绝 | 不允许插件声明持久化数据 |
| 用户授权闭环 | 管理员为插件指定用户后，授权用户能看到并访问页面，未授权用户拿不到 manifest、路由、API 和受保护资产；撤销授权立即影响新请求，旧 token 不能绕过 | 插件保持不可见/不可用，不能只做前端隐藏 |

**阶段 0 的退出准则**：六项均有自动化测试和一次失败用例；完成 threat/risk sign-off；明确 `PLUGIN_ROOT`、签名测试密钥和测试数据库。任一项未通过时，不能以“先做 P1、以后再补”为理由进入会执行插件代码的阶段。

---

## 1. 当前基线与硬约束

### 1.1 已核对的仓库事实

| 项 | 当前事实 | 实施约束 |
|---|---|---|
| 后端 | FastAPI；systemd 默认 `uvicorn --workers 2`，Docker 通过 gunicorn 启动 | 每个 worker 都必须独立预加载并参与一致性协议 |
| 主路由 | `backend/app/main.py:135-137` 目前硬编码 include 9 个宿主 router | 不把 `FastAPI app` 交给插件，不在主 include 循环中执行任意插件代码 |
| 启动 | `main.py:186-202` 先校验生产配置、再 `assert_schema_ready()`；启动不执行 DDL | 插件迁移只能显式安装/升级执行，不能放进 startup/lifespan |
| 健康检查 | `/health` 是不碰库的存活探针；`/ready` 才查库 | 插件可用性必须单独有一致的 runtime/readiness 语义，不能把 health 当作插件成功证明 |
| 配置 | `trusted_proxies` 默认空、`import_max_bytes=20MB`、`import_max_unzipped_bytes=200MB`、`registration_enabled=False`；当前无插件配置 | 复用上传容量闸门并新增显式 `plugin_*` 配置，默认关闭 |
| 认证 | `backend/app/core/deps.py` 用 HTTP Bearer；前端把 `cwq_token` 存 localStorage，Axios 注入 Authorization | 插件 API 继续走现有鉴权；不能假定浏览器原生 `import()` 会带 Axios header |
| 上传 | `backend/app/core/uploads.py` 要求共用 `read_upload/read_upload_sync`、`check_archive_size` 和 `RowBudget`；容量超限统一 `UploadTooLarge`/413 | 不为插件重造一套上传闸门 |
| 迁移 | 当前主迁移为 `0001_baseline.py` 至 `0025_backup_run.py`，既有迁移历史冻结且不可修改 | 只追加注册表主迁移 `0026_plugin_registry.py`；插件 revision 不写入主链 |
| 备份 | `backend/app/backup/export.py` 目前依据 `Base.metadata.sorted_tables` 和 FK 闭包枚举 | 插件表必须通过规范化 registry metadata 显式加入，不能只登记 JSONB 后就假定会备份 |
| 前端 | Vue 3 + Vite 静态 `dist`；路由静态数组；`nav.js` 是静态 `NAV`；`tabs.js` 在 import 期生成 `KNOWN_KEYS` | 动态插件元数据须是独立运行时层；不得就地修改宿主 `NAV`，并要重构 tabs 的运行时 key 过滤 |
| 资产请求 | `frontend/src/api/client.js` 只在 Axios 请求加 Authorization；`frontend/src/api/print.js` 已注明裸 URL/window.open 不带 token | 首版插件前端资产仍采用同源、不可写、无敏感数据的不可变 GET；插件页面是否可见由授权 manifest/路由门禁决定，插件数据 API 仍必须 Bearer 鉴权 |
| 部署 | CI 产物由 `deploy/deploy.sh` 取到生产，迁移后切产物并重启；生产不构建 | 插件包是运行时数据，不混进发布产物；`PLUGIN_ROOT` 要单独挂载并设置权限 |
| 测试 | 后端当前约 1406 个 test fn/class 匹配、前端有 23 个 `frontend/tests/*.test.mjs`；CI 有全量 pytest、护栏、前端 test/build 和收集数下限 | 验收不得写死“插件完成后仍为 900”；应保持现有 CI 门禁和当前收集数基线不下降，并确保新增测试被发现 |
| 插件现状 | 当前没有插件目录或插件实现 | 本计划是设计和实施顺序，不把不存在的目录写成现有事实 |

### 1.2 四个直接冲突

1. **多 worker**：每个进程内存独立。只在某个 worker 注册路由会造成相同 URL 间歇性成功，账务系统不能接受这种行为。
2. **静态前端**：插件入口只能在运行时取得。原生 dynamic `import('/api/...')` 不会复用 Axios 的 Bearer header，因此受保护的 asset URL 不能直接作为首版方案。
3. **备份枚举**：不在 `Base.metadata` 的插件表不会自动出现在现有导出中，可能静默丢数据。导出时必须校验 registry、`information_schema` 和实际 scope 的一致性。
4. **使用授权与静态资产**：浏览器原生 dynamic `import()` 不会带 Axios 的 Bearer header。首版不把静态代码资产误称为用户数据权限边界，而是保护 manifest、页面路由和业务 API；资产只允许不可写、无敏感数据、带 digest 的同源资源。若未来要求未授权用户连插件代码都不能下载，必须另做带 Authorization 的 asset loader，不能靠隐藏菜单解决。

---

## 2. 目标架构与运行时协议

### 2.1 目录和数据流

```text
PLUGIN_ROOT/                         # 运行时目录，不进发布包
├── .staging/<attempt-id>/           # 上传、校验、扫描、迁移期间的 staging
├── .quarantine/                     # reconcile 后待人工处理的孤儿包
└── <plugin-id>/<digest>/            # 按 digest 不可变存储，registry 选择 active 版本
    ├── plugin.json
    ├── backend/                     # 可执行 Python，受信任代码
    ├── frontend/                    # 预编译 JS/CSS/静态资源
    └── migrations/                  # 该插件自己的 revisions
```

安装数据流固定为：

```text
上传到临时文件
  -> 共享上传闸门
  -> 校验 zip 目录、成员路径和资源上限；不安全时拒绝
  -> 流式读取成员并计算 digest -> 校验签名 -> 静态扫描
  -> 安全解压到新建 staging
  -> 获取插件 advisory lock（不覆盖上传和解压）
  -> 在 staging 上执行显式插件迁移
  -> information_schema 核对声明表、列、FK、scope 和备份 metadata
  -> 同一文件系统内原子 rename 到 <plugin-id>/<digest>/ 不可变目录
  -> 提交 registry/attempt 事务
  -> 返回 pending_restart
```

DB 和文件系统不是同一个事务资源，因此安装必须带 attempt/journal 和启动 reconcile，不能只依赖 `os.rename` 或数据库 commit 的“看起来原子”。见 §8。

### 2.2 启动顺序

1. 继续执行现有生产配置校验和主 schema 只读校验。
2. 读取 registry 与 `PLUGIN_ROOT`，执行 reconcile；清理过期 staging，识别缺目录、孤儿目录和未完成 attempt。
3. 读取当前 generation，所有 worker 只加载 `desired_enabled=true` 且 digest 一致的插件。
4. 每个 worker 校验 manifest、确认插件迁移已到 head，再调用受限 registrar 的 `register(registrar, ctx)`。
5. worker 写入自己的 runtime registration：`worker_id`、generation、plugin digest、加载结果、错误码和租约时间。禁止用一个共享 `status` 字段让“最后写入的 worker”覆盖其他 worker 结果。
6. 只有同一 generation 下所有预期 worker 都完成预加载并通过一致性检查，插件才进入 `READY`；任意一个 worker 失败，聚合状态为 `UNAVAILABLE`，每个 worker 的插件路由都返回相同的 503 错误码，不提供部分功能。
7. 主 `/health` 和 `/ready` 继续代表宿主进程/数据库核心服务，不因单个插件失败而把整个记账系统从负载均衡中摘除。插件单独报告 capability readiness；`/api/system/plugins` 显示每个插件是 `READY`、`DISABLED` 还是 `UNAVAILABLE`，插件业务路由在 barrier 未就绪时统一返回 503。
8. 用户授权不参与 worker barrier 的 READY 判定：worker 只负责插件代码和运行状态；每个请求在当前认证用户上下文中再次检查授权。授权变更不需要重启，也不改变插件的 `READY` 状态。

P0 必须验证生产的 worker 数量来源（默认 2，或明确的单 worker 模式），并验证 supervisor 为同一批 worker 注入共享的随机 `CWQ_PLUGIN_GENERATION` 和预期 worker 数量。若无法可靠知道预期 worker 数量，在多 worker barrier 验证完成前，`plugin_enabled=true` 只允许单 worker；不能静默退化为“各 worker 自己决定”。旧 worker 的 generation/digest 与新代次不符时，必须被 barrier 排除，直到旧进程退出。

### 2.3 受限插件接口

插件入口签名为：

```python
def register(registrar, ctx) -> None:
    registrar.route(router, namespace="com.example.amortization")
    registrar.hook("on_period_close", handler)
```

`registrar` 只提供：带插件命名空间的 route 注册、受白名单约束的 asset metadata、白名单 hooks 和审计事件；`ctx` 只包含已声明且已授权的 capability helper。插件不能获得 `FastAPI app`，不能添加 middleware/startup/shutdown/event handler，不能替换宿主异常处理器，不能覆盖宿主 route。

后端路由统一挂在 `/api/plugins/{plugin_id}/...`，宿主统一加入 `get_current_user`，账套操作必须再次走 `get_my_book`/`require_book_write`。不向插件公开 engine、原始 `Session`、任意 SQL、Alembic 主 config 或宿主内部模块。

宿主在所有插件入口外再包一层统一的 `require_plugin_access(plugin_id, current_user)`：用户未被授权时一律返回稳定的 `404 PLUGIN_NOT_FOUND_OR_UNAVAILABLE`（不向未授权用户泄露插件是否已安装或用户名单），插件处于非 `READY` 时返回既有的 503 unavailable 错误。该门禁必须覆盖 manifest、插件业务 API 和动态页面入口；若未来声明了非公开资产，也必须覆盖该资产。首版不可变、无敏感数据的静态 asset 可以按 §6.3 的同源 GET 提供，但它不构成插件页面的入口，不能绕过 manifest、路由或业务 API 门禁；插件内部不得自行实现用户授权。

以上是能力契约，不是 Python 沙箱。插件仍可能通过 Python import 访问宿主内部模块；这属于违反规范和需要签名发布者承担的风险，不能在计划中声称已被阻止。

---

## 3. Manifest 与包规范

### 3.1 示例 manifest

`.cwp` 是 zip 包，根目录必须有唯一的 `plugin.json`：

```json
{
  "format_version": 1,
  "id": "com.example.amortization",
  "name": "账期摊销",
  "version": "1.0.0",
  "api_version": ">=1.0.0 <2.0.0",
  "description": "按月摊销并生成提示",
  "permissions": ["ledger:read", "book:read", "plugin_data:rw"],
  "access": {
    "assignment": "admin_users",
    "default": "none"
  },
  "contributes": {
    "menu": {
      "module": "extensions",
      "title": "账期摊销",
      "icon": "Calendar",
      "order": 90,
      "route": "/plugin/com.example.amortization"
    },
    "frontend_entry": "frontend/index.js",
    "backend_entry": "backend/main.py"
  },
  "hooks": ["on_period_close"],
  "tables": [
    {"name": "plugin_8f2a7c91d4b0_items", "scope": "book"}
  ],
  "files": [
    {"path": "backend/main.py", "size": 1234, "sha256": "..."},
    {"path": "frontend/index.js", "size": 2345, "sha256": "..."},
    {"path": "migrations/versions/0001_items.py", "size": 3456, "sha256": "..."}
  ],
  "signature": {
    "algo": "ed25519",
    "key_id": "finance-release-2026",
    "value": "base64..."
  }
}
```

`files` 必须列出除 `plugin.json` 外的每一个常规文件成员，按 Unicode NFC 规范化后的 UTF-8 路径排序；包内常规文件集合必须与该列表完全相等，显式目录成员要么拒绝、要么先规范化为目录而不进入签名文件集合。拒绝 JSON 重复键。签名 payload 使用 RFC 8785 JSON Canonicalization Scheme 编码为 UTF-8，内容为 `format_version`、规范化 manifest（仅移除 `signature.value`，保留并签入 `signature.algo` 与 `signature.key_id`）和规范化完整 `files` 列表。每个 `files[].sha256` 是该成员原始字节的 SHA-256；`plugin_digest` 定义为签名 payload 字节的 SHA-256，用于 registry、generation 和不可变目录名，ZIP 容器字节 hash 只作为可选审计字段。这样代码、前端 JS、CSS、迁移、资源、签名算法/密钥标识和声明本身都受签名保护，不能通过保留合法 manifest 而替换包内代码。

不得只对 manifest 做签名，也不得把“发布方自测”当作代码完整性保护。包哈希、每个文件 digest、签名 `key_id` 和签名算法都要持久化到 registry/audit 中。

### 3.2 字段和命名校验

所有校验 fail-closed，至少包括：

- `id` 使用严格的安全标识符正则，全局唯一；版本使用 SemVer；`api_version` 使用受支持的 specifier 解析器。
- `permissions`、`hooks`、菜单 icon、模块名和 asset 类型都是宿主白名单的子集。插件菜单只能进入扩展命名空间，不能覆盖 `voucher`、`ledger`、`master`、`system` 等宿主模块。
- `backend_entry`、`frontend_entry`、migration 路径和每个 `files.path` 必须是相对路径；拒绝绝对路径、空路径、`.`/`..` 组件、反斜杠混淆和路径归一化后越界。
- 路由必须以 `/plugin/{plugin_id}` 开始，名称必须以 `plugin:{plugin_id}:` 开始；菜单 key、route name 和 API 前缀不得与宿主或其他插件冲突。
- `access` 只描述授权策略，不在包内携带用户 ID 或默认授权名单；首版固定为管理员维护的显式用户名单（`admin_users`），默认值必须是 `none`。插件包不能自行扩大可使用范围，也不能把“安装者”编码为隐含授权。
- 插件表必须使用宿主批准的 schema 和 `plugin_<sha256(plugin_id)[:12]>_` 前缀；宿主检查 hash 前缀映射冲突，表名长度遵守 PostgreSQL 标识符限制。每张表的 `scope` 必须是 `book` 或 `global`。`book` 表必须有 `book_id`，global 表必须明确说明不含账套业务数据。
- 声明的 tables、columns、FK 和 scope 在迁移后由宿主从 `information_schema` 重新读取并规范化登记，manifest 不是事实来源。

---

## 4. 安全设计

### 4.1 上传、zip 和解压

统一复用 `backend/app/core/uploads.py` 的 `read_upload/read_upload_sync`、`check_archive_size` 和 `RowBudget`。插件可以有更严格的上限，但不能绕过共享闸门；容量异常统一映射为 413。

安装器在写入 staging 前后都要检查：

- zip-slip、绝对路径、`..`、NUL、反斜杠变体、重复文件名、大小写归一化冲突；
- zip 符号链接、硬链接或其他特殊成员；拒绝 `external_attr` 表示的 symlink；
- 压缩体积、解压体积、单文件大小、成员数、目录深度和总磁盘配额；
- 扩展名和实际文件类型白名单。拒绝 `.so`、`.pyd`、`.dll`、脚本执行文件和未知二进制；后端 Python、前端预编译 JS、JSON、CSS、迁移 SQL/MD 等允许类型仍需内容扫描；
- 解压根目录必须是新建的随机 staging，任何写入都通过安全的目标路径解析完成；失败时 staging 必须清理或隔离，不能留下半包。

上传、解包、签名和扫描不持有 PostgreSQL 长事务或 advisory lock。完成准备后才获取按 plugin id 的数据库 advisory lock，并重新检查版本、digest 和 registry，防止两个管理员基于旧列表同时安装。

### 4.2 密钥和签名策略

- 使用 Ed25519；可信公钥只读自环境或部署密钥配置，不存于上传包，不在 API 响应中回显。
- `key_id` 绑定可信 key、撤销列表和允许的算法；轮换时保留旧 key 仅用于验证历史已安装版本，撤销后禁止新的安装/升级。
- 记录包 digest、签名 key、manifest version 和安装时间，防止同一版本不同内容重放。
- 默认拒绝降级。恢复或显式回滚必须先完成备份，并由插件提供经过测试的 `down_revision`/回滚证明；没有经过验证的 down migration 时不得自动 downgrade。
- 校验顺序为：流式上传/zip 安全检查 -> 读取完整文件并计算 digest -> 校验签名 payload -> 解析和静态扫描 -> 迁移/安装。任何一步失败不执行插件代码。

### 4.3 静态扫描的范围和边界

Python AST、JavaScript 静态检查和资源扫描应至少阻断明显的 `subprocess`、`socket`、`ctypes`、`eval`、`exec`、`compile`、动态 `__import__`、危险反序列化、任意文件写入和二进制加载；前端同样禁止 `eval`、`new Function` 和内联脚本。扫描结果要包含文件、行号和规则 ID，供审计查询。

**静态扫描不是安全边界**。它挡住明显恶意和误操作，但无法证明进程内 Python 或浏览器同源 JS 是安全的，也不能阻止通过未扫描路径调用宿主能力。发布规则必须要求人工审阅插件源代码，管理员需要接受同进程风险；文档不能写“扫描后插件被沙箱隔离”。

### 4.4 权限和审计

权限采用默认拒绝和 capability 发放：

| 权限 | 宿主提供的能力 |
|---|---|
| `ledger:read` | 只读 `LedgerEngine` 结果视图；不提供任意余额算法入口 |
| `book:read` / `book:write` | 复用 `get_my_book`、`require_book_write` 和既有角色检查 |
| `voucher:write` | 仅提供既有凭证服务函数，继续经过 VIEWER/账套权限和账务护栏 |
| `plugin_data:rw` | 仅能访问当前 plugin id 的数据助手和允许的 scope |
| `audit:write` | 仅能写宿主审计助手规定的结构 |
| `settings:rw` | 仅能读写 `plugin.<id>.*` 前缀设置 |

#### 4.4.1 插件用户授权

插件的 capability 权限与用户使用授权是两层不同的概念：manifest `permissions` 决定插件代码可以请求哪些宿主能力；`plugin_user_access` 决定哪些登录用户可以看到并使用插件页面。前者不能替代后者，系统管理员角色也不应默认绕过后者。

- 新安装后插件默认为 `desired_enabled=true` 但 `access_default=none`，可用用户为空；安装完成后管理员必须从插件管理页显式添加用户，授权用户要等插件进入 `READY` 才能使用。插件包和安装者都不能携带/自动生成用户授权。
- 升级不会改动现有授权名单；仅管理员显式提交授权变更时才增删用户。插件卸载/删除时将有效授权全部撤销并保留审计依据。
- 管理员可以从现有用户目录选择用户，新增、移除和查看插件授权；不能通过前端传入任意用户 ID 绕过用户存在性、停用状态或管理员权限检查。默认不支持按角色、按账套或通配符授权，避免首版引入未定义的继承语义；需要时另行扩展为显式策略版本。
- 每次 manifest、页面入口、插件 API 和非公开资产请求都以当前 Bearer token 解析出的用户为准实时检查授权。撤销授权不要求重启，下一次请求立即生效；前端在重新获取 manifest 或收到 404 后移除菜单、动态路由和 tabs。
- 未授权用户的响应不得暴露插件名称、版本、digest、安装状态或其他被授权用户列表；用户相关 manifest/授权状态响应设置 `Cache-Control: private, no-store`，防止浏览器或共享代理把一个用户的插件清单复用给另一个用户。授权变更、授权校验失败和管理员的增删改操作写入审计，记录被授权/撤销的 `target_user_id` 与操作者，但不记录 token 或整份授权名单快照。
- 管理员管理插件安装/启停/升级/卸载的权限仍由 `require_admin` 控制；“能管理插件”不等于“能使用插件”。如运维确需管理员自测，必须把该管理员显式加入授权名单，并保留审计记录。

所有安装、升级、启用、停用、卸载、用户授权/撤销、清理孤儿表和失败尝试都写 audit。审计记录只存 plugin id、version、digest、操作者、必要时的 target user id、结果、错误码和 attempt id，不记录 token、私钥、完整上传包、源代码、授权名单快照或敏感业务数据。管理写操作失败也必须留痕。

---

## 5. 独立插件迁移轨

### 5.1 每插件独立 version table

原计划的共享 `plugin_schema_version` 表不可采用：标准 Alembic version table 不会自动填充 `plugin_id`，多个插件共享 `version_num` 也会冲突。首版采用确定性表名：

```text
plugin_schema_version_<sha256(plugin_id)[:12]>
```

宿主创建/校验该表，并把该 `version_table` 传给插件的 Alembic `MigrationContext`/配置；插件的 `ScriptDirectory` 只指向其 staging 目录的 `migrations`。表名由宿主计算，不能由 manifest 任意指定。插件可以有自己的 `alembic.ini` 作为脚本配置，但不能改宿主连接、schema、version table 或执行目标。

每个插件的迁移按契约只应写入其声明的 schema/table 前缀，并使用宿主发放的连接上下文；宿主在迁移前后核对实际 schema。这不是对恶意迁移程序的强制隔离，因为同进程 Python 仍可绕过该契约。安装/升级时显式执行 `upgrade head`，在独立、事务性的插件 DB migration transaction 中完成；启动路径只读 version table 与 script head，落后就标记 `MIGRATION_PENDING`，不执行 DDL。插件迁移提交与文件系统/registry 更新之间由 install attempt 和 reconcile 处理，不声称是一个跨资源事务。

### 5.2 表声明和存量库核对

迁移后宿主必须查询 `information_schema.tables/columns/table_constraints/key_column_usage`，核对：

1. manifest 声明的每张表实际存在，名称/schema/prefix 合规；
2. `book` 表有 `book_id`，global 表的 scope 与宿主允许的全局数据类型一致；
3. 列类型、nullable、主键、FK、触发器/序列和 FK 闭包能够被备份/恢复；
4. 实际新建但未声明的业务表、声明但不存在的表、跨插件或跨主表的非法 FK 都拒绝；
5. 最终写入 registry 的 `tables` 是宿主查询出的规范化 metadata，不是盲信 manifest JSONB。

插件自己的触发器和约束必须在自己的迁移中显式创建，并加入插件测试；宿主只保证能检查已声明的契约，不替插件证明账务语义正确。

### 5.3 主迁移隔离

新增 `backend/migrations/versions/0026_plugin_registry.py` 只负责宿主 registry、attempt、worker runtime 和 `plugin_user_access` 授权表所需的主表。它不执行任何插件 revision。安装前后必须断言主 `alembic_version` 相同，启动仍调用既有 `assert_schema_ready()`，不能把 `alembic upgrade` 放在 `main.py` startup/lifespan。

---

## 6. 注册表、管理 API 与资产 API

### 6.1 Registry 数据模型

`plugin_registry` 至少包含以下互相分离的字段：

```text
plugin_id              # 主键
version
plugin_digest          # 规范化签名 payload 的 SHA-256
manifest_version
desired_enabled        # 期望状态，不等于本进程运行状态
access_default         # 首版固定为 none；不从包内直接读取用户授权
runtime_status         # READY/DISABLED/PENDING_RESTART/UNAVAILABLE/...
generation             # 运行代次
error_code
error_message          # 脱敏、长度受限
installed_at
installed_by
migration_head
tables                 # 宿主核对后的规范化 metadata
manifest_metadata      # 仅保留前端/管理所需的受控字段
pending_attempt_id
```

新增 `plugin_user_access`（或等价的规范化授权表），至少包含：`plugin_id`、`user_id`、`granted_by`、`granted_at`、`revoked_at`/有效状态、`grant_id`。主键为 `(plugin_id, user_id)`；外键指向现有用户表和 `plugin_registry`，插件删除时保留可审计的撤销记录或按数据保留策略归档。为支持完整审计，可用 grant history 表保存重复授权/撤销历史，当前有效记录保持 `(plugin_id, user_id)` 唯一。授权查询必须使用数据库索引，不能把整张名单加载到前端再过滤。用户被禁用、删除或登录失效时，授权自然不可用，不能仅依赖授权表状态。用户授权变更与权限检查以数据库提交状态为准，不使用跨 worker 长缓存，保证增删授权无需重启且对下一次请求立即生效。

worker 状态使用独立的 `plugin_worker_runtime` 表或等价的聚合结构：`generation`、`worker_id`、`plugin_id`、`plugin_digest`、`status`、`error_code`、`lease_at`。一个 worker 的结果不能覆盖另一个 worker 的结果；registry 的 `runtime_status` 只能由 generation 聚合器按预期 worker 集合计算。

### 6.2 管理接口

全部管理写接口使用现有 `require_admin` 并记录审计。安装、启用和停用改变运行时目标状态，返回明确的 `pending_restart`；升级和卸载只能在已经停用、重启且无旧 worker lease 后执行，完成后保持停用，不要求再重启。

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/api/system/plugins` | 管理列表：desired/runtime、version、digest、generation、migration head、表数、错误码 |
| POST | `/api/system/plugins/install` | 上传并完成验证/登记，默认设置 `desired_enabled=true`、`access_default=none` 且授权名单为空；P1 不执行代码或插件 DDL，后续阶段才执行显式迁移；返回 `pending_restart` |
| POST | `/api/system/plugins/{id}/enable` | 设置 `desired_enabled=true`，不在当前 worker 热加载；返回 `pending_restart` |
| POST | `/api/system/plugins/{id}/disable` | 设置 `desired_enabled=false`，不在当前 worker 热卸载；返回 `pending_restart` |
| GET | `/api/system/plugins/{id}/users` | 管理员查看该插件当前授权用户；仅返回用户目录允许展示的最小字段，不返回 token 或认证信息 |
| GET | `/api/system/plugins/eligible-users?query=&page=` | 管理员从现有用户目录搜索/分页选择可授权用户；仅返回用户 ID、显示名、启用状态等最小字段，不返回凭据 |
| PUT | `/api/system/plugins/{id}/users` | 管理员以完整集合替换授权名单，或使用等价的增删接口；校验用户存在、账号启用和请求者为 admin，记录差异审计；授权变更即时生效，不返回 `pending_restart` |
| POST | `/api/system/plugins/{id}/users/{user_id}` | 显式授予单个用户使用权；幂等，写入 `grant_id` 和审计 |
| DELETE | `/api/system/plugins/{id}/users/{user_id}` | 撤销单个用户使用权；幂等，下一次请求立即拒绝 |
| POST | `/api/system/plugins/{id}/upgrade` | 仅在插件已停用且新 generation 确认所有 worker 不再运行该插件后接受；先强制备份，再验证完整包并执行向前迁移；默认拒绝降级，升级后保持停用 |
| DELETE | `/api/system/plugins/{id}` | 仅在所有 worker 已确认停用后移除/隔离代码；不自动删业务表，保留 orphaned 记录供清理；无需额外重启 |
| GET | `/api/system/plugins/{id}/tables` | 返回宿主实际核对的表、scope、列和 FK metadata |
| POST | `/api/system/plugins/{id}/tables/cleanup` | 单独、显式、审计化的表清理操作；必须二次确认和备份 |
| GET | `/api/plugins/manifest` | 当前用户已获授权且 READY 的插件元数据、受宿主校验的 route/menu 和 asset entry；响应必须 `private, no-store` |
| GET | `/api/plugins/assets/{plugin_id}/{plugin_digest}/{path}` | 同源、不可写、无敏感数据的静态资源；路径仅能落在对应不可变 digest 目录 |

`/api/plugins/manifest` 不能只写在前端计划里，必须有明确后端实现。它只返回当前用户已获授权且处于 `READY` 的插件，不返回签名密钥、服务器路径、源码、授权名单或管理错误详情。管理员不会因为拥有 `require_admin` 而自动出现在该响应中。

### 6.3 资产和前端 API 的认证边界

首版资产 GET 选择“同源、不可写、无敏感数据、不依赖业务 token”。这不是插件使用授权：未授权用户不能从 `/api/plugins/manifest` 获得入口，也不能通过宿主路由进入页面或调用插件 API；只要用户已经知道 URL，公共静态代码本身可能被读取。若业务要求连代码也只对授权用户可下载，必须在后续版本设计带 Authorization 的 `fetch`/asset loader，不得把 token 放到 query string：

- dynamic `import()` 直接请求同源 asset URL 即可，不需要把 token 放 URL；
- 资产 URL 包含 digest，仅来自已安装、已发布的不可变目录，设置 `nosniff` 和 `immutable` cache policy，不允许上传后覆盖；
- 不开启 CORS，不使用 CDN，不放宽 `script-src`，不执行 `eval`；
- 插件数据和管理 API 仍必须通过现有 Axios Bearer；资产端点不等于业务端点，不能因为 asset public 就放开数据 API；
- `/api/plugins/manifest`、页面路由和插件业务 API 必须实时执行 `require_plugin_access`。插件静态入口只有在被判定为不敏感且不可写时才可走公开 GET；不得把可猜测的 asset URL 当作用户授权证明。
- 若未来必须保护资产，另行实现经安全评审的 Authorization-aware asset loader（可评估 `fetch` + Authorization + `blob:` URL）；不得用 query token 或未经评审的 CSP 放宽作为折中方案。

---

## 7. 后端加载和多 worker 一致性

### 7.1 Loader 流程

`backend/app/plugins/loader.py` 在每个 worker 的 lifespan/startup 阶段调用，但不向插件传 FastAPI app：

```text
discover registry + active directory
  -> 校验 desired_enabled、digest、manifest 和 api_version
  -> 确认每插件 version table == migrations head
  -> 构造受限 registrar/context
  -> importlib 加载 backend_entry
  -> 调 register(registrar, ctx)
  -> 记录 worker-local result
  -> 交给 generation barrier 聚合
```

单个插件失败不应阻止其他插件或宿主核心路由，但失败插件必须在所有 worker 都处于一致的 `UNAVAILABLE`。运行期 handler 仍要检查 runtime gate；不能因为某 worker 在内存中已经注册了 router，就让它绕过聚合状态。

### 7.2 Generation barrier 验收协议

- 安装/升级/启用/停用产生新的 generation；registry 记录目标 digest 和 generation。
- generation 由 supervisor 为每次服务重启生成并传给该批 worker；worker 数量由同一部署配置显式给出，不能只用 release SHA 代替重启代次。
- worker 以唯一 `worker_id` 注册自己的结果，带租约和进程启动时间；旧 generation 的心跳不能更新新 generation。
- 预期 worker 集合由部署配置显式提供。`CWQ_API_WORKERS=2` 时必须有两个新 worker 的结果；无法确认集合时只允许单 worker 插件模式。
- 当所有 worker 同 digest 且均成功时，聚合为 `READY`；任意失败、超时、digest/generation 不同或成员缺失时，聚合为 `UNAVAILABLE`/`PENDING_RESTART`。
- 宿主在路由分发前对 `/api/plugins/{plugin_id}/...` 业务 API 执行 generation guard，包括插件未能注册 router 的 worker；barrier 未通过时每个 worker 都返回相同结构的 503 `PLUGIN_UNAVAILABLE`，不能落成 404 或间歇成功。`/api/plugins/manifest` 和静态 assets 不走该业务路由 guard。
- 重启期间旧 worker 与新 worker 并存时，不允许新代次对外报告插件 READY，直到 barrier 关闭。宿主 `/ready` 仍只决定核心服务是否可接流；部署可另查插件聚合状态，不能把插件失败伪装成宿主故障。

必须有真实的两个进程/两个 worker 集成测试，包含负载均衡重复请求和一个 worker 在 register 中抛异常的情形。仅用两个同进程测试 client 不足以证明该协议。

---

## 8. 前端运行时

### 8.1 Manifest 加载和动态路由

新增 `frontend/src/plugins/manifest.js`、`api.js` 和 `runtime.js`：

1. 用户完成登录后，用现有 API client 获取 `/api/plugins/manifest`；接口只返回该用户被授权且已 `READY` 的插件。普通用户与管理员都必须在 `plugin_user_access` 中有有效记录，失败时降级为“无插件”，宿主首页和系统管理页仍可用，并显示可诊断的非阻塞状态。
2. 对每个返回项校验 plugin id、route name/path 前缀、menu key 和 entry；再次拒绝覆盖宿主路由或其他插件。
3. 使用同源不可写 asset URL 加载预编译 `frontend/index.js`；首版允许该不敏感静态代码 asset 被直接读取，但它不包含业务数据，也不能单独打开插件页面；首版不接受运行时 `.vue` 编译、不引入 `vue3-sfc-loader`，不使用 eval。
4. 模块只返回宿主认可的 route/menu descriptor；宿主用 `router.addRoute` 注入命名空间路由，不能让插件直接替换 router 或全局 app。
5. 登录态变化/登出时清理插件运行时状态和动态 routes；首版不支持在当前会话热换包，重新登录也不能绕过后端重启要求。
6. 每个动态插件 route 都挂宿主 `beforeEach` 授权守卫：当前用户身份必须与获取 manifest 时的身份一致，且该插件仍在当前用户的授权 manifest 中；每次进入插件页面前重新校验当前用户 manifest，应用恢复/重新聚焦时刷新一次授权状态。直接输入 URL、恢复旧 tab 或跨用户切换时不得进入页面，失败时清理该插件 tabs 并回到宿主 404/首页。
7. 服务端撤销授权后，后端 API 从下一次请求起立即拒绝；客户端在收到拒绝或刷新 manifest 发现权限已撤销时移除菜单、路由、tabs 和已挂载插件页面，不能继续展示插件业务数据。

`nav.js` 保持静态 `NAV` 为宿主唯一静态来源，新增纯函数把受校验的 plugin modules 合并成派生导航树，不就地 `push`/修改导出对象。`router/index.js` 的宿主静态 routes 不被插件直接编辑。

### 8.2 Tabs、权限和错误降级

当前 `tabs.js` 的 `KNOWN_KEYS` 在 import 期从静态 NAV 计算，必须改成可接收运行时 plugin key 的 registry/计算方式，否则插件页签恢复会被静默丢弃。插件页签仍只保存既有的 `key/title/path`，不带账套 id。

插件菜单只按当前用户的授权 manifest 生成，不使用 `adminOnly` 作为插件授权替代；插件不能自行把未授权入口添加到导航。管理员若未被显式授权，也看不到用户插件菜单。manifest 拉取失败、模块加载失败、动态路由冲突和后端 503 都必须降级为可见错误状态，不得造成白屏或假装安装成功。

前端 JavaScript 也是同源受信任代码：它可以影响当前 origin 的页面并接触浏览器可见状态，不能把 CSP 或“只读 asset”描述成前端沙箱。该风险要写进 `docs/plugin-spec.md`。

---

## 9. 状态、并发、原子安装与恢复

### 9.1 期望状态和运行状态分离

状态字段的责任必须明确：

```text
desired_enabled: true/false
runtime_status: ABSENT | DISABLED | PENDING_RESTART | LOADING |
                READY | UNAVAILABLE | MIGRATION_PENDING | ORPHANED
plugin_digest, generation, error_code, error_message,
installed_at/by, migration_head, tables
```

推荐状态转移：

```text
ABSENT -> PENDING_RESTART -> LOADING -> READY
                             \-> UNAVAILABLE
READY -> PENDING_RESTART (disable/install)
PENDING_RESTART -> DISABLED (desired_enabled=false, restart 后)
DISABLED -> PENDING_RESTART (enable)
DISABLED -> DISABLED (upgrade 完成；digest 更新，desired_enabled=false)
DISABLED -> ORPHANED (delete 完成；代码移除，业务表保留)
任意 active 状态 -> ORPHANED (文件/registry 不一致，禁止加载)
```

管理操作只改变期望状态和代次；startup/reconcile/worker barrier 才改变 runtime 状态。`error_message` 脱敏并限长；错误码要稳定，便于前端和运维处理。

### 9.2 锁边界和安装原子性

1. 上传、大小检查、解压、digest、签名和扫描在临时文件/staging 中完成，不持有 DB transaction 或 advisory lock。
2. 取得按 plugin id 的 PostgreSQL session-level `pg_advisory_lock`（专用连接持锁）和需要的 `PLUGIN_ROOT` 文件锁，再重新核对 registry/version/digest。锁不覆盖上传/解压，但必须跨过迁移、rename 和 registry commit，不能因 Alembic 内部事务提交而提前释放。
3. 先持久化 `install_attempt=PREPARED`，再在 staging 上执行插件迁移和 schema/backup metadata 核对。迁移使用独立 version table，必须采用受支持的事务性 DDL；首版不接受需要 autocommit 或无法安全重试的迁移，不得修改主 version table。
4. 在 staging digest 与迁移 head 均已验证后，把 staging 原子 rename 到新的 `<plugin-id>/<digest>/` 不可变目录，再在短 DB 事务中更新 registry/attempt 并提交。旧 digest 目录保留到新 generation 成功后再按保留策略清理；rename 只能发生在同一文件系统，不能用跨设备 copy 假装原子。
5. 事务提交后只返回 `pending_restart`，不在当前 worker 执行 `register`。

文件系统 rename 与 DB commit 不是一个原子事务。安装依靠持久化 attempt、session advisory lock 和 reconcile 做跨资源恢复；必须测试连接中断、进程在 rename 前后退出和 commit uncertain 的情形，不能声称二者被同一 DB transaction 原子覆盖。

### 9.3 Crash recovery/reconcile

`install_attempt` 至少记录 attempt id、plugin id、old/new digest、staging path、active path、迁移结果、registry commit 状态和时间。启动及管理员手动 reconcile 时：

- 过期且没有活跃锁的 staging 清理或移入 quarantine；
- registry 指向 active digest 但目录缺失，标记 `ORPHANED/UNAVAILABLE`，不自动从未知源补包；
- active 目录没有已提交 registry 的，先 quarantine 并审计，不能直接加载；
- 数据库事务已回滚但 rename 留下目录，校验 attempt 后删除/quarantine；若 commit 状态不确定，优先保留并人工确认，不能覆盖现有版本；
- migration 成功但 registry 未提交时，从 attempt、plugin version table、staging/active digest 判断状态：若迁移事务回滚则可清理或重试；若已到预期 head 且包 digest 可验证，则向前完成 rename/registry commit；部分或状态不明时标记 `UNAVAILABLE` 并人工恢复，不自动删表或 downgrade；
- reconcile 不能自动删插件业务表、不能自动 downgrade、不能把未验证的目录标成 READY。

### 9.4 卸载、升级和失败

- 升级前强制导出包含插件表的可恢复备份。插件仍 active 时，upgrade 返回 `409 DISABLE_AND_RESTART_REQUIRED`，不执行迁移；管理员先 disable 并重启，确认聚合状态为 `DISABLED` 且无旧 generation worker lease 后再提交 upgrade。迁移失败时保留旧 digest 包和 registry，不把半完成版本标成 enabled。
- `DELETE` 仅在插件已停用且无旧 worker lease 时执行；不自动删表。默认移除/隔离代码并保留表和 `ORPHANED` metadata；表行数、备份位置和清理风险在管理页明确显示。
- 清理表是单独的显式操作，需 admin、二次确认、审计和可验证备份。
- 插件运行期异常由宿主边界转为带 request id 的 500，并记录错误；不能让异常关闭宿主核心路由。无法阻止插件主动破坏数据，这是风险签署项，不是假装已解决的故障隔离。

---

## 10. 备份与恢复（格式 v2）

现有 `cwq-book-backup` `VERSION=1`、`FORMAT_READERS` 和 v1 reader 必须永久保留。只要归档包含插件 metadata 或插件表（即使表当前无行），就升级为新版本（建议 `VERSION=2`），添加 v2 reader，不能让旧 reader“猜”插件表。

### 10.1 导出

v2 的插件段至少包含：

```json
{
  "id": "com.example.amortization",
  "version": "1.0.0",
  "digest": "sha256...",
  "migration_head": "0001_items",
  "tables": [
    {"schema": "public", "name": "...", "scope": "book",
     "columns": [...], "foreign_keys": [...]}
  ]
}
```

`build_table_scopes()` 必须将 registry 的规范化 plugin tables 与实时 `information_schema` 比对，再加入 direct/global/FK closure。发现 registry 漏登记、实际多表、列/FK/scope 漂移时拒绝导出并给出修复提示；不能只靠 registry JSONB 盲枚举，也不能静默漏表。每一张插件表必须能在最终 backup table set 中找到。

### 10.2 恢复

恢复前先做完整 dry-run，确认：

- 首版要求归档中的每个插件已安装同一 `plugin_id`、`plugin_digest` 和 `migration_head`；未实现并测试显式兼容策略前，不接受仅凭 SemVer 推断可恢复；
- 未知插件、未声明表、表 metadata 不匹配和版本不兼容在写入前明确拒绝；默认不允许“只恢复核心表然后丢插件数据”；
- 插件表的 FK/触发器/约束顺序、序列重置和 scope 过滤可执行；
- 恢复过程不会执行任意归档内代码，也不会把插件代码写进主程序目录。

通过 dry-run 后按 metadata/FK 拓扑恢复，重置序列并验证约束、触发器和导出-恢复 round-trip。v1 reader 仍可读取没有插件段的历史归档；旧版本程序遇到 v2/插件归档应明确 `RestoreRefused`，而不是静默忽略插件数据。

---

## 11. 受影响文件清单

### 11.1 后端新增

```text
backend/app/plugins/__init__.py
backend/app/plugins/manifest.py       # manifest、canonical payload、路径/字段校验
backend/app/plugins/registry.py       # registry、attempt、worker runtime 聚合
backend/app/plugins/loader.py         # 受限 registrar、importlib、generation barrier
backend/app/plugins/security.py       # zip 安全、digest、签名、AST/JS 扫描
backend/app/plugins/migration.py      # 每插件 version table 和显式迁移
backend/app/plugins/assets.py         # 同源、只读、digest 固定的资产分发
backend/app/plugins/api.py            # admin 管理 API + /api/plugins/manifest
backend/app/plugins/public_api.py     # 受权限控制的宿主窄接口
backend/app/plugins/reconcile.py      # staging/active/registry crash recovery
backend/app/plugins/errors.py
backend/migrations/versions/0026_plugin_registry.py
```

### 11.2 后端和部署修改

```text
backend/app/main.py                   # 接入 lifespan/启动加载；不 include 任意 plugin app
backend/app/core/config.py            # plugin_enabled/root/limits/trusted keys 等配置
backend/app/backup/export.py          # v2、plugin metadata、scope/FK/restore 核对
backend/.dockerignore
backend/Dockerfile                     # 不把运行时包烘进 image；保留挂载点/权限说明
deploy/systemd/cwq-api.service         # PLUGIN_ROOT、worker barrier/volume/权限配置
deploy/deploy.sh                        # 发布产物排除 runtime plugin；重启后等待 ready
.gitignore                              # 忽略 backend/plugins/* 和 staging；保留 .gitkeep 仅在确需空挂载点时使用
```

`backend/plugins/` 是运行时目录，不应把管理员上传包提交进版本库。若部署脚本可以可靠 `mkdir`，不必新增 `.gitkeep`；若 Docker/systemd 依赖仓库内的空挂载点，则使用仅保留目录的 `.gitkeep`，并确保 ignore 规则不会把运行时文件提交。

### 11.3 前端和文档

```text
frontend/src/plugins/manifest.js
frontend/src/plugins/api.js
frontend/src/plugins/runtime.js
frontend/src/router/nav.js             # 派生合并，不修改静态 NAV
frontend/src/router/index.js           # 保持宿主 routes，接入 runtime 生命周期
frontend/src/stores/tabs.js            # 运行时 plugin keys，不再只依赖 import-time KNOWN_KEYS
frontend/src/views/system/PluginManager.vue
frontend/src/api/system.js             # 或统一的 plugins api client
ARCHITECTURE.md                        # 新增插件体系章节和风险边界
docs/plugin-spec.md                    # manifest、签名、SDK、前端/后端信任模型
docs/plugin-example/                   # 最小签名、迁移、备份可运行示例
```

`PluginManager.vue` 除安装/启停/升级/卸载外，必须提供“可使用用户”管理区：从现有用户目录搜索选择、显示当前授权用户、单个撤销或批量保存；保存后提示即时生效且无需重启。列表不能向非管理员展示，界面不回显认证凭据。

---

## 12. 测试、验收和 CI

### 12.1 后端测试

新增测试必须被现有全量 pytest 自动发现，至少覆盖：

| 测试 | 重点 |
|---|---|
| `test_plugin_manifest.py` | id/version/API/路径/route/menu/permission/hook/table 的逐条 fail-closed 校验 |
| `test_plugin_security.py` | zip-slip、绝对路径、重复名、symlink、成员/压缩/解压/单文件上限、二进制、digest 改包、签名缺失/错误/key 撤销/重放/降级 |
| `test_plugin_loader.py` | `register(registrar, ctx)`、不暴露 app/engine/raw session、好坏插件隔离、迁移落后、路由命名空间和异常边界 |
| `test_plugin_workers.py` | 两个真实 worker、相同 generation/digest、单 worker 失败时所有请求统一 unavailable、旧 worker 不越过 barrier、ready 只在 barrier 后通过 |
| `test_plugin_migration.py` | 每插件确定性 version table、upgrade 失败回滚、启动不执行 DDL、主 `alembic_version` 完全不变、表前缀/metadata 校验 |
| `test_plugin_reconcile.py` | rename 前后进程退出、DB commit uncertain、孤儿 staging/目录、registry 缺目录、migration 成功但 registry 未提交 |
| `test_plugin_api.py` | 非 admin 管理操作 403、manifest 仅返回当前登录用户授权且 READY 的插件、未授权业务 API 稳定 404、pending_restart、失败审计、升级先备份、delete 不自动删表 |
| `test_plugin_user_access.py` | 新装默认零授权；显式 grant 用户成功、其他用户/未显式授权的 admin 都失败；禁用用户失败；撤销/重授幂等且无重启；多 worker 对同一数据库提交的授权变更立即一致；升级保留名单、删除撤销名单 |
| `test_plugin_public_api.py` | 未声明 capability 不存在；book/role/voucher 护栏仍有效；plugin data scope 不能越界 |
| `test_plugin_assets.py` | 静态 asset 限定为同源/不可写/无敏感数据；manifest 按用户返回且 no-store；业务 API 必须 Bearer 并执行实时授权；无 query token/CORS/路径逃逸/不可写覆盖 |
| `test_plugin_backup.py` | v2 export、plugin table scope/FK、dry-run、restore round-trip、未知插件/版本/digest/metadata 不兼容在写入前拒绝；v1 reader 保持 |
| `test_plugin_guardrails.py` | 插件声明的约束/触发器能到达存量库，缺失时检测而不是静默通过 |

安全测试要包含对签名后替换 Python、JS、migration 和资源的 adversarial case。测试结果只能说明实现满足已定义契约，不能把 AST 通过解释为恶意代码安全。

### 12.2 前端测试

沿用现有 `node --test tests/*.test.mjs` 和 `vite.ssrLoadModule` 范式：

- `plugin_manifest.test.mjs`：manifest 失败、版本/digest/route/menu 前缀、只显示当前用户授权插件及无授权时空列表；
- `plugin_nav.test.mjs`：派生合并不修改静态 `NAV`，只按当前用户授权 manifest 显示入口和冲突拒绝；
- `plugin_tabs.test.mjs`：运行时 plugin key 能开关/恢复，仍只存 key/title/path，不带账套 id；
- `plugin_runtime.test.mjs`：asset 入口加载、直接访问未授权 route 被拒、manifest 失败无白屏、授权撤销后下次访问清理 route/tab、模块失败降级、登出/用户切换清理动态 route。

前端 CI 继续执行现有测试发现数断言；新增文件必须被发现，不得用改 glob 或跳过测试让 CI 变绿。

### 12.3 真实 E2E 边界

E2E 使用专用 PostgreSQL 测试数据库、临时 `PLUGIN_ROOT`、临时 Ed25519 测试密钥和可控的两个 worker；不连接生产数据库、不使用生产签名密钥、不写真实上传目录。至少走通：上传 -> 安装后零授权（包括安装管理员都不可见）-> 管理员授予用户 A -> 重启/多 worker barrier -> A 登录看到菜单并访问页面/API、用户 B 登录看不到菜单且直接访问页面/API 均被拒绝 -> 管理员撤销 A -> A 的下一次请求立即失败且无需重启 -> backup/restore -> disable -> pending restart -> 重启并确认无旧 lease -> delete（撤销并留审计、保留表）。升级另行验证“停用重启后备份 -> upgrade -> 保持停用且保留名单”的路径。

### 12.4 CI 和 Definition of Done

Definition of Done 不是“测试数达到 900”。完成条件是：

1. 当前 `.github/workflows/ci.yml` 的后端全量 pytest、数据库护栏、前端 test、Vite build、Docker/deploy 相关门禁继续通过；不得降低现有收集数基线。
2. 新增插件测试全部被收集且有失败用例证明断言非空；收集数基线从 CI 实际输出取得，不在计划中写死旧数字。
3. 六项阶段 0 原型和多 worker/资产/备份安全测试通过。
4. 示例插件完成管理员安装、重启后可用、插件表导出/恢复、停用和保留表卸载；没有人工改库或改主程序代码。
5. 主 `alembic_version` 安装前后完全相同；启动期间没有隐式插件 DDL。
6. 签名错误、改包、路径穿越、重复文件、symlink、危险代码、未知插件和不兼容备份都 fail-closed 且无未清理文件。
7. 好/坏插件和 worker 失败场景均不会造成间歇性成功；插件 unavailable 时主健康/核心账务行为符合既有契约。
8. 授权用户登录后能看到页面、未授权用户和未显式授权的管理员看不到页面且不能调用 API；撤销下一请求生效、无需重启；升级保留名单、卸载撤销名单且有审计。
9. `ARCHITECTURE.md`、`docs/plugin-spec.md` 明确写出：同进程、无沙箱、扫描不是安全边界、插件 capability 与用户授权分层、资产和业务 API 的认证差异、升级/恢复限制。

---

## 13. 分阶段实施与回滚

| 阶段 | 范围 | 退出准则 | 回滚 |
|---|---|---|---|
| **P0 Contract/Prototype** | 六项高风险原型、签名 payload、worker barrier、asset auth、用户授权闭环、独立迁移轨、backup v2 | 所有失败用例和风险签署通过 | 不部署生产代码；若风险不可接受改进程外方案 |
| **P1 Registry + Upload** | `0026` registry/attempt/worker/`plugin_user_access` 表、共享上传闸、安全包校验、签名、manifest、授权存储/API、审计、reconcile；**不 import、不执行插件代码、不执行插件 DDL** | 管理员可安全存储/列出/拒绝包及维护授权名单；新装默认无人授权；旧功能零回归 | 关闭 `plugin_enabled`，清理 staging；主迁移为新增迁移，不回滚历史主链 |
| **P2 Controlled Backend Load** | 受限 registrar、loader、capability、generation barrier、unavailable/readiness、所有插件 API 的用户授权 gate；先只支持无持久化表 fixture | 两 worker 一致性、坏插件隔离、未授权 API 请求被拒绝、核心健康和 ready 验收通过 | 配置关闭插件并重启；registry 保留，active 包不执行 |
| **P3 Frontend Runtime/Manager** | `/api/plugins/manifest`、只读同源资产、按用户授权动态路由/导航/tabs、插件管理页中的用户选择和撤销、失败降级 | 授权用户可见、未授权用户/管理员不可见且后端拒绝；授权变更即时生效；CSP、asset/API auth、无白屏测试通过 | 关闭前端 runtime/插件开关，静态宿主导航继续工作 |
| **P4 Migration/Backup/SDK/Docs** | 每插件迁移、表 metadata、backup v2、public API、示例插件、规范文档和部署 volume | round-trip、缺插件拒绝、升级备份、所有 DoD 通过 | 使用已验证备份和旧插件版本恢复；不做未经测试的 downgrade，不自动删表 |

每阶段结束都要记录全量 CI、真实安装演练、ready/restart 结果和文件/DB reconcile 结果。P1 的“可存储”不等于“可执行”；任何阶段都不能把 `pending_restart` 改成前端假成功。

---

## 14. 风险清单和明确取舍

1. **同进程无沙箱**：这是全栈进程内决策的固有代价。管理员、签名和人工代码审阅是信任根；若业务要求对恶意插件隔离，必须换进程外架构。
2. **静态扫描有限**：AST/JS 检查用于阻断明显风险，不是安全证明，也不能阻止 import 宿主内部模块。
3. **多 worker 一致性复杂**：没有 generation/worker registration/barrier 就不能开多 worker 插件；“每个 worker 各自 load”不是完整方案。
4. **迁移不可随意回滚**：升级前备份；没有经过测试的 down migration 不允许 downgrade；卸载默认不删表。
5. **备份格式兼容**：v1 reader 永久保留；含插件数据使用 v2；旧版本遇到插件归档必须明确拒绝而不是漏恢复。
6. **前端同源代码风险**：资产无业务 token 不代表不可信；插件 JS 仍是当前 origin 的受信任代码，首版只接受签名且人工审阅的预编译 JS。
7. **文件系统/DB 双写恢复**：安装事务必须有 attempt journal 和 reconcile；孤儿目录、注册表缺目录、迁移成功但 registry 未提交都要有自动检测和人工可恢复路径。
8. **部署权限**：生产运行用户只能读 active 插件和写 staging/受控 runtime 目录；部署 artifact 不得覆盖管理员上传包；切流前必须通过新的 ready barrier。

首版不追求“插件像主程序一样自由”。优先保证核心账务仍由现有模块负责、插件失败可诊断、备份不漏数据、重启后所有 worker 语义一致，以及文档对同进程风险诚实。任何无法通过阶段 0 的能力都应删掉或推迟到进程外版本。
