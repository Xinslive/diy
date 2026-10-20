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
