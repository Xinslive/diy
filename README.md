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

登录页的**注册**页签存在，但**自助注册默认是关的**：
`config.py` 里 `registration_enabled: bool = False`，`REGISTRATION_ENABLED` 没配或
配成 false 时，调注册接口直接 **403**。公司内部要开，在 `backend/.env` 里显式
`REGISTRATION_ENABLED=true` 并重启后端。

> 三处必须一致，改任何一处都要同步另两处：`config.py` 的默认值、
> `backend/.env.example`、`ARCHITECTURE.md` §9.5。默认值是 `False`（默认关闭），
> **不要为了「让文档说得通」去改成 True** —— 那等于把刚收掉的口子重新打开。

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
| `/openings` | 期初录入：方向可逐行翻转，试算平衡实时提示 |
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
| 凭证 | `/vouchers` | 新增 / 修改 / 删除 / 查询 / 批量。**记账与反记账不在这里** —— 走「期末结账」的结账与反结账 |
| 凭证整理 | `/vouchers/organize` | 断号体检、重编号、按日期排序重排 |
| 凭证导入 | `/vouchers/import` | Excel 三段式：暂存 → 预览 → 确认入库 |
| 账簿 | `/balance` `/general-ledger` `/detail-ledger` | 科目余额表 / 总账 / 明细账，含试算平衡 |
| 报表 | `/reports` | 资产负债表 / 利润表，**每格公式可配 + 行列可增删改** |
| 打印 | `/print-templates` | 自由模板、纸型方向可调、实时预览 |
| 账套管理 | `/books` | 新建账套、切换账套、看结账进度 |
| 期初余额 | `/openings` | 按科目分组录入、**借贷实时试算平衡**、Excel 导入导出 |
| 期末结账 | `/periods` | 结转损益 / 结账 / **批量反结账** / 年度结转 |
| 科目表 | `/subjects` | 树形维护，**每账套独立** |
| 系统 | `/users` `/audit` | 用户授权（OWNER / ACCOUNTANT / VIEWER）、审计日志。仅管理员可见 |
| 扩展 | `/plugins` / `/plugin/{id}` | 管理员安装签名插件、重启生效、指定使用用户；能力与用户授权分别检查。默认关闭 |
| 数据安全 | `/data-security` | 阿里云 OSS 备份：配置 / 连通性测试 / 立即备份 / 运行历史。**仅管理员可见** |

---

## 受信任插件（默认关闭）

系统支持签名 `.cwp` 全栈扩展，包括后端 API、预编译前端和独立迁移。插件与宿主同进程、同操作系统权限，**没有沙箱**；只安装经人工审阅、受信任发布者签名的代码。扫描用于拦截明显危险行为，不保证任意代码安全。规范、限制和验收记录分别见 [插件规范](docs/plugin-spec.md)、[风险记录](docs/plugin-risk.md)、[验证记录](docs/plugin-verification.md)。

首次启用前先执行主迁移 `alembic upgrade head`（新增 `0026_plugin_registry`），在服务环境中配置 `PLUGIN_ENABLED=true`、`PLUGIN_TRUSTED_KEYS`（`key_id → base64 Ed25519 公钥` 的 JSON），并设置持久化的 `PLUGIN_ROOT`。systemd 默认工作目录下为 `backend/data/plugins`，Docker 为 `/app/data/plugins`；部署产物不包含、也不覆盖这个运行目录。Docker 应持久化挂载 `/app/data`。配置项及配额见根目录 `.env.example`。

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

---

## 业务口径（已定稿，代码按此实现）

这几条是需求的硬约束，改动前请先看 `ARCHITECTURE.md`。

1. **月度结转 = 自动生成结转损益凭证**，一张收入类、一张成本费用类，`source=CARRY_FORWARD`
2. **报表每格公式自由可配**（柠檬云式），不是写死在代码里的模板
3. **不做辅助核算**（无核算项目/客户/供应商维度）
4. **不审核凭证**，只有 `未记账 UNPOSTED` / `已记账 POSTED` 两态
5. **凭证号作废不留号** —— 删除后序号可复用，改完凭证重新打印
6. **批量反结账** —— 从任意期间起一次解锁到最后一个已锁期间，区间凭证自动回未记账
7. **一个账套就是一个完整的记账单元**，公司名只是账套的属性；同一家公司可以开多个账套，每个账套有独立的科目表和独立的期间
8. **期初余额一律「借正贷负」存储**，与账内其它地方同一口径。
   科目属性只是「通常方向」，不是强约束 —— 应收账款出现贷方余额（客户预付挂账）、
   应交税费为负（留抵）都是真实业务，界面按行给方向开关，反方向的行标「反」。
   存、取、Excel 导出再导入三个环节都按实际方位走，往返一次不会把方向洗掉

---

## 数据安全：备份到阿里云 OSS

界面在 **系统管理 → 数据安全**（`/data-security`，仅管理员可见）。填 OSS 参数，
点「测试连接」验通了之后，剩下的交给 `cwq-oss-backup.timer` 每天凌晨 03:47 自己跑。

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

### 启用定时备份

```bash
sudo cp deploy/systemd/cwq-oss-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cwq-oss-backup.timer
systemctl list-timers cwq-oss-backup.timer    # 看下次触发时间
```

03:47 是刻意的：本机原有的数据库 dump 在 03:17，两件都是重 IO 的活，
同一分钟起跑会互相抢磁盘。`Persistent=true`，关机期间错过的会补跑。

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
| GET | `/api/system/backup/runs` | 运行历史（状态 / OSS key / 字节数 / SHA256 / 行数 / 错误全文） |

五个都要求管理员。

「测试连接」是**写探针再删**，不是 `head_bucket`：只读权限的 Key 在
`head_bucket` 上是绿的，真备份时却写不进去 —— 那才是最难在事后发现的形态。

---

## 测试

```bash
cd backend

.venv/bin/python -m pytest                      # 全量单元/接口测试；当前数量以收集与验证记录为准
.venv/bin/python scripts/demo_e2e.py            # 69 项端到端流程
.venv/bin/python scripts/demo_modules.py        # 53 项模块冒烟
node onboard.mjs                                # 19 步浏览器开户全流程（需前后端都起着）
node report_design.mjs                          # 7 步浏览器报表设计器（需前后端都起着）

cd ../frontend && npm run test                  # 全量前端测试（node --test）
```

- 插件专项及完整回归命令见 [验证记录](docs/plugin-verification.md)。真实浏览器演练使用专用 `*_plugin_browser_test` 库和临时签名密钥，不能传业务库连接串。
- pytest 用**独立测试库** `cwq_test`，每个用例一个全新账套，绝不碰开发库
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
- **期初余额是推导值**：平时不物化任何快照，反结账只要删快照即可自动恢复可写。
  这是「批量反结账」能做到一键完成的前提。
