# 插件实施与验收记录

日期：2026-10-18。最新一轮为 SDK 1.2 应用级定时调度，对应 [plugin-roadmap.md](plugin-roadmap.md)；下方保留 SDK 1.1 导入闭环和最初 `plan.md` P0–P4 的验收历史。各轮结果分别记录，历史表中的 SDK、迁移和测试数量不代表当前版本。本次未部署生产、未修改业务数据库和生产配置，插件及插件定时任务仍默认关闭。实际发布者及实际插件包的人工风险接受见 [plugin-risk.md](plugin-risk.md)。


## SDK 1.2 定时调度追加验收

当前 API 版本为 1.2.0，CWP 格式保持 1；主迁移 head 为 `0030_admin_email_mfa`。宿主提供受密钥保护的 `GET /api/scheduler/tick?key=...`，供外部服务每分钟调用，统一领取 OSS 备份、插件导入清理和插件声明的到期任务。插件新增 `tasks` 声明与 `registrar.task()` 注册能力，管理员开启任务后，按当前有效授权逐用户执行，继续使用原有账套权限。

| 检查 | 本轮结果 |
|---|---|
| 后端既有测试及调度器回归 | **1830 项通过，4 项跳过**；排除工作区尚未纳入版本管理的 `test_plugin_periodic.py`，原因见下文 |
| 最终调度、插件契约、加载与迁移专项 | **91 项通过**；最后一次并发领取刷新与同分钟重复运行保护修改后重新执行 |
| 前端全量 | **57 项通过，0 失败、0 跳过** |
| 前端生产构建 | 通过；仓库指南、规范和验收文档继续内联到应用文档 |
| 部署脚本演练 | **60 项通过，0 失败**；没有实际部署 |

四项跳过均由本地环境触发：缺少真实 `pg_dump` / `psql` 导致两项真实 dump/恢复往返及对应开发环境分支跳过，另有一项性能数据检查因开发库未达到所需迁移版本跳过。本轮未重跑 PostgreSQL 全库 dump/恢复往返；既有账套逻辑备份及备份任务的宿主调用测试包含在已通过范围内。

### 调度与多用户行为

- 两个并发首次 tick 不会重复发现或领取同一任务；同一 cron 分钟内关闭再开启也不会重建已完成的运行。
- 外部密钥由管理员生成，数据库与审计仅留摘要或操作信息；轮换时当前及上一把密钥均可用，撤销上一把后立即拒绝。普通用户不能操作任务、历史或密钥。
- tick 的查询参数在进入下游处理前清除，提供的两份 Nginx 配置关闭该路径及末尾斜杠形式的访问日志；返回 `202` 表示已领取，结果通过管理员运行记录查询。
- 使用真实签名插件及真实授权记录验证逐用户执行；VIEWER 无法写账，另一个有权用户仍可执行，最终保留逐用户结果及整轮失败状态。
- 用户停用、插件撤权后被排除，重新授权后自动参加后续周期；没有有效用户时记为 `SKIPPED`。插件版本变化自动关闭任务，worker 临时未就绪只延后领取。
- 租约过期后复用原 `run_id`，领取次数递增，旧租约持有者不能覆盖新结果；停用、移除或升级的旧运行不重放。已关闭 OSS 备份的旧运行也会跳过。
- 内置任务调用既有备份及导入过期清理服务，备份部分失败会使调度运行标记失败。备份时间为北京时间 03:47，继续受数据安全设置约束。

任务采用至少一次执行语义。崩溃重领可能再次调用已成功用户，插件必须使用 `run_id:user_id` 幂等键；共享账套仍需按账套、业务规则和期间持久去重。当前不提供对受信任插件函数的强制超时或线程终止，普通失败等待下一周期。详细上下文与边界见 [插件规范](plugin-spec.md#定时任务)。

### 重跑与未计入范围

```bash
# 仓库根目录；只允许使用专用测试库，pytest 会重建隔离库 schema。
backend/.venv/bin/python -m pytest -n 3 --ignore=backend/tests/test_plugin_periodic.py backend/tests -ra --tb=short
backend/.venv/bin/python -m pytest -n 3 backend/tests/test_scheduler.py backend/tests/test_plugin_manifest.py backend/tests/test_plugin_loader.py backend/tests/test_schema_version.py backend/tests/test_migration_guardrails.py -ra --tb=short
npm --prefix frontend test
npm --prefix frontend run build
bash deploy/tests/test_deploy.sh
```

尚未纳入版本管理的周期记账插件测试已单独诊断：创建规则请求返回 `422`（缺少 query 中的 `book_id`），随后 fixture 清理插件表遇到数据库锁等待。这发生在插件业务接口阶段，未计作调度器通过证据，也未覆盖该工作区插件的业务实现。

本轮未重跑真实浏览器、Docker 镜像构建或远程 CI，也未创建外部一分钟调用任务。上线需先迁移到 `0030_admin_email_mfa`，生成调度密钥并配置外部调用；旧 `cwq-oss-backup.timer` 不应同时启用，既有本地 pg_dump 定时器继续保持关闭。部署步骤见 [README](../README.md#启用定时调度)。

## SDK 1.1 导入闭环追加验收

该阶段 API 版本为 1.1.0，CWP 格式保持 1；主迁移 head 为 `0027_plugin_imports`。本阶段完成用户选择目标账套、标准文件留存、持久预览、人工确认、整批原子提交、幂等重试、来源追溯和插件自有账套配置。SDK 后续扩展见优化清单，未计作本阶段已实现功能。

| 检查 | 本轮结果 |
|---|---|
| 后端全量 `pytest -q -n 3 -ra --tb=short` | **1810 项通过，1 项跳过**；唯一跳过项是“缺少 PostgreSQL 客户端”的环境分支，因为本轮已提供真实客户端；没有跳过备份恢复 |
| PostgreSQL 真实恢复 | 使用本机 PostgreSQL 16 容器内的真实 `pg_dump` / `psql`，经仅允许隔离测试库的本地启动器调用，并设 `CWQ_REQUIRE_PG_CLIENT=1`；验证完整 dump、空库恢复、结构/行数及实际标记数据 |
| 导入与配置集成测试 | `test_plugin_imports.py` 和 `test_plugin_import_example.py` 共 24 项，全部通过，包含真实数据库事务、并发、CSV 解析和 BOOK 备份恢复 |
| 迁移、schema 与打包专项 | 50 项通过；涵盖追加迁移、head/触发器清单、历史 stamp 重放、签名示例打包和 Finder 元数据排除 |
| 前端全量 `node --test tests/*.test.mjs` | **57 项通过，0 失败、0 跳过**；文档目录、SDK 和插件页面生命周期检查均通过 |
| 前端生产构建 | 通过；应用内文档直接内联仓库中的指南、数据接口、规范、风险、验证和优化清单 |
| 部署脚本演练 | **47 项通过，0 失败**；没有实际部署 |
| 真实浏览器、生产构建/CSP、两个 OS worker | 通过：签名安装、用户授权、CSV 配置、真实 multipart 上传、修改并保存预览、顶栏切换保留插件目标账套及未保存调整、刷新恢复、人工确认、两张凭证整批提交、重复提交返回同一结果；同时复验撤权、降级、备份恢复和保留数据卸载 |

浏览器截图为 `output/playwright/plugin-csv-submitted.png`。本轮发现并修复了用户资料刷新误触发身份变化、导致已加载插件样式被移除的问题；脚本在首次打开、刷新以及提交后均断言样式仍生效，再进行截图检查。

### 一致性和失败路径

- 同批四个并发提交请求只创建一组凭证和来源关联；提交成功后的重试返回原结果。
- 第二张凭证发生预期错误时，第一张及其凭证审计、来源均回滚，批次保留 `FAILED` 和错误，修正后可重试；意外异常回滚至 `CONFIRMED`，不留下半批凭证。
- 预览不平衡、停用科目、非末级科目、期间不合法、schema 错误、手工凭证号及负附件数均不能确认；错误持久化，不生成正式凭证。
- 修订冲突、插件升级、解析后配置变化、确认后配置变化均拒绝陈旧结果；首次配置并发保存只允许一个成功。用户授权或账套写权限撤销后下一请求被拒绝。
- 覆盖同批重复、部分文件交叠、容量上限、取消释放占位、原文件到期、清理后保留证据，以及包含源文件字节和提交来源的账套备份恢复。

### 本阶段重跑

先安装与本机 PostgreSQL 服务端匹配的真实客户端工具，使 `pg_dump` / `psql` 可在 PATH 找到，或设置 `CWQ_PG_DUMP`、`CWQ_PSQL` 为实际路径。以下命令只允许使用专用测试库；浏览器脚本另使用 `*_plugin_browser_test` 数据库。

```bash
# 仓库根目录；数据库连接遵循下文的测试库护栏。
CWQ_REQUIRE_PG_CLIENT=1 backend/.venv/bin/python -m pytest -q -n 3 -ra --tb=short
npm --prefix frontend test
npm --prefix frontend run build
bash deploy/tests/test_deploy.sh
backend/.venv/bin/python backend/scripts/plugin_e2e.py
```

本轮没有重新执行 Docker 镜像构建，也未执行远程 CI；下方 Docker 记录属于初始宿主阶段，使用的是当时的迁移 head。正式使用本阶段功能需要先迁移到 `0027_plugin_imports` 并重新构建前端。导入完整内容的到期清理 CLI 已交付，自动调度仍待部署方配置。

## 初始宿主已交付范围（历史记录）

| 阶段 | 实现与验收入口 |
|---|---|
| P0 契约及六项原型 | 整包 Ed25519/RFC8785、真实两个 OS worker、生产构建下 native import/CSP、独立迁移、backup v2、按用户授权；每项都有失败断言 |
| P1 注册与包管理 | 主迁移仅追加 `0026`；上传容量/路径/类型/签名/扫描门禁；registry/attempt/grant 审计；staging/journal/reconcile |
| P2 受控后端加载 | 每 worker 独立预加载；受限 registrar/context；共享 generation、租约及全 worker 屏障；请求前实时授权；异常降级 |
| P3 前端与管理页 | 当前用户 manifest；同源只读资产；动态路由/导航/tabs；用户搜索、选择、保存与撤销；身份切换和失败清理 |
| P4 数据、SDK、部署及文档 | 每插件 Alembic、实时表 metadata、backup v2/v1 reader、窄 SDK、签名示例、持久目录及 supervisor、CI 浏览器作业 |

下表记录 SDK 1.0 初始宿主的组合验收，保留作历史基线；本阶段最新结果以上方 SDK 1.1 追加验收为准。没有生产上线或人工批准记录。

## 初始宿主检查结果（SDK 1.0 历史记录）

| 检查 | 结果 |
|---|---|
| 后端全量 pytest，真实 PostgreSQL、pg_dump 和 psql，`-n 3 -rA` | 1783 项通过、1 项按环境跳过；其中 75 项插件测试全部通过 |
| 故障恢复及 worker 定向检查 | 11 项通过；包括进程硬退出、实际数据库连接终止、两个 OS worker 和代次交叠 |
| 前端 `node --test tests/*.test.mjs` | 43 项通过，0 失败；磁盘上 27 个测试文件全部被发现 |
| 前端生产构建 | 通过 |
| `deploy/tests/test_deploy.sh` | 47 项通过，0 失败；含屏障失败阻断、运行目录保留 |
| 真实浏览器演练 | 通过：管理员安装、零默认授权、用户选择、两 worker 重启、CSP/资产/API 鉴权、撤权/再授权、页面失败降级、数据读写、v2 恢复、停用重启、保留表卸载 |
| Docker | 真实构建及默认 CMD 启动通过：非 root、可写持久目录、主迁移到 `0026`、Gunicorn 两 worker、health/ready、插件状态检查；临时开启模式验证两个实际 worker 共享 generation |
| 主迁移不变性 | 前 25 条迁移保持原样；插件安装/升级前后主 `alembic_version` 不变 |
| CI | 已添加真实浏览器作业并保留全量测试、测试发现数、数据库护栏、构建与部署检查；远程 CI 需推送后执行 |

测试使用独立的本机 PostgreSQL 测试库和临时签名密钥、包目录。pytest 的 schema 重建只针对测试库；浏览器演练只允许 loopback 且库名匹配 `[a-z][a-z0-9_]*_plugin_browser_test`。本机 PostgreSQL 服务端是 16；数据库备份测试调用匹配的真实 16 版 `pg_dump`/`psql`，并强制 `CWQ_REQUIRE_PG_CLIENT=1`，没有用 stub 跳过恢复。

## P0 成功与失败证据

| 原型 | 成功路径 | 失败路径及对应文件 |
|---|---|---|
| 完整包签名 | 示例离线构建、文件清单核验、canonical payload 摘要及验签 | Python/JS/迁移/资源篡改、未知/撤销密钥、缺签名、重复/穿越/链接、容量和危险代码被拒绝；`test_plugin_security.py`、`test_plugin_manifest.py`、`test_plugin_package_cli.py` |
| 多 worker | 同 generation 两个独立 OS 进程均可处理已授权请求 | 第二 worker 预加载故障时两个端口均稳定返回 503；旧 generation 拒绝，新 cohort 等待旧 lease；`test_plugin_workers.py`、`test_plugin_supervisor.py` |
| 浏览器资产 | 生产构建及现有同源 CSP 下原生 ESM 加载不带 Bearer，API 带 Bearer | 匿名 API 401、未授权 404；错误模块显示扩展降级页，核心仍可访问；`test_plugin_assets.py`、`tools/uitest/plugin_e2e.mjs` |
| 独立迁移 | 插件独立 version table；升级只执行新增 revision 并保留原行；主链不变 | 迁移异常/越界、非法 scope/FK、真实约束/触发器漂移均拒绝；启动不执行 DDL；`test_plugin_migration.py`、`test_plugin_guardrails.py`、`test_plugin_lifecycle.py`、`test_plugin_loader.py` |
| backup v2 | 实际插件行导出、dry-run、FK 顺序恢复、序列重置；book/global 闭包 | 未知插件、缺代码、digest/head/version/metadata 不同、越界行和缺父行在写入前拒绝；v1 历史 reader 保留；`test_plugin_backup.py` 及既有 backup 测试 |
| 用户授权闭环 | 管理 UI 搜索/勾选/保存，旧 token 在 grant 后无需重启即可使用 | 安装者及 admin 默认无 grant；其他用户拒绝；revoke 后下一请求拒绝且 manifest/routes/tabs/styles 清理；`test_plugin_user_access.py`、前端 plugin 测试及真实浏览器脚本 |

## 生命周期和崩溃恢复

`test_plugin_reconcile.py` 的实际子进程在迁移已提交后，于 rename 前、rename 后、registry 提交后直接 `os._exit(71)`，不会执行 Python `finally`。父进程验证 session advisory lock 和文件锁均释放，journal 保留；连续两次 reconcile 后只存在同一安装结果和同一插件 head，没有重复 DDL。另一个用例通过 `pg_terminate_backend()` 实际终止 registry 提交连接，核对恢复后的 digest、attempt、active 目录一致。

`test_plugin_lifecycle.py` 覆盖“先停用并完整重启”的硬要求、低版本/重复版本拒绝、升级前可验证备份、升级保留授权且保持禁用、新 revision 保留数据。浏览器脚本再用签名示例走 HTTP 安装与用户管理，启动整个两 worker supervisor，完成业务读写、真实 v2 数据恢复，再经 HTTP 停用、重启、卸载：保留两条插件业务行、撤销全部用户授权、旧 token 被拒绝、备份位置可查，最后显式 reconcile。

卸载后的普通 restore 仍要求恢复完全匹配的安装代码及 registry 现场。内部“保留表备份结构核验”只允许 dry-run，不能用于写入。没有自动 downgrade，也没有把文件系统 rename 和数据库 commit 描述成一个原子事务。

## 重跑方式

以下命令会重建隔离测试库的 public schema，只能指向专用测试数据库。先准备 PostgreSQL、匹配服务端的客户端工具和仓库锁定依赖。

```bash
# 仓库根目录；按本机实际端口填写，只能使用专用测试库。
export CWQ_TEST_DATABASE_URL='postgresql+psycopg://cwq:cwq123@127.0.0.1:15432/cwq_plugin_test'
export CWQ_REQUIRE_PG_CLIENT=1
backend/.venv/bin/python -m pytest -n 3 -rA
pnpm --dir frontend test
pnpm --dir frontend build
bash deploy/tests/test_deploy.sh
```

真实浏览器演练需要根目录 `npm ci`、`pnpm --dir frontend install --frozen-lockfile` 和 `npx playwright install chromium`，使用单独的数据库。运行前确保该库没有需要保留的数据。

```bash
CWQ_PLUGIN_E2E_DATABASE_URL='postgresql+psycopg://cwq:cwq123@127.0.0.1:15432/cwq_plugin_browser_test' \
  backend/.venv/bin/python backend/scripts/plugin_e2e.py
docker build -t cwq-api:plugin-check backend
```

脚本临时生成 Ed25519 测试密钥、构建完整签名示例包，启动生产前端构建及 CSP 同源代理，先在浏览器安装并授权，再重启两个后端 worker，最后完成恢复及卸载。退出时终止测试服务、清理临时包及测试 schema。截图写入 `output/playwright/`（已忽略，不进入发布包）：

- `plugin-install.png`：安装状态及可使用用户管理。
- `plugin-authorized.png`：授权用户的示例插件页面。
- `plugin-fallback.png`：失败插件降级及宿主页面。

示例包构建命令、完整 manifest/签名契约、SDK 及安装运维步骤见 [plugin-spec.md](plugin-spec.md) 和仓库 README。

## 验收边界

所有通过结果是契约行为的工程证据，不能证明任意 Python/同源 JavaScript 安全；该架构没有沙箱。签名、扫描、受限 SDK 也不能阻止可信代码主动退出进程、越过 SDK 或消耗资源。已读取的数据及浏览器模块缓存无法撤回，撤权约束下一次请求。租约是共享数据库上的就绪证据，依赖数据库和时钟；它不强制终止旧进程。

生产开启需运营方审阅实际包、配置可信公钥、保存匹配代码与备份，并接受 [plugin-risk.md](plugin-risk.md) 中的风险。工程交付保持 `PLUGIN_ENABLED=false`，未填造人工批准结论。
