# 云财务记账系统

公司内部使用的多账套财务记账系统。FastAPI + PostgreSQL + Vue3 + Element Plus。

一句话架构：**凭证和分录是唯一事实来源，余额、账簿、报表、打印全部由同一套聚合引擎派生。**
因此不存在「某个报表和总账对不上」这种事 —— 它们读的是同一份数据。

---

## 快速开始

### 1. 起数据库

```bash
docker run -d --name cwq-pg -p 15432:5432 \
  -e POSTGRES_USER=cwq -e POSTGRES_PASSWORD=cwq123 \
  -e POSTGRES_DB=cwq postgres:16
```

首次启动会自动建表并装载标准科目表，无需手动执行 SQL。

### 2. 起后端

```bash
cd backend
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/uvicorn app.main:app --reload --port 8000
```

- 接口文档：http://localhost:8000/docs
- 启动时会自动 `init_database()`：建表 + 装载标准科目表 + 创建演示账号

### 3. 起前端

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

演示账号：`admin / 123456`、`zhangsan / 123456`

---

## 功能清单

| 模块 | 路径 | 已完成 |
|---|---|---|
| 凭证 | `/vouchers` | 新增 / 修改 / 删除 / 查询 / 记账 / 反记账 / 批量 |
| 凭证整理 | `/vouchers/organize` | 断号体检、重编号、按日期排序重排 |
| 凭证导入 | `/vouchers/import` | Excel 三段式：暂存 → 预览 → 确认入库 |
| 账簿 | `/balance` `/general-ledger` `/detail-ledger` | 科目余额表 / 总账 / 明细账，含试算平衡 |
| 报表 | `/reports` | 资产负债表 / 利润表，**每格公式可配** |
| 打印 | `/print-templates` | 自由模板、纸型方向可调、实时预览 |
| 期末处理 | `/periods` | 结转损益 / 结账 / **批量反结账** / 年度结转 |
| 科目表 | `/subjects` | 树形维护，**每账套独立** |
| 系统 | `/users` `/audit` | 用户授权（OWNER / ACCOUNTANT / VIEWER）、审计日志 |

---

## 业务口径（已定稿，代码按此实现）

这几条是需求的硬约束，改动前请先看 `ARCHITECTURE.md`。

1. **月度结转 = 自动生成结转损益凭证**，一张收入类、一张成本费用类，`source=CARRY_FORWARD`
2. **报表每格公式自由可配**（柠檬云式），不是写死在代码里的模板
3. **不做辅助核算**（无核算项目/客户/供应商维度）
4. **不审核凭证**，只有 `未记账 UNPOSTED` / `已记账 POSTED` 两态
5. **凭证号作废不留号** —— 删除后序号可复用，改完凭证重新打印
6. **批量反结账** —— 从任意期间起一次解锁到最后一个已锁期间，区间凭证自动回未记账
7. **一公司多账套**，每个账套有独立的科目表和独立的期间

---

## 测试

```bash
cd backend

.venv/bin/python -m pytest                      # 159 个单元/接口测试
.venv/bin/python scripts/demo_e2e.py            # 69 项端到端流程
.venv/bin/python scripts/demo_modules.py        # 53 项模块冒烟
```

- pytest 用**独立测试库** `cwq_test`，每个用例一个全新账套，绝不碰开发库
- 两个 demo 脚本每次运行自建 uuid 账套，可以反复跑

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
  scripts/          # 端到端演示脚本
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
