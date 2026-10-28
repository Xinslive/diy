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

**这一节有两套值，别混用。**

- **1.6 GB 档**（**当前生产机**，43.138.178.174）：下表「1.6 GB 档」列。
- ~~**4 GB 档**~~（`/opt/cwq` 那台机，110.42.44.53）：**已下线**。
  2026-10-24 该机已清理完毕，本项目不再部署到它。那张表保留在下面，
  是因为**换成别的小内存机器时还要照着「1.6 GB 档」重算一遍**，
  而这两张表就是那份算式的来源。

> ### ⚠️ 2026-10-24：1.6 GB 机器按 4 GB 档配 = 必然 swap
>
> 这台机总内存只有 **1.6 GB**。4 GB 档那套参数落在这台机上，光是 PG 自己就要：
>
> | 参数 | 4 GB 档 | 占 1600 MB | 算式 |
> |---|---|---|---|
> | `shared_buffers` | 1 GB | **64%** | 1024 MB，单它就吃掉三分之二整机 |
> | `wal_buffers` | auto | 2% | = shared_buffers/32 = 32 MB |
> | `work_mem` × `max_connections` | 8 MB × 50 | **25%** | 400 MB |
> | `maintenance_work_mem` | 256 MB | 16% | 建索引/autovacuum 时独占 |
> | **PG 下限** | | **≥ 1712 MB** | **> 全机 1600 MB** |
> | + nginx 40 + API 236 + OS 200 | | **≥ 2188 MB** | **= 全机的 137%** |
>
> `effective_cache_size = 3GB` 更是 **187%** —— 它不占内存，但**有害**：
> 规划器以为有 3 GB 缓存可用，于是低估磁盘 I/O、选错执行计划。
>
> **所以生产机必须用 1.6 GB 档那一列。** 这不是「保守一点」，
> 是照抄 4 GB 档会让 PG 在启动时就把自己算进 swap。

### 4 GB 档（`/opt/cwq` 布局那台机）

| 参数 | 建议值 | 理由 |
|---|---|---|
| `shared_buffers` | `1GB` | 内存的 25%。实测工作集约 200 MB，给足余量 |
| `effective_cache_size` | `3GB` | 内存的 75%，**只给规划器做参考，不占内存** |
| `work_mem` | `8MB` | **按连接数反推**：30 条连接 × 8 MB = 240 MB，最坏情况可控 |
| `maintenance_work_mem` | `256MB` | 只给建索引/改表这类一次性操作 |
| `max_connections` | `50` | 应用稳态只用 30，默认 100 偏大 |
| `max_wal_size` | `2GB` | Excel 导入会产生大量 WAL |

### 1.6 GB 档（**当前生产机**，43.138.178.174，2026-10-24 新增）

| 参数 | 建议值 | 算式 |
|---|---|---|
| `shared_buffers` | **`160MB`** | 10% × 1600。共享机上它是**永不回收**的常驻内存，必须小 |
| `work_mem` | **`2MB`** | 30 连接 × 2 = 60 MB 最坏（4 GB 档的 8 MB 按比例下来） |
| `max_connections` | **`30`** | = 2 worker × (pool 5 + overflow 10) = 30，与 `config.py` 的默认值精确对齐 |
| `maintenance_work_mem` | **`64MB`** | 夜间建索引独占；PG 峰值 = 200 + 210 + 64 ≈ 474 MB |
| `effective_cache_size` | **`640MB`** | 40% × 1600 ≈ shared_buffers 160 + 可用页缓存 ~480 |
| `max_wal_size` | **`512MB`** | 磁盘；2 GB 在小盘上会挤掉 `.releases` 的 dump 空间 |
| `autovacuum_max_workers` | **`2`** | 默认 3 × `autovacuum_work_mem` 会造成尖峰 |
| `autovacuum_work_mem` | **`32MB`** | 默认 64 MB × 3 workers = 192 MB 尖峰 |
| `max_parallel_workers` | **`4`** | PG 默认 8；每个并行 worker 独立吃 `work_mem` |
| `max_parallel_workers_per_gather` | **`2`** | 每个并行 worker 独立吃 `work_mem` |

**改完必须重启 PG**（`ALTER SYSTEM` 只改配置文件；而且这台机上的 PG
**不在容器里** —— 完整改法与判据见第十一节）：

```bash
# ⚠️ 用宿主机 psql，不是 docker exec —— 这台机上没有名为 cwq-pg 的容器
# ⚠️ 路径里的 16 是当前生产机（43.138.178.174）的 PG 大版本
sudo -u postgres /usr/lib/postgresql/16/bin/psql -d cwq -c "
ALTER SYSTEM SET shared_buffers='160MB';
ALTER SYSTEM SET work_mem='2MB';
ALTER SYSTEM SET max_connections='30';
ALTER SYSTEM SET maintenance_work_mem='64MB';
ALTER SYSTEM SET effective_cache_size='640MB';
ALTER SYSTEM SET max_wal_size='512MB';
ALTER SYSTEM SET autovacuum_max_workers='2';
ALTER SYSTEM SET autovacuum_work_mem='32MB';
ALTER SYSTEM SET max_parallel_workers=4;
ALTER SYSTEM SET max_parallel_workers_per_gather=2;"
```

### 1.6 GB 机器的内存预算（改完之后）

| 组件 | 稳态 MB | 依据 |
|---|---|---|
| OS + 宝塔面板 + sshd + cron | 200 | 宝塔自跑 PHP/MySQL，需实测 |
| 页缓存（可回收） | 150 | 给 PG 缓存外的查询留底 |
| nginx（master + 2 worker，含另 11 个子域） | 40 | 每连接约 50 KB |
| Docker 容器 ×3 | **待实测** | `docker stats --no-stream` —— 这一格是整个预算里最不确定的 |
| uvicorn 父进程 | 28 | 本文第一节 |
| uvicorn worker ×2 | 208 | 2 × 104 MB（延迟导入后） |
| 账簿聚合缓存 ×2 worker | 22 | `LEDGER_CACHE_MOVES_MAXSIZE=4` × 2.7 MB × 2（见下） |
| PostgreSQL（按 1.6 GB 档参数） | 300 | 见上表算式 |
| **合计（不含 Docker）** | **948** | 1600 − 948 = **652 MB** 给 Docker 与发版尖峰 |

> 账簿聚合缓存这一格在 2026-10-24 从 48 MB 降到了 22 MB：
> `moves` 缓存一条就是**整本账**的发生额聚合（压测规模 7,608 组 ≈ 2.7 MB），
> 原来的 maxsize=16 对它太大。现在 `moves` 单独用
> `LEDGER_CACHE_MOVES_MAXSIZE=4`（见 `config.py`）。

### 为什么 `work_mem` 要按连接数反推

`work_mem` 是**每个排序/哈希节点**的额度，30 条连接同时各跑一个报表聚合就是
30 份。默认 4MB × 30 = 120 MB 尚可，但一旦调大到 64MB 就是 1.9 GB ——
**在 4 GB 机器上直接把 PG 推到 swap**。这正是「调大 work_mem 提升查询」
最常见的翻车方式。在 1.6 GB 机器上同理：2 MB × 30 = 60 MB 才是能兜住的数。

> ⚠️ 4 GB 档那张表里 `work_mem=8MB` 与 `max_connections=50` **自身矛盾**：
> 正文按 30 条连接算 240 MB，表里的 50 会让最坏情况变成 400 MB。
> 4 GB 档的 `max_connections` 应改成 **30**（与 `config.py` 的
> `pool_size 5 + overflow 10` × 2 worker 精确对齐）。

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

---

## 八、`TRUSTED_PROXIES`：IP 维度的登录节流靠它才成立

**不配的后果不是「少一道防护」，是节流机制本身失效。**

登录节流有两层：

| 维度 | 存在哪 | 不配代理时 |
|---|---|---|
| 账号（`sys_user.failed_logins`） | 数据库 | **照常生效** |
| 来源 IP（`login_ip_throttle`） | 数据库，键是那个 `ip` | **失效** |

IP 那一层的键取自请求的来源地址。不配 `TRUSTED_PROXIES` 时后端按
**fail-closed** 处理：**忽略** `X-Forwarded-For`，只看 TCP 对端。
而这套系统只有一个 nginx 在前面，于是对应用来说来源永远是 `127.0.0.1`。

后果不是「攻击者换个 IP 就能绕过」（那种情况有账号层兜着），而是：

> **全站所有人共用同一个节流桶。**

一个来源反复试错，会把整个公司一起拖进节流窗口。这比没有这层防护更糟 ——
没有防护只是放过攻击者，有了这层但配错则是**误伤所有正常用户**。

### 它和 nginx 是一对，必须同时改

| 位置 | 要做的事 | 漏了会怎样 |
|---|---|---|
| `deploy/nginx/cwq.conf` | `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` | app 侧配了也拿不到头 |
| `backend/.env` | `TRUSTED_PROXIES=127.0.0.1` | 头发过来了也被丢弃 |

**只改一边等于没改**：nginx 发了头而 app 不信，等于没发；
app 信任了但 nginx 没发，等于所有来源都是 nginx 自己。

### 怎么配

```bash
# /opt/cwq/backend/.env
TRUSTED_PROXIES=127.0.0.1
```

- 多个代理时逗号分隔：`TRUSTED_PROXIES=127.0.0.1,10.0.0.5`
- **只填你自己的代理，不要填 `*` 或 `0.0.0.0/0`**：
  那等于重新信任任何人伪造的 `X-Forwarded-For`，节流立刻可被绕过
- 改完要 `systemctl restart cwq-api`，热加载不生效

### 为什么启动只告警不拒绝

`config.py::validate_production_settings` 里留空时打的是**告警**而不是拒绝启动：
`TRUSTED_PROXIES` 配错会同时废掉「取到正确 IP」和「不被伪造 IP」两件事，
而拒绝启动意味着**一台还没配好的机器连登录页都打不开**。
在这个取舍下，配置错误应该表现为「登录限流体验异常」这种可观察的降级，
而不是「服务起不来」。

所以 `deploy.sh` 第 7/7 步每次发布都会检查一遍 `.env` 里这一项，空的就醒目提示 ——
**这个检查不是阻断式的**，配错不会让一次本来成功的发布变成「发布失败」。

### 怎么验证真的生效

```bash
# 从别的机器访问（经 nginx），看节流表里记的是真实来源而不是 127.0.0.1
psql "$DATABASE_URL" -c 'SELECT ip, failures, blocked_until FROM login_ip_throttle ORDER BY last_failed_at DESC LIMIT 5;'
```

表里出现的是**真实客户端 IP** = 配对了；全是 `127.0.0.1` = 还没配。

---

## 九、写路径：版本号触发器的锁代价（2026-10-15 补）

`scripts/loadtest.py --write` 第一次量到了写路径。本机（macOS + Docker PG，
**未在 Linux 上复测**，口径同 §六），并发 4，自建临时账套、跑完删除。

| 场景 | 样本 | p50 | p95 | p99 | RPS |
|---|---|---|---|---|---|
| `write-voucher`（每张 2 行） | 94 | 36.3 ms | 66.5 ms | 70.3 ms | 15.3 |
| `write-voucher-bulk`（每张 500 行） | 18 | 156.6 ms | 191.2 ms | 194.2 ms | 2.9 |

**怎么读这组数。** 它回答的是「版本号触发器让写变慢了多少」：

* 语句级触发器让一次 5000 行导入只做**一次** `book` 行 UPDATE，
  而不是 5000 次。所以 500 行与 2 行的差距（36 → 157 ms）主要是
  分录本身的插入成本，不是版本号的锁。
* 想验证这一点可以直接看触发器定义：`tests/test_data_version.py::
  test_触发器必须是语句级` 会把「有人改成行级」变成红灯。
* 上界：并发 4 下 p95/p50 ≈ 1.8，写路径有排队但没有崩。

**写吞吐不可与 §三/§四 的读数字横向比** —— 写受磁盘与 PG 落盘影响远大于读。

### 顺带查出一个并发正确性问题（未修，待决策）

并发写同一账套时，**凭证号分配会撞**。实测 8 个并发请求建凭证，
1 个成功、7 个返回：

```
400 凭证号 记001 在 2026-01 已经有一张了（可能刚被另一条记录占用），请刷新后换一个号重试
```

**这不是数据损坏** —— `uq_voucher_period_no` 把它挡住了，账上没有重复号，
失败提示也是设计好的（让人换一个号重试）。

但它确实是个缺口，性质要说清楚：

* `test_organize_renumber_race.py` 覆盖了**重编号**路径的并发，
  而**正常建凭证时的取号**没有对应用例 —— 压测报告 §七第 2 条
  「凭证写入的锁竞争与凭证号分配并发正确性未纳入」说的就是这个空白。
* 多用户同时记账是常态，不是边缘场景。现在两个人同时点「新增凭证」
  有约 7/8 的概率要重试一次。
* 取号算法是账务关键路径上的东西，改它属于设计决策，
  **不在本轮性能收尾的范围内**，所以只记录不擅自动手。

要修的话有两个方向，取舍不同，需要拍板：
1. 取号时对「该期间已用号」加**行锁/串行化**，保证不撞 —— 正确但并发写会排队；
2. 撞号时**内部重试**若干次再报错 —— 保留并发度，但要把重试次数与退避写清楚。

### 复现

```bash
cd backend
.venv/bin/python scripts/loadtest.py --write --concurrency 4 --duration 6
```

写场景**自建临时账套、跑完自动删除**，不会碰演示数据。

---

## 十、限流开关（nginx 侧）与前端产物的目录布局（2026-10-20 补）

### 10.1 限流：两段文件，缺一不可

限流**默认关闭**，而且**刻意不给默认阈值** —— 阈值只能用业务峰值定，
而这套系统的真实峰值没有任何测量数据（月结日与平日的并发能差一个数量级，
凭空写一个数上去，要么形同虚设，要么在月结那天把全公司挡在门外）。

**开限流要建两个文件，指令上下文不同，不能只写一半：**

```bash
# 1) http 级：定义 zone（limit_req_zone 的合法上下文只有 http）
mkdir -p /etc/nginx/cwq-include
cat > /etc/nginx/cwq-include/cwq-rl-zone.conf <<'EOF'
limit_req_zone  $binary_remote_addr zone=cwq_api:10m rate=<阈值>;
limit_conn_zone $binary_remote_addr zone=cwq_api_conn:10m;
EOF

# 2) location 级：引用 zone
cat > /etc/nginx/cwq-include/cwq-rl-api.conf <<'EOF'
limit_req  zone=cwq_api burst=<突发量> nodelay;
limit_conn cwq_api_conn <并发连接数>;
EOF

# 然后：
nginx -t && systemctl reload nginx    # 宝塔上用 nginx -s reload
```

**删掉这两个文件即关闭。** 两个 include 都是**通配**（`cwq-rl-zone*.conf` /
`cwq-rl-api*.conf`），通配 include 在没有匹配文件时**不报错** ——
「文件不存在」就是干净的关闭状态。

> **以前这里只实现了一半。** 宝塔形态的 vhost
> （`deploy/nginx/acct.xins.live.conf`）只有 location 级那一半，
> 缺 http 级的 zone 定义 —— 照着建好文件去 reload，`nginx -t` 直接失败
> （`unknown limit_req_zone "cwq_api"`），**限流在这个部署形态下永远开不了**。
> 现在两份 vhost（`cwq.conf` 与 `acct.xins.live.conf`）都有这两半，
> 并且 `deploy/tests/nginx_conf_check.py` 每次 CI 都会**真的建出这两个文件
> 再跑一次 `nginx -t`** —— 「按文档启用」这条路径本身必须能过。

> **两种 nginx 形态只能选一种。** 独立 nginx（`cwq.conf`）与宝塔
> （`acct.xins.live.conf`）各自都声明了同名 zone，同时加载会报
> `limit_req_zone "cwq_api" is already bound to key ...`（直接 emerg）。

### 10.2 前端产物现在是「版本化目录 + 软链」

```
/opt/cwq/frontend/releases/dist-<commit>-<时间戳>/   真正的前端内容（一份一版）
/opt/cwq/frontend/dist -> releases/dist-<commit>-<时间戳>   nginx 的 root 指这里
```

- **nginx 的 `root` 路径没变**，所以这次改造不需要 reload nginx。
- 换版 = 把 `dist` 这个软链指向新的 release 目录（一次 `rename`），
  中间**没有「目录整个不存在」的窗口**（原来的两次 `mv` 有）。
- 上一版的 release 目录还在盘上（保留最近 3 份），所以回退之后
  旧前端引用的 `/assets/*` 仍然打得开。

运维要动前端时，改的是 `releases/dist-<commit>-<时间戳>/` 里的内容 ——
**不要直接改 `dist/`**（那是软链，改的是当前这一份，改完下次发布就没了）。

---

## 十一、生产机上手工做的事（**43.138.178.174**）

**仓库里的改动只是代码。** 这一节列的是**主机状态** —— 代码改不到，
必须在生产机上执行。每一条都给了判据。

> 数字来自 2026-10-24 的生产机实测：`nproc=2`、`Mem total 1973 MB`
> （约 1.9 GB，取整按 1.6 GB 档配）、无 swap、`oom_kill=0`。
> **先量再改** —— 别把别的机器的数搬过来。
>
> **2026-10-24 换机说明**：本节原先写的是旧机 `8.155.171.57`
> （PG 15、`/www/wwwroot/acct.xins.live` 宝塔布局）。
> 那一台已清理下线，本项目**只在 43.138.178.174 上部署**。
> 当前生产机是 **Debian 13 + PG 16 + `/www/wwwroot/acct.xins.live`**，
> 规格与下表一致，下面各条按新机复核过。

### 11.1 PostgreSQL 换成 1.6 GB 档

**⚠️ 这台机的 PG 不在 Docker 里。** 生产机跑的是 **Debian/Ubuntu 打包的
宿主机 PostgreSQL 16**（`/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main`），
监听 `127.0.0.1:5432`，由 `postgresql.service` 管。
**没有名为 `cwq-pg` 的容器** —— `docker exec cwq-pg psql` 照抄会直接报
`No such container`。

2026-10-24 在**当前生产机**上实测的**实际生效值**与第四节「1.6 GB 档」的对照：

| 参数 | 实测值 | 1.6 GB 档目标 | 要不要改 |
|---|---|---|---|
| `shared_buffers` | 20 MB | 160 MB | ⚠️ 偏低但可用（PG 16 默认；新机实测 2 GB 内存下够用，见下注） |
| `work_mem` | 2 MB | 2 MB | ✅ 已达标 |
| `max_connections` | 30 | 30 | ✅ 已达标 |
| `maintenance_work_mem` | 64 MB | 64 MB | ✅ 已达标 |
| `effective_cache_size` | 640 MB | 640 MB | ✅ 已达标 |
| `max_wal_size` | 512 MB | 512 MB | ✅ 已达标 |
| `min_wal_size` | 80 MB | 80 MB | ✅ 可不动 |
| `autovacuum_max_workers` | 2 | 2 | ✅ 已达标 |
| `autovacuum_work_mem` | 32 MB | 32 MB | ✅ 已达标 |
| `max_parallel_workers` | 4 | 4 | ✅ 已达标 |
| `max_parallel_workers_per_gather` | 2 | 2 | ✅ 已达标 |
| `checkpoint_timeout` | 300 s | 300 s | ✅ 可不动 |
| `random_page_cost` | 4 | 4 | ✅ 可不动（SSD 调 1.1 收益不确定，先不动） |

> **`shared_buffers` 只有 20 MB 这件事**：PG 16 的默认值比 1.6 GB 档的
> 160 MB 更保守。当前生产机实测 2 GB 内存、且**不再与 11+ 子域共用**
> （旧机那种「同机跑 19 个站点」的压力已经没有了），20 MB 实测够用。
> 若日后发现查询计划变差、或命中率掉下来，再按上面的 `ALTER SYSTEM`
> 提到 160 MB —— 改法见本节下方（把路径里的 15 换成 16）。

改法（`ALTER SYSTEM` 写进 `postgresql.auto.conf`，优先级高于
`postgresql.conf`，且不动主配置文件）：

```bash
# ⚠️ 用宿主机 psql，不是 docker exec
# ⚠️ 路径里的 16 是当前生产机的 PG 大版本（15 是已下线旧机的）
sudo -u postgres /usr/lib/postgresql/16/bin/psql -d cwq -c "
ALTER SYSTEM SET shared_buffers='160MB';
ALTER SYSTEM SET work_mem='2MB';
ALTER SYSTEM SET max_connections='30';
ALTER SYSTEM SET maintenance_work_mem='64MB';
ALTER SYSTEM SET effective_cache_size='640MB';
ALTER SYSTEM SET max_wal_size='512MB';
ALTER SYSTEM SET autovacuum_max_workers='2';
ALTER SYSTEM SET autovacuum_work_mem='32MB';
ALTER SYSTEM SET max_parallel_workers=4;
ALTER SYSTEM SET max_parallel_workers_per_gather=2;"
```

**其中 5 个参数必须重启 PG 才生效**（`shared_buffers` / `max_connections` /
`max_wal_size` / `max_parallel_workers` / `autovacuum_max_workers`），
其余是 `SIGHUP` 级 —— 所以**必须重启，不能只 reload**：

```bash
# 重启前先记下当前连接数，确认降到 30 不会把现有连接挤掉
sudo -u postgres /usr/lib/postgresql/16/bin/psql -d cwq -tAc \
  "select count(*) from pg_stat_activity;"
systemctl restart postgresql
```

> **重启 PG 会断开 API 的连接池。** 池带 `pool_pre_ping`，重连是自动的；
> 但重启窗口内（几秒）接口会 500。**在低峰期做**，重启后立刻按 11.5 的判据
> 确认 API 自己爬起来了 —— 如果没爬起来，`systemctl restart cwq-api`
> （那不是故障，是池没恢复）。

**判据**：

```bash
sudo -u postgres /usr/lib/postgresql/16/bin/psql -d cwq -c \
  "SHOW shared_buffers; SHOW work_mem; SHOW max_connections; SHOW effective_cache_size;"
# 期望：160MB / 2MB / 30 / 640MB
systemctl show postgresql -p ActiveState        # active
```

### 11.2 systemd 单元

`deploy.sh` **不安装** `cwq-api.service`（它只 `systemctl restart cwq-api`），
所以必须手工渲染后装上去。

**⚠️ 生产机上已经有一份手工修过路径的 unit**
（`/etc/systemd/system/cwq-api.service`），它的路径类键已经是
`/www/wwwroot/acct.xins.live`（不是模板里的 `/opt/cwq`），
**不能直接拿仓库模板覆盖** —— 覆盖了服务就起不来。

**正确做法：用仓库那份模板，按本机布局渲染**：

```bash
# 1) 备份现有的手工 unit
cp -a /etc/systemd/system/cwq-api.service \
      /etc/systemd/system/cwq-api.service.bak.$(date +%Y%m%d-%H%M%S)

# 2) 从仓库模板渲染（把 /opt/cwq 换成生产路径）
sed -e 's|/opt/cwq/backups|/www/wwwroot/acct.xins.live/backups|g' \
    -e 's|/opt/cwq|/www/wwwroot/acct.xins.live|g' \
    deploy/systemd/cwq-api.service \
  > /etc/systemd/system/cwq-api.service

# 3) 装之前先让 systemd 自己验一遍语法（装坏了会起不来）
systemd-analyze verify /etc/systemd/system/cwq-api.service

# 4) 确认可写白名单三行齐全，且没有残留 /opt/cwq
grep -nE '/opt/cwq' /etc/systemd/system/cwq-api.service    # 应无输出
grep -n 'ReadWritePaths' /etc/systemd/system/cwq-api.service
#   期望：/www/wwwroot/acct.xins.live/backend/logs
#         /www/wwwroot/acct.xins.live/backend/data
#         /www/wwwroot/acct.xins.live/backups

systemctl daemon-reload && systemctl restart cwq-api
```

> **发布顺序是硬约束：先发代码，后装这一段。**
> `MemoryHigh=400M` 是按**优化之后**的稳态（**实测 290 MB**）定的，
> 而生产上**旧代码**的稳态实测是 **313 MB** —— 先装 unit 再发代码，
> unit 一装上就会把正在跑的旧版本节流掉，表现为「发版后接口变慢」，
> 而根因是「上限按新代码的数定的，却先套在了旧代码上」。

### 11.3 logrotate

**2026-10-25 起：`deploy.sh` 会自动装，不用手工。**

`unit` 里是 `StandardOutput=append:…/api.log`（append 不轮转）——日志只增不减。
以前这一步是手工的（2026-10-24 实测确认 `/etc/logrotate.d/` 里没有 cwq 相关文件），
它有两个坏处：**靠人记得**（换机就漏），以及**规则里写死路径**（写错就盯着一个
不存在的路径，看着装了、其实没转）。

现在发布时自动渲染并安装两条规则：

| 规则 | 管的文件 |
| --- | --- |
| `cwq-api` | `backend/logs/api.log`、`api.err.log` |
| `cwq-backup` | `backups/oss-backup.log`、`backups/backup.log` |

规则内容：`daily` + `size 50M` + `rotate 7` + `compress` + `copytruncate`。
装完立刻 `logrotate -d` 演练一次，演练报错会打进发布日志。

**手工装**（发布没跑成功时的补救）：

```bash
for f in deploy/logrotate/cwq-*; do
  sed -e "s|/opt/cwq/backups|实际备份目录|g" -e "s|/opt/cwq|实际应用根|g" \
    "$f" > "/etc/logrotate.d/$(basename "$f")"
done
logrotate -d /etc/logrotate.d/cwq-api
```

**不归这两条规则管的**（各有各的规则，别重复管）：

- nginx 的 access/error log —— Debian 自带的 `/etc/logrotate.d/nginx`（`rotate 14`）；
- PostgreSQL 的日志 —— Debian 自带的 `postgresql-common`；
- journald —— systemd 自己有上限（`SystemMaxUse`，默认磁盘的 10%）。

### 11.4 `.env`：三项建议显式写上

生产机当前的 `.env` **已经有的**：`CWQ_API_WORKERS=2`、
`LEDGER_CACHE_MAXSIZE=16`、`LEDGER_CACHE_TTL_SECONDS=60`、
`CWQ_BACKUP_WORK_DIR=…/backups/work`（已是推荐值）、
`INVOICE_STORAGE_DIR=…/backend/data/invoices`。

**下面三项在代码里都有默认值，不配也能正常工作**
（`ledger_cache_moves_maxsize=4`、`CWQ_API_MAX_CONCURRENCY=32`、
`CWQ_API_MAX_REQUESTS=2000`）。**建议显式写上**，理由是「配置文件要能读出
当前生效值」，而不是「不配就会出错」：

```
LEDGER_CACHE_MOVES_MAXSIZE=4
CWQ_API_MAX_CONCURRENCY=32
CWQ_API_MAX_REQUESTS=2000
CWQ_API_MAX_REQUESTS_JITTER=200
```

> `CWQ_API_MAX_CONCURRENCY` 注意 **MCP 的 SSE 连接会长期占住一个名额**
> （nginx 侧读超时 360 s）。真的不够用就调大。

### 11.5 发布后验收（判据）

```bash
# 服务与重启
systemctl show cwq-api -p ActiveState -p NRestarts -p MemoryCurrent
#   ActiveState=active   NRestarts=0
#   MemoryCurrent 应当明显低于旧版的 313 MB（优化省掉了账簿物化与上传缓冲）

# 双端健康
curl -fsS http://127.0.0.1:8000/api/health && echo   # 内网
curl -fsS https://acct.xins.live/api/health && echo   # 公网

# 启动参数（应当能看到四道护栏）
ps -eo args | grep '[u]vicorn app.main:app'
#   期望：--workers 2 --timeout-keep-alive 60
#         --limit-concurrency 32 --limit-max-requests 2000
#         --limit-max-requests-jitter 200 --no-access-log
```

### 11.6 ~~给 3 个 Docker 容器设内存上限~~ —— **撤回**

**⚠️ 2026-10-24 生产机实测：这台机的三个容器都不是本项目的。**

| 容器 | 内存占用 | 是什么 |
|---|---|---|
| `invoice-system-app` | 112.8 MiB | **别的业务**（发票系统） |
| `media-parser-backend` | 141.9 MiB | **别的业务**（媒体解析） |
| `frps_tz6c_frps_TZ6c_1` | 26.8 MiB | **别的业务**（内网穿透 frps） |

**本项目不对别人的容器设置内存上限。** 给 `media-parser-backend` 设 256 MB 上限，
是在**随时可能把别人的服务 OOM 掉** —— 它已经占了 141.9 MiB。
整台机的容器内存治理是**运维层面的独立决策**，需要先量出各容器的真实峰值、
并与各业务方确认 —— 不在本项目范围内。

> 同一台机还同时跑着另外 11 个子域，本项目只是其中一个乘客 ——
> 所以 unit 里给的是 `CPUWeight=50`（**竞争时让路**）而不是 `CPUQuota`
> （那是把整台机的 CPU 当成独占额度）。
