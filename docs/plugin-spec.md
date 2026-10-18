# 受信任插件平台规范（SDK 1.0.0 / CWP 1）

本规范对应 `plan.md` 的 P0–P4。实现位于 `backend/app/plugins/`、`frontend/src/plugins/`。运行目录必须独立于程序包和 Git checkout；默认关闭。风险边界及生产启用条件见 [plugin-risk.md](plugin-risk.md)，验证记录见 [plugin-verification.md](plugin-verification.md)。

## 信任与授权

插件 Python、迁移和同源 JS 都是经管理员接受的受信任代码。它们在宿主进程或浏览器 origin 内执行。窄 SDK、签名、扫描、书面审查降低误装风险，不构成 Python、SQL 或 JavaScript 沙箱；恶意插件理论上可以访问宿主进程能访问的数据和浏览器会话。

管理权、用户使用权、插件声明能力、账套角色是四层独立条件：

- 管理操作要求有效登录及 `is_admin`。
- `access` 必须等于 `{"assignment":"admin_users","default":"none"}`。新装不授权任何人，安装者和管理员也无使用权。
- 插件只能获得其 manifest 声明的 SDK 能力。未声明的属性不存在。
- 读写账套仍执行宿主 `get_my_book`、`require_book_write`。VIEWER 只能读，凭证创建走原有会计校验。

manifest、业务 API、SDK 每次调用都读取当前已提交用户状态/授权，不缓存 READY 或授权结果。撤销下一次请求生效，不撤回已经完成的响应，也不会中止已经开始的事务。已认证未授权用户访问任意插件业务路径（包括加载失败 worker 上不存在的路径）统一 404，`detail.code=PLUGIN_NOT_FOUND_OR_UNAVAILABLE`，不泄露插件元数据；已授权而屏障未就绪统一 503，`detail.code=PLUGIN_UNAVAILABLE`。匿名请求 401。插件运行异常返回带 request id 的 500；核心 `/api/health`、`/api/ready` 保持各自语义。

## 包和签名

`.cwp` 为 ZIP，根目录有唯一 `plugin.json`，成员只允许普通文件。无目录成员、链接、可执行权限、设备、加密成员；路径使用相对 POSIX 格式，NFC 规范化；拒绝绝对路径、反斜杠、百分号、冒号、控制字符、空段、`.`、`..`、大小写折叠重名。最长 240 UTF-8 字节、10 层。实际解压也逐成员计数/计字节，不能仅相信中央目录的声明。

共享 `read_upload_sync`、`check_archive_size`、`RowBudget` 与插件上限同时生效，超限 413。默认上传 20 MiB、解压总计 100 MiB、单文件 10 MiB、文件数 512、运行目录总配额 1 GiB；共享 import 上限更小时取更严者。`plugin.json` 上限 256 KiB。备份后重新检查总配额，超限保留备份并停止变更。

签名清单 `files` 必须精确包含除根 `plugin.json` 外的每个文件：`path`、实际 `size`、小写 SHA-256；按 NFC 后路径的 UTF-8 字节排序。Python、JS、迁移、样式、图片均在清单内。不得有清单外文件。签名算法只接受 Ed25519，公钥是 32 字节 raw key 的标准 Base64；`signature.value` 为 64 字节签名的标准 Base64。

令 M 为通过严格 schema 检查且文件清单排序后的 manifest；仅删除 `M.signature.value`，保留 `algo`、`key_id`，构造：

```text
P = RFC8785({"format_version": M.format_version,
             "manifest": M_without_signature_value,
             "files": M.files})
signature.value = Base64(Ed25519.sign(private_key, P))
plugin_digest = lowercase_hex(SHA256(P))
archive_digest = lowercase_hex(SHA256(original_zip_bytes))
```

清单在 manifest 和外层各出现一次是 CWP 1 的固定契约，不可由打包者自行省略。ZIP 时间、压缩级别、成员顺序改变不会改变 plugin_digest。拒绝 JSON 重复键、非有限数字和未知字段。不能用普通 `json.dumps(sort_keys=True)` 代替 RFC 8785。

`PLUGIN_TRUSTED_KEYS` 是 `{key_id: public_base64}` JSON。`PLUGIN_REVOKED_KEYS` 和 `PLUGIN_HISTORICAL_KEYS` 为逗号分隔 key id，均禁止新装/升级；历史已安装 digest 仍允许完整验签启动，公钥需留存。这是兼容历史包的明确策略，紧急阻断历史代码应关闭平台并重启，或停用该插件并重启；不能把撤销 key 当作即时终止已运行进程。

扫描包含明显危险 Python import/反射/动态执行、JS import/eval/Function/远程代码、CSS 外部代码、危险 SQL 和二进制检查。静态扫描不能证明代码安全。首版前端是预编译、自包含的 ESM，不接受运行时 SFC 编译器、外部依赖导入、CDN 或 eval。图片仅允许通过文件头校验的 PNG/JPEG/WebP。

## manifest

完整可打包源见 [plugin-example/plugin.json](plugin-example/plugin.json)。源模板不包含 `files`/`signature`，由离线工具补齐；上传的包必须已经具备这两段。

- `id` 为小写分段标识，最多 96 字符，至少两段；SDK 不允许注册内部保留 id。
- `version` 为严格 SemVer，最多 64 字符；升级按 SemVer precedence，只接受更高版本，build metadata 不参与高低比较。
- `api_version` 使用数值 SDK comparator，例如 `>=1.0.0 <2.0.0`；SDK 当前 `1.0.0`。
- 必须有 `contributes.backend_entry`（`backend/*.py`）；可选 `frontend_entry`（`frontend/*.js`），有前端时必须有 menu，反之亦然。
- menu 固定归属 `extensions`；route 限 `/plugin/{id}` 及小写子路径；route name/key 如显式给出必须为 `plugin:{id}:main`。可选 order 为 0–10000。图标只允许 Calendar、Grid、Document、DataAnalysis、Tools、Coin、Folder。
- 首版只开放 `on_period_close` hook；一次提交后同步通知，rollback 不通知，hook 错误审计并隔离，不回滚已完成的结账。
- `tables` 最多 32 张，schema 仅 `public`。global 表必须声明 `purpose=configuration|reference`，不得存账套业务行。

## 安装、重启与恢复日志

安装流程：完整校验和扫描 → 同文件系统 staging → PostgreSQL session advisory lock + 文件锁 → PREPARED journal → 插件迁移与 MIGRATED journal 同事务提交 → 原子 rename → 短 registry commit/COMMITTED。

文件系统与 PostgreSQL 没有跨资源原子事务。关键文件和目录 fsync，DB journal 持久化；进程中断后由 reconcile 验证事实并向前完成或隔离，不能声称 DB+FS 一次原子提交。全局文件锁还串行化磁盘配额和 reconcile；advisory lock 不因 migration commit 释放。

安装、启用、禁用返回 `pending_restart`；安装后 desired_enabled=true，但没有运行代码或用户授权。管理页不得显示热加载成功。registry.generation 记录期望变更时的 supervisor 代次；该代次的心跳不能把期望变更变成 READY。

重启必须通过 `scripts/start_server.py`：supervisor 一次生成随机 generation、纳秒 started_at、expected_workers，然后 exec uvicorn/gunicorn，由所有子 worker 继承。生产默认 2 worker。不要手工为每个 worker 生成 generation。

每 worker 注册随机 worker id、宿主行和各插件 LOADING/LOADED/FAILED/DISABLED 行，定期续租。每次请求读取当前 cohort、live host 集合、插件 worker 集合、digest 和 desired_revision。必须恰好匹配 expected_workers 且全部 LOADED 才 READY。任一失败、缺席、过期或旧 generation 均拒绝业务流量，连没有匹配路由的 worker 也执行相同前置门禁。较老 supervisor 不能接管较新 cohort；新 cohort 还要等待所有旧代次有效租约退出或过期，重启交叠期间统一拒绝业务流量。核心 ready 不代表插件 ready，部署还运行 `plugin_status.py --wait 30`。

升级、卸载必须先禁用并重启，确认本代次全 DISABLED，且任何代次不存在活的旧 LOADED/LOADING/FAILED lease。升级先生成 SYSTEM+全部 BOOK 备份，检查哈希与 metadata/scope/FK，再迁移；保留授权，保持禁用。启用新版本后再重启。首版禁止移除已登记表、版本重放、降级、复用已卸载 id。

卸载撤销全部授权，隔离代码，保留表及 ORPHANED metadata；不自动删表。独立清理接口要求插件已卸载、无旧 lease、输入完整 id、再次备份；管理页显示表行数、结构及备份位置。备份、旧 digest 目录、quarantine 属于恢复材料，不随应用发布删除。

reconcile 的处理：PREPARED（DDL 未提交）隔离 staging/active 并标 FAILED；MIGRATED 先验证完整签名文件、版本表与实时结构，再完成 rename/registry；commit uncertain 检查 attempt id + digest，幂等完成；未知陈旧目录隔离；缺失 active/结构不明保持 unavailable，不猜测、不重跑已提交 DDL、不自动 downgrade。

## Python SDK

入口签名固定 `register(registrar, ctx)`，返回 None。registrar 在私有 APIRouter 收集相对 HTTP 路径：`get(path, fn)`、`post(path, fn)`、`add(path, fn, methods)`、`route(router, namespace=id)`、`hook(name, fn)`。宿主加 `/api/plugins/{id}` 前缀。无生命周期、自定义 Depends、WebSocket、宿主 Request/Response/Session；不传 FastAPI app。handler 使用普通类型/字典参数，宿主统一解析验证。

| permission | ctx 属性 | 约束 |
|---|---|---|
| book:read | book_read(book_id) | 返回 id/name，检查账套成员 |
| book:write | book_write(book_id) | 检查 OWNER/ACCOUNTANT 写权限 |
| ledger:read | ledger_read(book_id, period_id) | 调宿主余额查询，保留账套/期间校验 |
| voucher:write | voucher_create(book_id, payload) | 宿主 VoucherIn 与凭证创建链，不能直写核心表 |
| plugin_data:rw | data.read/insert/update/delete | 仅 manifest 自有表，book 表必须 book_id；global 写入还需 admin |
| audit:write | audit_write(action) | 1–32 字符事件标识，宿主带用户/plugin/digest |
| settings:rw | settings.get/put(key, value) | 私有短前缀，值最长 4096；写入需 admin |

数据助手：`read(name, book_id=None, limit=100)` 返回字典列表、最多 500 行；`insert(name, values, book_id=None)` 返回新行；`update(name, id, values, book_id=None)`/`delete(name,id,book_id=None)` 返回 affected count。update/delete 要求 id 列；禁止修改 id/book_id。每个助手独立使用宿主管理的短事务，不提供跨助手事务承诺。未鉴权的 register 阶段不能调用数据能力。SDK 无 engine、Session、SQL 字符串接口。

## 独立迁移与数据库契约

迁移脚本位于 `migrations/versions/*.py`，单根单头线性 revision/down_revision；宿主读取 AST 检查链。只执行显式安装/升级，启动只读 head/version/metadata。脚本用 Alembic op/SQLAlchemy，但不运行包内 env.py。主 `alembic_version` 不变，version table 为 `plugin_schema_version_<sha256(id)[:12]>`；业务表前缀为 `plugin_<sha256(id)[:12]>_`。两个 id 哈希前缀碰撞拒绝安装。

安装真实反射列/type/default/identity/computed、PK/FK/check/unique/index、触发器定义及函数源码，规范化存 registry。验证前后宿主/其他插件结构、非本插件 sequence、schema、view、函数和自定义类型不变。不允许改宿主表或跨插件 FK。book 表要求非空 INTEGER/BIGINT book_id → book.id；book 表互相引用要求复合 FK 在同一位置包含 book_id；global 只能引用 global。触发器仅接受本前缀、public、SECURITY INVOKER 函数，首版仅允许 NEW 行正规化，禁止查询/写表/动态执行。正常插件的 DDL 误操作可被回滚；此检查仍不是抵抗恶意进程代码的权限隔离。

迁移后的真实结构是登记依据；缺失或被删除的约束、触发器/函数变更会使 startup 和备份拒绝，不能用 manifest JSON 冒充已经落地的 guardrail。没有通用自动 downgrade。

## 前端运行时和公共资产

用户 manifest 为 `GET /api/plugins/manifest`，Bearer、`Cache-Control: private, no-store`，只含当前用户明确授权且全 worker READY 的前端插件。没有授权时 `{"plugins":[]}`。

资产 URL 为 `/api/plugins/assets/{id}/{digest}/frontend/...`，仅当前已登记 digest，公开 GET、只读、同源、immutable、nosniff、CORP same-origin。不含用户数据、token 或秘密；本机代理不放宽 CSP/CORS。Python、迁移、plugin.json 不作为公共资产。没有 query token；所有资产 URL 禁止 query。公开代码不等于匿名可调用业务 API。

运行时 native `import(entry)`，因此 JS 请求不带 Axios Bearer。模块 default 导出为接收冻结 host 的组件工厂。host 提供 `h/ref/computed/watch/onMounted/onUnmounted`、`bookId()/periodId()`、`api.get/post/put/patch/delete`。api 固定请求当前插件命名空间，自动携带当前登录 Bearer；不提供 raw Axios 实例。示例见 `plugin-example/frontend/index.js`。

动态路由及 menu 派生合并，不修改静态 NAV。tabs 使用动态 key 注册表；恢复前先校验当前用户 manifest，丢弃伪造 path/陈旧 key。登录、切换用户、退出、401、授权刷新、route entry、窗口 focus 都参与清理/刷新；异步旧 manifest 不能重装上个用户的路由。撤权移除 routes/menu/tabs/styles 并回 books。加载失败使用“扩展暂不可用”组件，核心页面继续可用。代码已进入浏览器模块缓存无法被撤回；服务端实时授权是数据访问的最终门禁。

## 管理 API

以下均要求 admin，响应不缓存，修改记录审计；`GET /api/system/plugins` 仍可在平台关闭时检查登记状态。

| 方法 | `/api/system/plugins` 后缀 | 含义 |
|---|---|---|
| GET | 空 | 列表、desired/runtime/error、digest/head、授权数、表数 |
| POST | /install | multipart `file`；全部闸门后安装 |
| POST | /reconcile | 显式恢复未完成日志 |
| GET | /eligible-users?query=&page=1 | 启用用户搜索/30 条分页 |
| GET / PUT | /{id}/users | 读授权名单 / `{user_ids:[...]}` 替换名单 |
| POST / DELETE | /{id}/users/{user_id} | 幂等授予/撤销 |
| POST | /{id}/enable 或 /disable | 保存期望，等待重启 |
| POST | /{id}/upgrade | multipart file，先备份；成功仍禁用 |
| DELETE | /{id} | 先备份，撤权、隔离代码、保留表 |
| GET | /{id}/tables | 真实 metadata、row_count、backup_path |
| POST | /{id}/tables/cleanup | `{confirmation:"完整插件id"}`，独立确认 |

稳定管理错误码包括 INVALID_MANIFEST、UNSAFE_PATH、INVALID_SIGNATURE、UNTRUSTED_SIGNATURE、PLUGIN_BUSY、DISABLE_AND_RESTART_REQUIRED、DOWNGRADE_OR_REPLAY_REJECTED、TABLE_METADATA_MISMATCH、MIGRATION_SCOPE_VIOLATION。扫描失败含 findings，但不返回源码/密钥。审计不写全量用户名单、原始插件代码或密钥；每条 grant/revoke 带目标 user id，实体 id 长度受宿主列约束，完整 plugin id 留在结构化 detail 中。

## 备份 v2 与恢复

无插件 metadata/业务表时仍写 v1；存在任意插件 metadata（即使无表或空表）写 v2。FORMAT_READERS 永久保留 v1，拒绝替换已有 reader。v1 不猜测插件表。

v2 header.plugins 保存 id/version/digest/migration_head 与规范化真实 tables。导出在同一 REPEATABLE READ 快照中核对 registry、版本表、实际结构及所有散落的插件前缀表；未知/多余/漂移拒绝导出。BOOK 含自己的 book 行与 global reference 的 FK 闭包；SYSTEM 不混入账套数据。运行时 registry/journal/cohort/worker 不作为可回灌业务表；registry 事实由 header 校对，用户授权作为 SYSTEM 数据保留。

普通 restore/dry-run 在写入前要求对应插件仍已安装、完整代码验签通过、digest/version/head/metadata 精确相等，逐表逐行核对 scope、完整列和 FK 闭包；不存在推断 SemVer 兼容的分支。未知/缺失插件、卸载 tombstone、篡改代码/结构、缺父行均拒绝；不会先灌核心表再丢插件表。恢复按核心/插件 FK 拓扑插入并重置序列，不运行归档代码、不写主代码目录。调用者控制整个 DB transaction，任一插入失败回滚。

变更前本地 `.backups/{attempt}/` 含 SYSTEM 与全部 BOOK gzip、index.json/SHA-256、行数。为支持卸载后保留表的再次备份，内部备份结构核验可核对 tombstone 元数据；该模式仅限 dry-run，硬性拒绝写入，不是普通 restore 的绕过选项。恢复这些材料前必须重新建立完全匹配的代码及 registry/DB 版本现场。操作员另行保存匹配的签名 `.cwp`、公钥及数据库备份；JSON 数据快照不能代替完整环境备份。不要编辑归档里的 version/digest/head 或删除列来强行恢复。
