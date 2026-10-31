# 部署目标机

> **一句话：本项目只在 `43.138.178.174` 上部署。**
> 其余机器上的本项目痕迹已于 2026-10-24 全部清理。

---

## 一、当前状态

| 项 | 值 |
|---|---|
| 生产机 | `43.138.178.174` |
| SSH | `ssh -i <私钥> root@43.138.178.174` |
| 操作系统 | Debian 13 |
| nginx | 1.26.3（源码编译，`/usr/sbin/nginx`） |
| PostgreSQL | 16.15（宿主机，`/usr/lib/postgresql/16/bin/`） |
| 应用根 | `/www/wwwroot/acct.xins.live` |
| 域名 | `acct.xins.live`（经阿里云 ESA CDN 回源）、`test.xins.live`（直连） |
| 目标机授权标记 | `/etc/cwq-deploy-target`（内容：`43.138.178.174`） |

---

## 二、已下线的机器

历史上本项目部署过三台机。前两台在 **2026-10-24** 清理完毕。

### 8.155.171.57（`invoice-vps`）

宝塔布局，与 **9 个子域共用一台主机**（tax / work / ai / hook / im /
invex / analysis / web / aa 等），nginx 1.31.3 + PG 15，
应用根 `/www/wwwroot/acct.xins.live`。

**已删除**（本项目相关）：

- 应用目录 `/www/wwwroot/acct.xins.live/`（1.2 GB）与 `/opt/cwq`
- 数据库 `cwq` 与角色 `cwq`（该机 PG 现只剩 `postgres` / `template*`）
- systemd 单元 `cwq-api.service`、`cwq-backup.*`、`cwq-oss-backup.*`、
  `cwq-scheduler-watchdog.*`、`cwq-cert-expiry-check.*`、`cwq-cert-sync.*`
- nginx 站点配置 `acct.xins.live.conf*`
- `cwq-cert-expiry-check` 的证书目录与状态文件
- 调度 tick 脚本与 crontab 条目
- 系统用户 `cwq`
- 证书收件目录 `/root/cert-drop/`

**保留未动**（与本项目无关）：

- 其余 9 个子域的 nginx 配置、证书目录与站点数据 —— **全部照常服务**
- `/usr/local/sbin/cwq-cert-apply.sh` —— 该机各站点**共用**的证书生效工具
  （第三方证书面板 sas 会调用它）。它的默认配置已从已删除的
  `acct.xins.live` 改为仍在本机服务的 `xins.live`；带域名参数调用时
  参数内的目录优先，不受影响。
- 宝塔面板自身配置、第三方证书面板 sas、以及所有非 `cwq` 的账号与文件

### 110.42.44.53（`ningbo-stotk-cache`）

更早的一版，`/opt/cwq` 布局，与 **19+ 站点共用**。

**已删除**：`/opt/cwq`（812 MB）、`/www/wwwroot/acct.xins.live`、
数据库与角色 `cwq`、`cwq-api.service`、`cwq-backup.*`、nginx 站点配置、
系统用户 `cwq`。

**保留未动**：其余 19+ 站点（api / note / img / yun / new / xins /
desk / im / app / sas 等）—— 全部照常服务。

> `im.xins.live` 反代到 `127.0.0.1:2283`，该端口无服务监听，
> 所以它返回 502。**这是既有故障，与本次清理无关** —— 它的上游服务
> 本项目从未部署过，站点目录从 2026-08-28 起就是空的。

---

## 三、发布时的目标机守卫

`deploy/deploy.sh` 在发布开始前核对本机身份（`check_deploy_target`），
对不上**直接退出**，不做任何改动。

### 三道判据

| 顺序 | 判据 | 作用 |
|---|---|---|
| 1 | **下线主机名单** | `8.155.171.57` / `110.42.44.53`。**永远先查这一道** |
| 2 | **目标机授权标记** | `/etc/cwq-deploy-target`，内容须与本机出口 IP 一致 |
| 3 | **内置名单** | 没有标记文件时，只有出口 IP 是 `43.138.178.174` 才放行 |

**为什么下线名单要永远先查**：授权标记是「这台机被授权了」的意思，
下线名单是「这台机明确不许发布」。如果顺序反了，把生产机的标记文件
拷到老机上就能放行 —— 那正是这道闸最该拦住的事。
**授权不能推翻禁令。**

### 判据为什么用公网出口 IP

- **不是 hostname**：云厂商会改（老机 `iZf8zah6zow0n8277dekxvZ`，
  新机 `VM-0-15-debian`），拿它当判据等于「换个名字就绕过闸门」。
- **不是网卡地址**：云主机普遍是 NAT 布局，网卡上只有内网地址
  （当前生产机是 `10.1.0.15`），而它对外的身份是弹性公网 IP
  `43.138.178.174`。拿网卡地址当判据的话：新机会被误杀（合法发布被挡），
  老机则**认不出来**（正是最该拦的那台）。

出口 IP 优先问外部服务拿（`ifconfig.me` → `checkip.amazonaws.com` →
`ipinfo.io` → `api.ipify.org`，**要多试几个** —— 实测 `api.ipify.org`
在当前生产机上不通，而其余三个正常）；拿不到才退回网卡地址。

### 沙箱与演练

```bash
CWQ_ALLOW_ANY_HOST=1 deploy.sh     # 只在沙箱/演练环境用
CWQ_HOST_IP=1.2.3.4 deploy.sh      # 显式指定判据（测试用）
```

**生产环境不要设 `CWQ_ALLOW_ANY_HOST`** —— 它存在的唯一目的是让本地
演练能跑通。

**`CWQ_HOST_IP` 也是逃生口，不要在生产机上设。** 它长得像个参数，
其实是**替换掉目标机守卫判本机身份用的那个 IP**：注成 `43.138.178.174`，
标记文件那道与内置名单那道就都过了 —— 于是「以为在 A 机、实际在 B 机」
（两个库各自推进、调度任务被领两遍）又被放行了一次。脚本检测到它被设置时
会打一条 `⚠️` 告警。

两个变量都登记在 `backend/app/core/escape_hatches.py` 的部署侧逃生口清单里，
CI 有一条断言守着「`deploy.sh` 里出现的每个 `CWQ_*` 名字都登记过」。

### 换机器时怎么办

1. 新机上放 `/etc/cwq-deploy-target`，内容写新机的公网 IP；
2. 把 `deploy/deploy.sh` 里 `CWQ_RETIRED_HOSTS` 的旧 IP 挪进去
   （旧机就此下线）；
3. 同步更新本文件与 `deploy/tuning.md` 第十一节的主机状态数字。

### 怎么发布到这台机器（参数一个都不能少）

**这台机器的布局不是脚本的默认值**，所以 `APP_DIR` 与 `BACKUP_DIR` **必须显式给**：

| 变量 | 这台机要给的 | 脚本默认值（**别用**） |
|---|---|---|
| `APP_DIR` | `/www/wwwroot/acct.xins.live` | `/opt/cwq` |
| `BACKUP_DIR` | `/www/wwwroot/acct.xins.live/backups` | `/opt/cwq/backups` |
| `CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR` | `1` | `0` |

**`CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1` 是 2026-10-25 补上的**（§3.41 的闸门
在这天上线）。`deploy/nginx/*.conf` 里的 `root` 写死的是 `/opt/cwq/frontend/dist`，
而 nginx 配置**不在发布脚本的渲染范围**（这台机上同域名有两份落点，不能让脚本去改
全局 nginx）。所以脚本现在会**明确拒绝**在非 `/opt/cwq` 的根上发布：

```
❌ APP_DIR=/www/wwwroot/acct.xins.live 不是 /opt/cwq —— 拒绝发布。
```

**这台机的 nginx 已经手工配好了**（`nginx -T` 里
`root /www/wwwroot/acct.xins.live/frontend/dist;` 与
`alias /www/wwwroot/acct.xins.live/downloads/;`），所以放行是安全的 ——
放行后脚本**不会**去改 nginx，只会在发布后体检里验「线上跑的是不是刚发的那一版前端」
（`runtime-config.js` 里的 version 对账）。

> **别把这个开关加到别的机器上**：它是「这台机的 nginx 已经手工配好了」的
> **断言**，不是「绕过检查」的开关。别的机器要换应用根，得先把它的 nginx 配好，
> 或者让它也用 `/opt/cwq`。

### 这台机的 `.env` 必须有的两行（缺了就**拒绝启动**）

`backend/app/core/config.py::validate_production_settings` 是**启动闸门**：
不满足就抛异常、**进程直接退出**。这台机的 `.env` 里必须有：

```
TRUSTED_PROXIES=127.0.0.1
MCP_ALLOWED_HOSTS=acct.xins.live,acct.xins.live:*,test.xins.live,test.xins.live:*
```

**`MCP_ALLOWED_HOSTS` 两种写法都要给**（传输层的匹配规则是「精确命中，
或 host 以『域名:』开头」—— 后者**要求带冒号**）：

| 写法 | 匹配 | 漏了会怎样 |
|---|---|---|
| `acct.xins.live` | 不带端口的 Host（**nginx 传的 `$host` 就是这种**） | 每个 MCP 请求都被挡成 **421** |
| `acct.xins.live:*` | 带端口的 Host（`acct.xins.live:443`） | 走端口访问时同样被挡成 **421** |

**只写 `acct.xins.live:*` 是不够的** —— 那会让「启动闸门过去、站点起来、
但 MCP 端点一个请求都进不去」。这真踩过（2026-10-25 那次发布）。

> **起不来了怎么办**：服务会**直接退出**，`systemctl is-active cwq-api` 显示
> `inactive`，原因在 `backend/logs/api.err.log` 的最后一段（不是 journal ——
> 应用的日志在自己的文件里）。`journalctl -u cwq-api` 只有 systemd 那几行。

忘了给的后果**不是报错**：脚本会对着 `/opt/cwq` 装 unit、装 logrotate、
建备份目录 —— 每一步都可能「成功」，而真正在跑的那套系统一点没变。
（`--check-only` 里会直接露出马脚：它会显示「后端依赖: 缺失 .venv」。）

### 离站备份的加密：交给 **OSS 服务端加密**（2026-10-26 起）

本项目**不在客户端加密**备份制品了。静态加密由**桶上的默认加密规则**
（SSE-OSS / AES256）承担 —— 它对**不带 SSE 头的普通上传**同样生效
（实测：不带头传的对象，HEAD 回来同样带 `x-oss-server-side-encryption: AES256`）。

这台机的桶 `acct963`（`oss-cn-heyuan`）已经配好：

| 项 | 值 | 怎么确认的 |
|---|---|---|
| 服务端加密 | `AES256`（SSE-OSS，OSS 托管密钥） | `get_bucket_encryption()` → `sse_algorithm=AES256` |
| 保留策略 | WORM `Locked`，**30 天** | `get_bucket_worm()` → `RetentionPeriodInDays=30` |

> **读桶的加密状态要用 `get_bucket_encryption()`，别用 `list_buckets()`** ——
> 后者返回的 `BucketInfo` 里**不填** `bucket_encryption_rule` 字段
> （oss2 2.19.1 只在 `GetBucketInfo` 那条路上填），照它读会得到
> 「没开」这个**假阴性**。

**客户端加密那一层为什么被移除**（连带移除的东西）：

* `BACKUP_ENCRYPTION_KEY` / `BACKUP_ENCRYPTION_KEY_WARN_ONLY` 两个配置项；
* 启动闸门与运行期那道「配了 OSS 就必须配备份加密密钥」；
* `app/backup/export.py` 里的 `encrypt_file` / `decrypt_file` /
  `encryption_key` / `is_encrypted`。

**取代它的是「看得见的事实」**：每轮备份与「测试连接」都会读一次桶的 SSE
状态并报出来（`app/backup/oss_client.py::server_side_encryption`）。
**它不是一道闸** —— 有人把桶上的 SSE 关掉，备份不会**被拦住**，但日志与
界面上会立刻显示「桶上没有开启服务端加密」。这是刻意的：SSE 是**桶上的
开关**，不在本项目的配置里。

**代价（要写下来，别到时候忘了）**：SSE 保护的是**静态数据**，
它**不**保护「拿到桶读权限的人」—— 那种人照样能让 OSS 替他解密。
客户端加密配一把**单独存放**的密钥时，那才是连 AK 泄露都不怕。

**WORM 保留策略带来的两个后果**（代码里已经处理，文档要记着）：

1. **保留期内的对象删不掉、也不可覆盖**（服务端报 `FileImmutable`）。
   所以「测试连接」的探针 key **每次都是新的**（UUID），否则第二次点那个
   按钮就会因为「覆盖被拒」而变红；探针对象删不掉，**那不算故障** ——
   写入已经验证成功了。
2. **到期清理遇到受保护的对象会跳过它**（不报「清理失败」）。30 天保留期
   与本系统「天级保留 30 天」几乎同时到期，边界上的那一份可能要留到
   保留期结束才删得掉。

这台机**没有 `gh`**（也不该有 GitHub 凭据），所以走 `ARTIFACT_DIR` 人工投放：

```bash
# 1) 本地：下载 CI 产物并校验
gh run download <run-id> -n "cwq-deploy-<完整 sha>" -D /tmp/cwq-release-<sha12>/artifact
( cd /tmp/cwq-release-<sha12>/artifact && sha256sum --check SHA256SUMS )

# 2) 传到目标机
scp -i <私钥> /tmp/cwq-release-<sha12>/artifact/* \
    root@43.138.178.174:/root/cwq-release-<sha12>/artifact/

# 3) 目标机上：发布脚本**从产物里取**，不手工拷贝
cd /root/cwq-release-<sha12>
tar -xzf artifact/cwq-deploy-<sha12>.tar.gz -C .
chown -R root:root deploy          # 归档里记的是 CI 构建身份
APP_DIR=/www/wwwroot/acct.xins.live \
BACKUP_DIR=/www/wwwroot/acct.xins.live/backups \
ARTIFACT_DIR=/root/cwq-release-<sha12>/artifact \
CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
bash deploy/deploy.sh
```

### 发布输出必须落盘（2026-10-31 补）

上面那段命令是**交互式**跑的 —— `deploy.sh` 的全部输出只进 SSH 的终端，
**脚本自己一个日志文件都不写**。所以一旦发布失败，机器上什么都不剩：
下次再发版时，谁也说不清上次是死在哪一步的。

真实案例（2026-10-31）：当天 11:02 有一次发布失败了，
`.releases/20261031-110252/` 只留下 `artifact/ backend/ dist/` 三样 ——
**没有 `database-before-migration.sql`，也没有 `db_backup.err`**，
说明它死在第 4 步（备份数据库）之前，也就是第 3 步「装运行时依赖」里。
但**具体哪一行报错，无法复原**：输出没留。同一份产物 12:27 再发一次就通了。

所以发布要**脱离 SSH 会话**跑，并把输出落盘：

```bash
cat > /root/run-deploy-<sha12>.sh <<'INNER'
#!/usr/bin/env bash
export APP_DIR=/www/wwwroot/acct.xins.live
export BACKUP_DIR=/www/wwwroot/acct.xins.live/backups
export ARTIFACT_DIR=/root/cwq-release-<sha12>/artifact
export CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1
cd /root/cwq-release-<sha12>
bash deploy/deploy.sh
INNER
chmod +x /root/run-deploy-<sha12>.sh
setsid bash -c '/root/run-deploy-<sha12>.sh > /root/deploy-<sha12>.log 2>&1; \
                echo $? > /root/deploy-<sha12>.exit' < /dev/null > /dev/null 2>&1 &
```

**为什么用 `setsid`**：它把发布进程放进一个**新的会话**，SSH 断连时的
SIGHUP 送不到它 —— 而 `deploy.sh` 自己有 trap，收到 TERM 会触发回退。
也就是说，一次「本地网络抖动」会被变成「发布到一半被回退」。
**发布不是能在客户端断连的场景下继续跑的事情。**

**为什么还要把退出码写进另一个文件**：日志里没有退出码时，无法区分
「发布失败」和「日志还没写完」。

### 失败过一次，怎么定位它死在哪一步

`.releases/<时间戳>/` 里有什么，就说明走到了哪一步 —— 这是**脚本自己
留下的现场**，比翻聊天记录可靠：

| 目录里有 | 说明走到了 |
|---|---|
| 只有 `artifact/` | 第 1 步（取产物）就没过 |
| `+ backend/` `+ dist/` | 第 2 步（备份现场）过了，**死在第 3 步里或它之前** |
| `+ venv/` | 当时锁文件与现网**不一致**（一致的话不会有 venv 快照） |
| `+ database-before-migration.sql` | 第 4 步的数据库备份成功了，**迁移可能已经执行过** |

> `db_backup.err` 存在、而 `database-before-migration.sql` 不存在
> = 备份命令**被调用过且失败了** —— 真正的报错就在那份 err 里。
> 两者都没有 = 备份命令**根本没被调用**，失败在更早的地方
> （第 3 步装依赖就是嫌疑最大的一处：它要访问 PyPI，网络抖动就会失败）。

**发布后一定要验这两条**（发布脚本自己也会验，但它的提示指向
`/opt/cwq/backend/logs/api.err.log` —— **这台机上是错的路径**，
真实的日志在 `/www/wwwroot/acct.xins.live/backend/logs/api.err.log`）：

```bash
systemctl is-active cwq-api                       # 要 active
curl -fsS http://127.0.0.1:8000/api/health        # 要 200
curl -fsS --resolve acct.xins.live:443:127.0.0.1 \
  https://acct.xins.live/runtime-config.js | grep <sha12>   # 线上前端要是这一版
```

**发布脚本从产物里取**（2026-10-25 起）：产物里带着 `deploy/`，脚本会比对
「我正在跑的」与「产物里那份」的 sha256，不一致就中止。详见
ARCHITECTURE §11.19。

> 归档里的文件属主是 CI 的构建身份（uid 1001）。`deploy/` 是发布工具，归
> `root` 管；后端代码由 `deploy.sh` 自己 `--no-same-owner` 解压并归位到
> `cwq`。

---

## 四、证书

`*xins.live` 通配符证书**手工上传**（无 ACME 自动续签）。
外部应用（第三方证书面板 sas）通过 SSH 部署到新机：

```bash
scp -i <私钥> fullchain.pem privkey.pem root@43.138.178.174:/root/cert-drop/
```

上传后自动发生：`cwq-cert-sync.path` 触发 `cwq-cert-sync.service` →
校验配对 / 域名 / 有效期 / 链信任 → 备份旧证书 → 安装 → `nginx -t` →
reload → 验证线上供出的序列号。任一步失败自动装回备份。

> **只传新机这一台。** 旧机的 `/root/cert-drop/` 已随本项目移除，
> 外部应用的部署目标里不要再配旧机（配了会一直失败）。
> 旧机上其余子域的证书由 sas 走**宝塔 API** 单独续期，与本项目无关。

---

## 五、清理备份的位置

两台旧机的清理前备份在各自的 `/root/cwq-cleanup-backup-20261024/`：

- 旧机：nginx 站点配置 + systemd unit 文件 + `cwq-cert-apply.conf`
- 110.42：nginx 站点配置 + systemd unit 文件

**确认稳定后可以删。** 里面的 nginx 配置是宝塔形态的老写法，
新机用的是 `deploy/nginx/acct.xins.live.conf`（Debian 形态），
两者不能直接互换。
