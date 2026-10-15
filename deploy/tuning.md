# 部署机调优参考（2026-10-15）

这份文件把**压测实测出来的数字**与**运维参数**对到一起。它不是通用最佳实践
清单 —— 每一项都写明「为什么是这个数」，以及「什么情况下要改」。

数据来源：`压测基线报告_2026-10-15.md`（60,000 分录 / 320 科目 / 1 账套，
macOS + PostgreSQL 16 容器）。

> **定稿前必须在 Linux 上复测。** 本机是 macOS + Docker Desktop，
> 绝对值不能直接当作 VPS 上的表现。

---

## 一、当前实测占用（定容量的依据）

| 组件 | 稳态实测 | 说明 |
|---|---|---|
| uvicorn 父进程 | ~28 MB | 只负责 fork 与信号 |
| 每个 worker | ~114 MB（延迟导入后 ~104 MB） | 见下 |
| API 合计（2 worker） | ~250 MB | 压测负载下峰值 ~330~430 MB |
| PostgreSQL 16 | 实测占用 ~200 MB（`docker stats`） | 含 shared_buffers |
| 磁盘：venv + dist + .releases×3 | ~600 MB | |

### worker 内存的两个已知构成

| 构成 | 大小 | 可否优化 |
|---|---|---|
| 解释器 + 框架（fastapi/sqlalchemy/psycopg） | ~50 MB | 否 |
| openpyxl + lxml + olefile | **~10 MB** | **已优化：改为延迟导入** |

---

## 二、连接池：**不要调大**

实测（60,000 分录账套，并发 16）：

| 配置 | 总 RPS | ledger-1 p50 |
|---|---|---|
| `pool_size=5` + `overflow=10`（默认） | **5.5** | 375 ms |
| `pool_size=20` + `overflow=20`（上限 80） | **4.0** | 584 ms |

**放大连接池让系统慢了 27%。** 连接数不是瓶颈；更多并发只会让更多查询去抢
PG 的同一批 buffer，放大争用。

> 若调整 `db_pool_size` / `db_pool_max_overflow`，**必须同时核算**
> `worker 数 × (pool_size + max_overflow)` 与 PG 的 `max_connections`。
> 这是一条耦合的等式，不是两个独立旋钮。

---

## 三、worker 数：**保持 2**

| 配置 | 总 RPS | ledger-1 p95 | 服务 RSS 峰值 |
|---|---|---|---|
| 2 worker | 5.5 | 553 ms | 356 MB |
| 4 worker | 5.6 | 787 ms | **634 MB** |

加 worker 不涨吞吐、内存翻 78%。瓶颈在 PG 侧（实测 PG 占 400~450% CPU，
API 只占 210%），应用侧再加进程只是让更多请求同时去挤 PG。

> 前提：若日后解决了 PG 侧瓶颈（见第六节），这条结论需要重测。

---

## 四、PostgreSQL 参数

按物理内存给出，**按 2 vCPU / 4 GB 档**（推荐配置）：

| 参数 | 建议值 | 理由 |
|---|---|---|
| `shared_buffers` | `1GB` | 内存的 25%。实测工作集约 200 MB，给足余量 |
| `effective_cache_size` | `3GB` | 内存的 75%，**只给规划器做参考，不占内存** |
| `work_mem` | `8MB` | **按连接数反推**：30 条连接 × 8 MB = 240 MB，最坏情况可控 |
| `maintenance_work_mem` | `256MB` | 只给建索引/改表这类一次性操作 |
| `max_connections` | `50` | 应用稳态只用 30，默认 100 偏大 |
| `max_wal_size` | `2GB` | Excel 导入会产生大量 WAL |

### 为什么 `work_mem` 要按连接数反推

`work_mem` 是**每个排序/哈希节点**的额度，30 条连接同时各跑一个报表聚合就是
30 份。默认 4MB × 30 = 120 MB 尚可，但一旦调大到 64MB 就是 1.9 GB ——
**在 4 GB 机器上直接把 PG 推到 swap**。这正是「调大 work_mem 提升查询」
最常见的翻车方式。

### 验证方法

```bash
# 看实际峰值而非配置值
psql -c "SHOW work_mem;"
# 看有没有落盘排序（落盘 = work_mem 不够）
psql -c "EXPLAIN (ANALYZE, BUFFERS) <你的聚合查询>;" | grep -i "Sort Method"
# Sort Method: external merge  Disk: xxx  ← 说明 work_mem 不够
```

---

## 五、nginx 主配置

以下三条属 `http {}` / main 级，**必须改 `/etc/nginx/nginx.conf` 主配置**，
`conf.d/cwq.conf` 里写不了（写在 conf.d 反而会因为主配置的 include 规则被
加载两次）。`cwq.conf` 文件头已说明这条边界。

```nginx
worker_processes auto;      # 跟随 CPU 核数
worker_rlimit_nofile 8192;  # 与 systemd 的 LimitNOFILE 对齐

http {
    sendfile on;            # 零拷贝发静态文件
    tcp_nopush on;          # 与 sendfile 配套；关掉它 sendfile 的收益会打折
    keepalive_timeout 65;
}
```

改完：`nginx -t && systemctl reload nginx`。

> `worker_processes auto` 与 API 的 `CWQ_API_WORKERS` 是**两件事**：
> 前者是 nginx 的进程数，后者是应用 worker 数。别一起调。

---

## 六、PG 侧瓶颈（真正的大头）

压测定位到的根因不在配置层，而在查询形状：

`LedgerEngine._query_movements` 原本每次请求都把**整个账套**聚合一遍
（317 末级 × 24 期间 = 7,608 组），而明细账只要 1 个科目 × 24 期间 = 24 组 ——
**97% 的聚合是白算的**。单次请求 85.9ms 里有 77.7ms 花在这上面。

已在 2026-10-15 修复（把科目条件下推到 SQL，并按范围分桶缓存），
明细账单科目降到 **12.6 ms**（7.1 倍）。

**如果换到 VPS 后 PG 仍是瓶颈，按这个顺序查：**

1. `EXPLAIN (ANALYZE, BUFFERS)` 看是否走 Index Only Scan
2. 看有没有 `Sort Method: external merge Disk:`（work_mem 不够）
3. 看 `log_min_duration_statement` 抓慢查询

---

## 七、发版：生产机不再需要构建工具链

改造后生产机**不需要** node / pnpm / pytest。可卸载：

```bash
apt-get purge -y nodejs
# pnpm / pytest 是随 .venv 与 node 装的，删 .venv 重装即可
```

保留：`pg_dump`（`db_backup.py` 的退路）、`curl`、`tar`、`sha256sum`、
`git`（仅 `--check-only` 与版本记录用）。

检查当前状态：

```bash
bash /opt/cwq/deploy/deploy.sh --check-only
# 输出里会直接告诉你 node/pnpm/pytest 是否还装着
```
