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

忘了给的后果**不是报错**：脚本会对着 `/opt/cwq` 装 unit、装 logrotate、
建备份目录 —— 每一步都可能「成功」，而真正在跑的那套系统一点没变。
（`--check-only` 里会直接露出马脚：它会显示「后端依赖: 缺失 .venv」。）

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
bash deploy/deploy.sh
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
