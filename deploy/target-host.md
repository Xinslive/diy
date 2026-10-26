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

### 这台机必须有的两个 systemd drop-in（**不是** `.env`）

`BACKUP_ENCRYPTION_KEY_WARN_ONLY=true`（备份加密闸门的逃生口）在这台机上
**不能写进 `backend/.env`** —— 写了就**拒绝启动**。

闸门只查**持久化配置文件**（`app/core/config.py::persistent_escape_hatches`）：
命中即判定「生产配置校验不通过」→ 进程直接退出 → 站点 502。
这正是那道闸的设计意图：「逃生口只允许对**单次运行**生效」。

它必须放在**进程环境**里，也就是 systemd 的 drop-in：

| 文件 | 服务 |
|---|---|
| `/etc/systemd/system/cwq-api.service.d/10-backup-encryption-warn-only.conf` | API（**真正跑离站备份的是它**） |
| `/etc/systemd/system/cwq-oss-backup.service.d/10-backup-encryption-warn-only.conf` | 旧的 systemd 备份路径（手工补跑用） |

内容是一行：

```ini
[Service]
Environment=BACKUP_ENCRYPTION_KEY_WARN_ONLY=true
```

**为什么需要它**：这台机的 OSS 参数配在**库里**（`sys_setting`），所以启动闸门
（只看 env 层）不响，但**运行期**那道闸
（`app/backup/config.py::require_encrypted_ready`，按**生效配置**判）会响 ——
于是应用内调度任务 `system:oss_backup` 每天失败一次。
真实事故：2026-10-25 19:49 发布后，**2026-10-26 03:47** 的离站备份失败
（`EncryptionKeyMissing`），并发了失败通知；上一次成功是 10-25 03:47。

> **drop-in 不在版本库里**，重建机器就丢了 —— 丢了之后备份会**静默地**每天失败
> （只有失败通知，没有别的提示）。装机器时记得补上这一段。

**开着它的代价**：OSS 上的备份制品是**明文 gzip**。
正式配法是配一把 `BACKUP_ENCRYPTION_KEY`（生成：
`python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"`），
配好后就能把这两个 drop-in 删掉。**密钥要单独存一份** ——
它和 OSS AccessKey 放在一起，等于「拿到桶读权限就能解开所有备份」。

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
