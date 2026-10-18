# 插件实施与验收记录

日期：2026-10-18。对应仓库根目录 `plan.md` 的 P0–P4。这里记录最终工作区的工程验收，未部署生产、未修改生产配置，插件仍默认关闭。生产责任人对实际发布者和实际插件包的人工风险接受见 [plugin-risk.md](plugin-risk.md)，本记录不代替该签署。

## 已交付范围

| 阶段 | 实现与验收入口 |
|---|---|
| P0 契约及六项原型 | 整包 Ed25519/RFC8785、真实两个 OS worker、生产构建下 native import/CSP、独立迁移、backup v2、按用户授权；每项都有失败断言 |
| P1 注册与包管理 | 主迁移仅追加 `0026`；上传容量/路径/类型/签名/扫描门禁；registry/attempt/grant 审计；staging/journal/reconcile |
| P2 受控后端加载 | 每 worker 独立预加载；受限 registrar/context；共享 generation、租约及全 worker 屏障；请求前实时授权；异常降级 |
| P3 前端与管理页 | 当前用户 manifest；同源只读资产；动态路由/导航/tabs；用户搜索、选择、保存与撤销；身份切换和失败清理 |
| P4 数据、SDK、部署及文档 | 每插件 Alembic、实时表 metadata、backup v2/v1 reader、窄 SDK、签名示例、持久目录及 supervisor、CI 浏览器作业 |

下表结果来自最终组合实现；没有将最终一次回归伪称为各中间阶段当时的独立发布记录。没有生产上线或人工批准记录。

## 最终检查结果

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
