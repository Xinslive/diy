# 阿里云 ESA 回源双向校验（mTLS）

> 目标形态：源站**只认 ESA 的回源**，任何绕过 CDN 直打源站的流量都拿不到内容。
>
> ```
> 浏览器 ──HTTPS──▶ ESA 边缘
>                    └── 回源（HTTPS + mTLS）──▶ nginx ──▶ FastAPI / 静态目录
>                        ↑ 这里 ESA 会带上自己的客户端证书，
>                          源站验它的签发链 —— 对不上就 400
> ```

适用站点：`acct.xins.live`（源站 `43.138.178.174`，Debian 13 / nginx 1.26.3）。

---

## 0. 这件事由三方共同完成，缺一不可

| 谁 | 做什么 | 谁做 |
| --- | --- | --- |
| **源站 nginx** | 信任 ESA 根证书、开启客户端证书校验 | ✅ **已就位**（本文 §2） |
| **ESA 控制台** | 开启「回源双向校验」+「源站证书校验」，配置回源协议为 HTTPS | ⬜ **需要你在控制台操作**（本文 §3） |
| **DNS** | 把域名 CNAME 到 ESA | ⬜ 按你的接入进度 |

只做源站侧不做控制台侧 —— 没有任何流量会带证书，源站那边等于白配；
只做控制台侧不做源站侧 —— 握手失败，**全站 502**。顺序必须是先源站、后控制台。

---

## 1. 现状（2026-10-24 实测）

| 项 | 值 |
| --- | --- |
| 源站 | `43.138.178.174`，nginx `1.26.3`（`--with-http_ssl_module --with-http_v2_module`） |
| 源站配置 | `/etc/nginx/conf.d/acct.xins.live.conf`（= 仓库 `deploy/nginx/acct.xins.live.conf` 的新机形态） |
| 前置 CDN | **阿里云 ESA**（响应头 `server: ESA`、`via: ens-cache20.*`、`x-site-cache-status`；回源节点 `112.48.144.x`） |
| 回源协议 | HTTPS → 源站 443（握手实测 `TLS_AES_256_GCM_SHA384` / `TLS_CHACHA20_POLY1305_SHA256`） |
| 源站证书 | `CN=*.xins.live`，链：`WR1` ← `GTS Root R1` ← `GlobalSign Root CA`，有效期至 **2026-12-26** |
| 当前 mTLS 状态 | `ssl_verify_client optional`（过渡态，见 §2.2）；ESA 侧**还没开**「回源双向校验」 |
| 真实 IP 还原 | **未装**（见 §6） |

> 迁移轨迹：21:21 前后前置还是腾讯云 EdgeOne（`eo-cache-status`），21:30 前后
> 已经变成 ESA（`server: ESA`）。下面 §2.2 里「EdgeOne 回源不带证书」那套
> 理由仍然成立 —— **任何不带客户端证书的 CDN 回源在 `on` 模式下都会被拒**，
> 与是哪一家无关。


---

## 2. 源站侧（✅ 已完成）

### 2.1 已装的东西

**ESA 根证书**（信任锚）：

```
/etc/nginx/ssl/esa/esa-client-ca.pem
```

出自 ESA 官方文档「回源双向校验 → 站点粒度配置」一节：

```
CN=Alibaba Cloud, OU=Alibaba Cloud SSL Certificate Authority
RSA 2048 / sha256WithRSAEncryption / CA:TRUE
有效期 2025-01-13 ~ 2035-01-11（自签根）
SHA256 Fingerprint: FA:E7:61:FF:13:E6:0D:45:B9:08:A3:BD:AC:8D:C3:91:C0:A7:C3:BA:1B:8E:44:3C:12:22:92:0A:67:F2:80:EA
```

**nginx 指令**（在 443 那个 server 块里）：

```nginx
ssl_client_certificate /etc/nginx/ssl/esa/esa-client-ca.pem;
ssl_verify_client     optional;
ssl_verify_depth      2;
```

**观测日志**（在 combined 基础上**追加**三列，不是替换 —— 既有按列解析日志的地方不受影响）：

```
mtls=$ssl_client_verify dn="$ssl_client_s_dn" cipher=$ssl_cipher
```

访问日志：`/var/log/nginx/acct.xins.live.log`

### 2.2 为什么现在是 `optional` 而不是 `on`

`on` = 握手阶段**必须**收到客户端证书，拿不到直接失败。

而 **ESA 侧「回源双向校验」还没开** —— 现在 ESA 回源**不带**客户端证书
（源站日志实测 `mtls=NONE`）。改成 `on` 的那一刻：

- ESA 的回源会**全挂**（没有客户端证书 → 握手被拒 → 站点 5xx）；
- `test.xins.live` 是**直连**源站的，浏览器不带证书 → 也一起断。

`optional` 的语义是：没带证书的照常放行，但**带了**证书的必须验签通过
（验不过请求被挡成 `400 The SSL certificate error`）。于是它同时给出两件事：

1. 现在就能回答「ESA 那张证书在源站这边验不验得过」—— 看日志里的 `mtls=`；
2. 不会打断 ESA 现有的回源和直连测试。

**正确的顺序**：先在 ESA 控制台开「回源双向校验」（§3），确认源站日志出现
`mtls=SUCCESS`，**再**把源站切到 `on`（§5）。


### 2.3 实测行为（2026-10-24）

| 场景 | 结果 | 日志 |
| --- | --- | --- |
| EdgeOne 回源（不带证书） | `200` | `mtls=NONE` |
| 直连源站 `test.xins.live`（不带证书） | `200` | `mtls=NONE` |
| 直连 + **伪造**的自签客户端证书 | **`400 The SSL certificate error`** | `mtls=FAILED:self-signed certificate dn="CN=Not-ESA-Fake-Client"` |
| `on` 模式下直连（不带证书） | **`400 No required SSL certificate was sent`** | `mtls=NONE` |
| ESA 带的真证书（待控制台开启后） | `200` | `mtls=SUCCESS dn="...Alibaba Cloud..."` |

**两种 400 是不同的东西，别混**：

| 现象 | 含义 | 排查方向 |
| --- | --- | --- |
| `400 The SSL certificate error` | 客户端**带了**证书但验签不过 | 证书签发链不对 / 不是 ESA 那张 |
| `400 No required SSL certificate was sent` | 客户端**根本没带**证书 | `on` 模式下的正常拒绝 |

伪造证书那次握手本身是**完成**的（TLS 1.3 的语义：客户端证书在服务端
Finished 之后才送到），被拒在 HTTP 层。nginx 侧表现是 400，不是连接重置 ——
**排查时别去盯连接错误，要盯这个 400 和日志里的 `mtls=FAILED`**。

---

## 3. ESA 控制台侧（⬜ 需要你操作）

控制台路径：**站点管理 → 点目标站点 → 左侧 SSL/TLS → 源站证书**

1. **回源协议和端口** → 配置
   - 回源协议：**HTTPS**（不要用「跟随客户端协议」—— 客户端走 HTTP 时会退化成明文回源，mTLS 就没有了）
   - HTTPS 端口：**443**
2. **源站证书校验** → 打开
   - ⚠️ 开了之后 ESA 会拿预置根证书验你源站那张证书。**你这张能过**：
     `GTS Root R1` 在 ESA 的预置根证书列表里（第 113 项，136 项之一）。
   - 但**证书 2026-12-26 到期**，到期后回源会直接失败。续签走仓库已有的
     `cwq-cert-sync` 机制（外部应用上传 → 自动校验安装 → 自动 reload），
     别忘了同步续签。
3. **回源双向校验** → 打开
   - 默认使用 ESA 自签证书回源，源站已信任对应根证书（§2.1）。

### 3.1 加密套件（已核对，能对上）

ESA 回源支持的套件：

| 协议 | 套件 |
| --- | --- |
| TLS 1.2 | `TLS_ECDHE_{ECDSA,RSA}_WITH_AES_{128,256}_GCM_SHA{256,384}` |
| TLS 1.3 | `AEAD-AES128-GCM-SHA256`、`AEAD-AES256-GCM-SHA384`、`AEAD-CHACHA20-POLY1305-SHA256` |

源站现状：`ssl_protocols TLSv1.2 TLSv1.3;`、`ssl_ciphers HIGH:!aNULL:!MD5;`。
TLS 1.2 侧 `HIGH` 覆盖了全部四个 ECDHE-GCM 套件；TLS 1.3 侧 OpenSSL 3.x 的
默认套件正好就是 ESA 列的那三个。**无需改动**，实测握手已用
`TLS_AES_256_GCM_SHA384` / `TLS_CHACHA20_POLY1305_SHA256`。

---

## 4. 怎么验证「ESA 那张证书验过了」

开启控制台开关后，在源站上看日志：

```bash
ssh -i ~/.ssh/<你的私钥> root@43.138.178.174 \
  'grep -oE "mtls=[A-Za-z:0-9-]+" /var/log/nginx/acct.xins.live.log | sort | uniq -c'
```

期望看到：

```
     12 mtls=SUCCESS
```

`SUCCESS` = ESA 带的证书验签通过，这套 mTLS 真的跑通了。

- `mtls=NONE` 占绝大多数 → 那是 EdgeOne 和直连流量（不带证书），正常；
- `mtls=FAILED:xxx` → ESA 带的证书源站验不过，**看 `dn=` 里的 Subject 对不对**；
- 出现 `mtls=SUCCESS` 但网站 502 → 不是 mTLS 的问题，去看 ESA 控制台的回源报错
  和源站的 `acct.xins.live.error.log`。

---

## 5. 转「强制校验」（ESA 接管全部流量之后）

只有当 **ESA 已经接管全部流量**、且日志确认 `mtls=SUCCESS` 之后，才转 `on`。
`on` 的含义是「没有 ESA 证书的连接一律拒绝」。

```bash
ssh -i ~/.ssh/<你的私钥> root@43.138.178.174 \
  '/usr/local/sbin/cwq-esa-mtls.sh strict'
```

脚本会：备份 → 改 `optional` 为 `on` → `nginx -t` → **过了才 reload** →
失败自动装回备份。

**转 `on` 之后的两个副作用**（都要提前想清楚）：

1. **直连源站会断**：`test.xins.live` 直指 `43.138.178.174`，浏览器不带客户端证书
   → 握手失败。要继续用这个测试域名，就把 `test.xins.live` 也 CNAME 到 ESA。
2. **EdgeOne 回源会断**：EdgeOne 不带客户端证书。要么把 EdgeOne 撤掉，
   要么让 EdgeOne 走另一个端口。

回滚：

```bash
ssh -i ~/.ssh/<你的私钥> root@43.138.178.174 \
  '/usr/local/sbin/cwq-esa-mtls.sh relax'
```

查看当前状态：

```bash
ssh -i ~/.ssh/<你的私钥> root@43.138.178.174 \
  '/usr/local/sbin/cwq-esa-mtls.sh status'
```

### 5.1 切换后立刻验证：注意 reload 竞态

`nginx -s reload` 是**信号**，命令返回时新 worker 未必已经接管完 ——
**老 worker 还会用旧配置继续服务几秒**。切换完马上 curl，很可能打到的还是
老 worker，看到的是**切换前的结果**，于是得出「没生效 / 生效了」的相反结论。

实测踩过：切到 `on` 后立刻直连源站拿到 `200`（老 worker 还挂着 `optional`），
过了几秒再试就是 `400`。**判定标准是「新 worker 的启动时间」**：

```bash
ssh -i ~/.ssh/<你的私钥> root@43.138.178.174 \
  'ps -o pid,lstart,cmd -C nginx | grep worker'
```

worker 的 `lstart` 晚于切换时刻，才算切完。验证前 `sleep 3` 更省事。

### 5.2 会不会被会话复用绕过？（已验证：不会）

`ssl_verify_client on` 看着像是能被 TLS 会话复用绕过 —— 因为会话里缓存了
握手结果，复用时不再重新握手。实测两条路径都测了：

| 协议 | 复用方式 | 切到 `on` 后复用旧会话 |
| --- | --- | --- |
| TLS 1.3 | session ticket | `400 No required SSL certificate was sent` ✅ 没绕过 |
| TLS 1.2 | session id | `400 No required SSL certificate was sent` ✅ 没绕过 |

即：nginx 在会话复用时**仍然校验客户端证书**，不存在「先在 optional 下建个
会话、再拿它绕过 on」的路径。所以 `on` 模式下「只有带 ESA 证书的连接能进」
这个结论是成立的，**不需要额外关掉 `ssl_session_cache` / `ssl_session_tickets`**。

---

## 6. 顺带发现并修掉的一个坑：真实 IP 没还原

> 这一节是 2026-10-24 配 mTLS 时**顺带发现的独立问题**，跟 mTLS 不是一回事，
> 但同属「ESA 在前、源站在后」这套形态，一起看。

### 6.1 现象与根因

本系统的**登录节流按 IP 分桶**。它取客户端 IP 的口径（`app/api/auth.py`
的 `_throttle_ip`）是：

1. 对端不在 `TRUSTED_PROXIES` → 只认 TCP 对端；
2. 对端是白名单里的代理 → **先读 `X-Real-IP`**；
3. 没有 `X-Real-IP` 才退回 `X-Forwarded-For` 的**最右**一个。

生产上 `TRUSTED_PROXIES=127.0.0.1`（nginx），而 nginx 的
`proxy_set_header X-Real-IP $remote_addr` —— `$remote_addr` 是**源站看到的
TCP 对端**。

CDN 在前面时，那就是 **CDN 的边缘节点 IP**。于是：

- 登录节流按「边缘节点」分桶 → 一个节点后面成千上万个用户共用一个桶，
  一个人连着试错会把同节点其他人一起挡；
- 审计日志里的 IP 全是节点地址 → 出事时查不到是谁。

**这个故障不报错**：节流照常工作，只是分桶的维度错了。

> 实测（2026-10-24）：源站 `/etc/nginx/cwq-include/` 是**空的** —— 仓库里
> 早就备好的 `deploy/nginx/cwq-cloudflare-realip.conf` 从来没装过。所以不管
> 前置是 EdgeOne 还是 ESA，真实 IP 一直没还原。

### 6.2 ESA 对 X-Forwarded-For 的处理（实测）

客户端伪造 `X-Forwarded-For: 6.6.6.6` 请求 `acct.xins.live`，源站看到的是：

```
"6.6.6.6, 39.144.252.250"
```

即 ESA 是**追加**语义：把自己看到的真实客户端 IP 追加在**最右边**。所以：

- nginx 的 realip 模块默认（`real_ip_recursive off`）取 XFF 的**最后一个**
  元素 → 正好是真实客户端 IP，伪造的前缀自动被忽略；
- 实测 ESA 是**单层回源**（29 条回源请求里 29 条 XFF 只有 1 个元素），
  所以最右元素就是客户端本人。

### 6.3 修法（需要你做一步）

ESA 的回源网段**不是静态公开列表**，必须从控制台取：

1. ESA 控制台 → 站点 `acct.xins.live` → **安全防护 → 源站防护** → 配置 →
   打开状态 → 勾选「我已确认风险」→ 开启 → **复制 IP 地址**（收敛后的回源
   节点网段，含 IPv4 / IPv6）；
   - 也可以走 OpenAPI `esa:GetOriginProtection`（需要该站点 SiteId），
     取 `CurrentIPWhitelist.IPv4` / `.IPv6`。
2. 把列表存成文件（一行一个 IP 或网段），然后：

```bash
ssh -i ~/.ssh/<你的私钥> root@43.138.178.174 \
  'cat > /tmp/esa-ips.txt' < /path/to/esa-ips.txt
ssh -i ~/.ssh/<你的私钥> root@43.138.178.174 \
  '/usr/local/sbin/cwq-esa-realip-update.sh /tmp/esa-ips.txt'
```

脚本会：解析列表 → 生成 `/etc/nginx/cwq-include/cwq-esa-realip.conf` →
备份旧的 → `nginx -t` → **过了才 reload** → 失败自动装回备份。

三份 nginx 配置里那行

```nginx
include /etc/nginx/cwq-include/cwq-esa-realip*.conf;
```

已经加好了（源站生效配置 + 仓库两份模板）。它是**通配** include ——
文件不存在时不报错，也就是「没装 CDN」的干净关闭状态，所以现在装不装都不影响线上。

验证（装完之后，源站日志里的 remote_addr 应该变成真实客户端 IP）：

```bash
ssh -i ~/.ssh/<你的私钥> root@43.138.178.174 \
  'tail -20 /var/log/nginx/acct.xins.live.log'
```

### 6.4 为什么 `set_real_ip_from` 的网段不能写 0.0.0.0/0

写成 `0.0.0.0/0` 的话，**任何能连到源站的人**都能自己塞一个
`X-Forwarded-For: 1.2.3.4`，把 IP 伪造成任意地址 —— 于是 IP 维度的节流与
审计全部失效（攻击者每个请求换一个头就换一个桶）。所以必须逐条列出 ESA 的
回源网段。

### 6.5 ESA 的回源网段会变

阿里云会通过站内信 / 邮件通知「ESA 的节点 IP 发生变化」。**变了必须重跑
§6.3 的脚本**，否则新网段的回源不还原真实 IP（退化成分桶不准，不报错，
也不会更安全）。

---

## 7. mTLS 不是防火墙的替代品

`ssl_verify_client on` 只保证「**带着正确证书**的连接被接受」。它挡不住：

- 直接打源站 80 端口（现在 80 端口是 301 跳转到 443，链路上仍然露着）；
- 打源站上的其它服务（5432 / 8000 —— 8000 目前只监听 `127.0.0.1`，✅ 安全；
  5432 同样只监听 `127.0.0.1`，✅ 安全）。

真正把源站藏起来的还是**安全组 / 防火墙只放行 CDN 网段**：

- EdgeOne 回源网段：腾讯云官方公布；
- ESA 回源网段：阿里云 ESA 官方公布的 IP 列表。

**建议**：转 `on` 之后，把 443 的来源 IP 限制到 ESA 网段（过渡期把 EdgeOne 网段也加上），
让「必须带证书」和「只能从 CDN 来」两道闸叠在一起。

---

## 8. 已知限制

| 限制 | 说明 |
| --- | --- |
| 直连测试域名在强制模式下失效 | 见 §5 |
| 源站证书 2026-12-26 到期 | 开了「源站证书校验」之后，证书过期 = 回源全挂。续签靠 `cwq-cert-sync` |
| `ssl_verify_depth 2` | ESA 换证书链（多一层中间证书）时够用；再多就要改 |
| `optional` 期间不是强制 | 在 `on` 之前，绕过 CDN 直打源站仍然能拿到内容 |
| 真实 IP 还没还原 | 见 §6 —— 需要你从 ESA 控制台取一次回源网段 |
| 源站 443 对公网开放 | 开了 `on` 之后握手就挡住了；但 80 端口仍露着，且 §6.3 没做之前直打源站能拿到内容 |
| 仓库模板已同步 | `deploy/nginx/acct.xins.live.conf` 和 `deploy/nginx/cwq.conf` 里都有这三段。**下次把模板拷到服务器时不会丢** |

---

## 9. 相关文件

| 位置 | 作用 |
| --- | --- |
| `/etc/nginx/ssl/esa/esa-client-ca.pem` | ESA 根证书（信任锚） |
| `/etc/nginx/conf.d/acct.xins.live.conf` | 生效配置（`ssl_client_*` 三行 + `log_format cwq_mtls` + `cwq-esa-realip` include） |
| `/usr/local/sbin/cwq-esa-mtls.sh` | `status` / `strict` / `relax` 切换脚本 |
| `/usr/local/sbin/cwq-esa-realip-update.sh` | 真实 IP 还原：吃 ESA 的回源网段列表，生成并安全安装配置 |
| `deploy/esa-mtls.sh` | 上面那个切换脚本的仓库副本 |
| `deploy/esa-realip-update.sh` | 上面那个真实 IP 脚本的仓库副本 |
| `deploy/nginx/acct.xins.live.conf` | 仓库模板（宝塔形态 / 新机同源） |
| `deploy/nginx/cwq.conf` | 仓库模板（独立 nginx 形态） |
| `deploy/verify_cdn.sh` | 发布后自检：认 Cloudflare / EdgeOne / **ESA** 三家的缓存状态头 |
| `deploy/cloudflare.md` | 通用 CDN 部署形态（缓存规则、真实 IP 还原） |

