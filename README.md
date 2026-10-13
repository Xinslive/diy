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

## 测试

```bash
cd backend

.venv/bin/python -m pytest                      # 995 个单元/接口测试（53 个测试文件，实测 `--collect-only`）
.venv/bin/python scripts/demo_e2e.py            # 69 项端到端流程
.venv/bin/python scripts/demo_modules.py        # 53 项模块冒烟
node onboard.mjs                                # 12 步浏览器开户全流程（需前后端都起着）
node report_design.mjs                          # 7 步浏览器报表设计器（需前后端都起着）
```

- pytest 用**独立测试库** `cwq_test`，每个用例一个全新账套，绝不碰开发库
- 两个 demo 脚本每次运行自建 uuid 账套，可以反复跑
- `onboard.mjs` 跑真实浏览器：新建账套 → 录期初（含反方向余额）→ 试算平衡 →
  保存回读 → 导出再导入 → 下载文件鉴权
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
    api/            # HTTP 层，只做参数转换和错误翻译
    models/         # 17 张表
    core/           # 配置、连接、鉴权、初始化 SQL
    seed/           # 标准科目表、内置报表模板
  scripts/          # seed_demo.py 演示数据 + demo_e2e/demo_modules 端到端脚本
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
