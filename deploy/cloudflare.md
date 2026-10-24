# 部署到 Cloudflare（CDN 在前、源站在后）

> 目标形态：**小带宽服务器只跑动态部分，静态资源由 CDN 边缘缓存。**
>
> ```
> 浏览器 ──HTTPS──▶ Cloudflare 边缘
>                      ├─ /assets/*   命中边缘缓存（不回源站）
>                      └─ 其它         回源 ──▶ nginx ──▶ FastAPI / 静态目录
> ```
>
> 与「静态资源放到另一个 CDN 域名」那种形态的**根本区别**：这里的 URL
> **不变**（还是 `https://acct.example.com/assets/xxx.js`），CDN 是**反向
> 代理**，按路径决定缓不缓存。所以**不需要改任何前端代码、不需要开 CORS**
> —— 页面与 API 仍然同源。

---

## 0. 前提：这套东西为什么能开箱即用

仓库里已经为这个形态准备好了三件事：

| 准备 | 在哪 | 作用 |
| --- | --- | --- |
| 静态资源带长缓存 | `deploy/nginx/*.conf` 的 `location /assets/` | `Cache-Control: public, max-age=31536000, immutable` —— CDN 命中后**一年**不过期 |
| 入口页与运行时配置**不缓存** | 同上，`location /` 与 `location = /runtime-config.js` | `Cache-Control: no-cache` —— **改了立刻生效** |
| 静态资源带跨源头 | 同上 | `Access-Control-Allow-Origin: *`（同源时无害，但换 CDN 域名时必需） |

另外：资源文件名**带内容哈希**（`index-CUpwOnfx.js`），内容变了文件名就变了。
所以 CDN 缓存一年**不会出错**，也**不需要在发布后刷新缓存** —— 新版 HTML
指向的是新文件名（见「发布流程」）。

---

## 1. DNS 与证书

1. 域名接入 Cloudflare（NS 改到 Cloudflare）。
2. **SSL/TLS 模式设为 `Full (strict)`** —— 不要用 `Flexible`：
   - `Flexible` 下 Cloudflare 到源站走明文 HTTP。**源站上的 nginx 只监听
     443**（`cwq.conf` 里 80 端口那个 server 只做 301 跳转），于是
     Cloudflare 回源会被拒 —— 表现是「Cloudflare 报 525 / 522」。
   - `Full`（非 strict）会验证证书但接受自签；能用，但不如 strict 严格。
3. 证书用源站那一份（宝塔形态是
   `/www/server/panel/vhost/cert/acct.xins.live/fullchain.pem`）。
   Cloudflare 也会自己签一张边缘证书，浏览器看到的是那张 —— 源站证书
   只用于 Cloudflare → 源站这一段。

---

## 2. 源站防护（**不要跳过**）

**安全组 / 防火墙只放行 Cloudflare 的网段**（IPv4 与 IPv6 都要）：

```
https://www.cloudflare.com/ips-v4
https://www.cloudflare.com/ips-v6
```

为什么必须做：

- 不做的话，任何人都能**绕过 CDN 直接打源站** —— 那 CDN 缓存就白做了，
  源站照样吃满带宽，而且攻击面完整暴露。
- 做了之后，源站只认 Cloudflare 来的连接，**`CF-Connecting-IP` 那个头
  才是可信的**（见下一节）。

> 顺带：nginx 里的 Host 白名单（`cwq-host-map.conf`）会让**用 IP 直连**
> 的请求拿到 444 —— 但那是应用层的兜底，不是网络层的。别只靠它。

> 还有一条别顺手改掉的：应用的 CSP 是 `script-src 'self'`，**没有**
> `'unsafe-inline'`。所以运行时配置是**外部文件**（`/runtime-config.js`）
> 而不是内联在 HTML 里的 `<script>` —— 内联会被 CSP 直接挡掉。
> 「省一次几百字节的请求」不值得把 XSS 防线降一级。

---

## 3. nginx 侧：还原真实客户端 IP

**这一步不能省。** 本系统有一层**按 IP 分桶**的登录节流
（`LOGIN_IP_*`）。CDN 在前面时，nginx 看到的对端是 **CDN 的边缘节点 IP**
—— 那台机器后面可能连着成千上万个用户。于是：

- 登录节流按「边缘节点」分桶 → 一个人在连着试错，**同一节点上的其他人
  一起被挡**（误伤，而且不报错）；
- 审计日志里的 IP 全是 Cloudflare 的地址 → 出事查不到人。

装法：

```bash
cp deploy/nginx/cwq-cloudflare-realip.conf /etc/nginx/cwq-include/
nginx -t && nginx -s reload
```

它做的是 `real_ip_header CF-Connecting-IP` + `set_real_ip_from <Cloudflare
网段>`：**只有对端确实属于 Cloudflare 网段时**才把 `$remote_addr` 换成真实
用户 IP。网段白名单是安全边界 —— 写成 `0.0.0.0/0` 的话，任何能连到源站
的人都能自己塞一个 `CF-Connecting-IP` 头伪造 IP，节流与审计一起失效。

**后端的 `TRUSTED_PROXIES` 仍然填 `127.0.0.1`**（nginx 仍是后端的直接
对端）。那里填的是「谁在跟后端说话」，不是「谁在跟 nginx 说话」。

> Cloudflare 会增删网段。**升级 Cloudflare 之后核对一次**上面的两个
> 链接，改了要 `nginx -t && reload`。

---

## 4. Cloudflare 侧：缓存规则

### 4.1 默认行为够用吗

Cloudflare 的默认缓存行为（官方文档）：

- **按文件扩展名**决定缓存哪些，**不看 MIME**；
- 默认缓存：`JS`、`CSS`、`SVG`、`WOFF/WOFF2/TTF`、`PDF`、`XLS/XLSX`、图片等；
- **默认不缓存 HTML 与 JSON**；
- 源站给了 `Cache-Control: private / no-store / no-cache / max-age=0` 的
  **不缓存**；给了 `public` 且 `max-age>0` 的**缓存**；
- 免费 / 专业 / 商业版默认「尊重源站的 Cache-Control」。

按这套默认行为，本项目**开箱即用的部分**：

| 路径 | 源站头 | Cloudflare 默认 |
| --- | --- | --- |
| `/assets/*.js` `/assets/*.css` | `public, max-age=31536000, immutable` | 缓存 ✅ |
| `/`（入口 HTML） | `no-cache` | 不缓存 ✅ |
| `/runtime-config.js` | `no-cache` | **默认会缓存**（它是 `.js`）⚠️ |
| `/api/**`（JSON） | 无 `public` | 默认不缓存 ✅ |

### 4.2 必须自己加的 Cache Rule

**两条规则，缺一不可：**

| 规则 | 匹配 | 动作 |
| --- | --- | --- |
| ① 运行时配置 | `http.request.uri.path eq "/runtime-config.js"` | **Bypass cache**（不缓存） |
| ② 业务接口 | `http.request.uri.path starts_with "/api/"` | **Bypass cache** |

**①为什么必须加**：Cloudflare 按**扩展名**决定缓存，而 `runtime-config.js`
就是 `.js`。它决定「API 在哪」「静态资源在哪」，**每套部署都不同** ——
缓存住了就是「改了配置不生效」。免费/专业/商业版默认尊重源站的
`no-cache`（所以通常没事），但**企业版可以把「尊重源站」关掉**（那它就按
默认 Edge TTL 缓存 200 响应 120 分钟）。显式加一条规则，**所有套餐行为
一致**，也不依赖那个默认开关。

**②为什么必须加**：API 响应默认不缓存（JSON 不在扩展名列表里），但
「默认」不是「保证」：

- 导出接口返回的是 `.xlsx`（**在**默认缓存扩展名列表里）；
- 请求带 `Authorization` 头时 Cloudflare 默认不缓存它，但那是**行为**
  不是**规则**，配置一改（Cache Rules、Workers、Page Rules）就可能变；
- 缓存住一个 API 响应 = **把 A 账套的账目发给 B**。这条规则是一道
  便宜的保险。

> 规则 ② 也可以改成「只缓存 `/assets/`」（Cache everything + 路径匹配），
> 效果一样但更直观。

### 4.3 必须关掉的开关

| 开关 | 位置 | 为什么关 |
| --- | --- | --- |
| **Rocket Loader** | Speed → Optimization | 它会**改写 `<script>` 标签**（延后加载、改 src）。本项目是 **ES module**，被改写后模块图与执行顺序都可能出问题，且症状是「偶发白屏」，极难查。 |
| **Auto Minify** | Speed → Optimization | 它会**重新压缩** HTML/CSS/JS。压缩后的产物与源站的 CSP / SRI 对不上时会被浏览器直接拒绝执行。 |
| **Always Online** | Caching → 边缘 | 源站挂了时它会拿**缓存里的旧页面**顶上。对一个记账系统来说，「页面能打开但数据是旧的」比「明确报错」危险得多 —— 人会以为账没记上。 |
| **Browser Integrity Check**（可选） | Security → Settings | 会拦掉一部分「像爬虫」的请求。本系统全是正常浏览器流量，默认开着通常没问题；但若出现「某个浏览器登不进去」，先关掉它排除。 |

---

## 5. 已知限制（**免费/专业/商业版改不了**）

Cloudflare 的**回源读超时是 125 秒**（写超时 30 秒）。超出就是 **524**
（Cloudflare 的错误页，不是源站的）。

| 场景 | 影响 | 怎么办 |
| --- | --- | --- |
| 报表导出 / 打印生成 > 125 秒 | 524 | 先优化查询；确实需要慢的接口，走**DNS-only 子域**（灰云，不经过 Cloudflare） |
| **MCP 的 SSE 长连接** | 边缘在**静默 125 秒**后掐断连接 | MCP 端点（`/api/mcp`）建议走 **DNS-only 子域**；或用企业版把 Proxy Read Timeout 调高（需要内容可缓存才生效） |

nginx 那边 `proxy_read_timeout` 是 180 秒（`/api/mcp` 是 360 秒）—— 那比
Cloudflare 的 125 秒长，于是**卡点其实在 CDN 这一层**，改 nginx 的超时
没用。这一点要跟运维说清，否则会去查错的方向。

**上传大小**：免费 / 专业版上限 100 MB。本系统的发票批量上传上限是
50 MB（`INVOICE_MAX_TOTAL_BYTES`），nginx 侧闸门是 52 MB —— 都在 100 MB
以内，**不用改**。

---

## 6. 发布流程

**发布后不需要刷新 CDN 缓存。** 原因：

- 静态资源文件名带内容哈希 → 内容变了文件名就变了 → 新 HTML 指向的是
  新文件，不存在「拿到旧文件」的可能；
- 入口 HTML 与运行时配置都是 `no-cache` → 每次都回源拿最新的；
- 旧文件留在 CDN 上没人引用 —— 最多占点边缘空间，且 Cloudflare 有
  LRU 清理。

所以 `deploy/deploy.sh` **不用改**，照常发布即可。

（若将来把入口 HTML 也交给 CDN 托管，那就**必须**在发布后刷新
`/` 这条路径的缓存 —— 见 README「动静分离与 CDN 部署」里「页面也托管在
CDN」那一节。）

---

## 7. 验证

```bash
# 1) 静态资源：应该 HIT（第二次请求）
curl -sI https://acct.example.com/assets/index-XXXX.js | grep -iE 'cf-cache-status|cache-control|age'
#   cf-cache-status: HIT
#   cache-control: public, max-age=31536000, immutable
#   age: 120

# 2) 入口页：应该 MISS/DYNAMIC + no-cache
curl -sI https://acct.example.com/ | grep -iE 'cf-cache-status|cache-control'
#   cf-cache-status: DYNAMIC
#   cache-control: no-cache

# 3) 运行时配置：应该 DYNAMIC（规则①生效了）
curl -sI https://acct.example.com/runtime-config.js | grep -iE 'cf-cache-status|cache-control'
#   cf-cache-status: DYNAMIC
#   cache-control: no-cache, must-revalidate

# 4) 接口：应该 DYNAMIC，且**不能**有 cf-cache-status: HIT
curl -sI https://acct.example.com/api/health | grep -iE 'cf-cache-status'
#   cf-cache-status: DYNAMIC
```

仓库里有个现成的脚本一次跑完这四条：

```bash
bash deploy/verify_cdn.sh https://acct.example.com
```

---

## 8. 排障对照表

| 症状 | 大概率原因 | 去哪看 |
| --- | --- | --- |
| 页面能开，**JS 不生效**（控制台有 CORS 错误） | CDN 剥掉了 `Access-Control-Allow-Origin` | `curl -sI <资源> \| grep -i access-control`；CDN 控制台看响应头改写规则 |
| **发布后界面没变**（还是上一版） | 入口 HTML 被 CDN 缓存了 | `curl -sI / \| grep -i cf-cache-status`；若是 HIT，缓存规则 ①/入口 HTML 规则没配 |
| **改了 runtime-config 不生效** | `/runtime-config.js` 被缓存 | 同上，规则 ① 没配 |
| 某台机器登不进去、别人正常 | IP 维度的节流误伤（CDN 边缘 IP 分桶） | 装 `cwq-cloudflare-realip.conf`；查后端「登录防护」页面 |
| 偶发 **524** | 接口耗时超 125 秒 | 看 Cloudflare 的 524 报表与源站慢日志 |
| MCP 连一会儿就断 | SSE 静默 125 秒被边缘掐断 | MCP 走 DNS-only 子域 |
| 源站 IP 被人直接访问 | 安全组没只放行 Cloudflare 网段 | 收紧安全组 |
| 源站日志里的 IP 全是 Cloudflare 地址 | 没装 realip 配置 | 见第 3 节 |
