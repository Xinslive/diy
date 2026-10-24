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

### 4.1 先分清三个层级（选错就白配）

控制台里有三处都叫「缓存」的东西，**各管一件事**。混起来配，就会出现
「我明明选了遵循源站，怎么还是不对」这种查不出原因的现象：

| 层级 | 在哪 | 管什么 |
| --- | --- | --- |
| **缓存资格 Cache eligibility** | Caching → Cache Rules → Cache eligibility | 这条 URL **能不能**进边缘缓存（Bypass cache / Eligible for cache） |
| **Edge TTL** | 同一条 Cache Rule 里 | 进缓存之后**待多久**、要不要回源重验 |
| **Browser TTL** | 同一条 Cache Rule 里 | **浏览器**缓存多久 —— 与边缘缓存是两回事，改它不影响 CDN |
| **Origin Cache Control（OCC）** | Cache Rule 里（**仅企业版**能关） | 是否严格按 RFC 7234 遵守源站的 `Cache-Control`。免费 / 专业 / 商业版**默认开启且不能关** |

> 「缓存级别」（Caching → Configuration → 缓存级别）现在只管**查询串**行为
> （无查询串 / 忽略查询串 / 标准），**不再管缓存资格** —— 缓存资格已经完全
> 由 Cache Rules 表达了。

**Edge TTL 的三个选项**（这是最容易选错的下拉框）：

| 选项 | 官方语义 | 本项目该怎么用 |
| --- | --- | --- |
| ① `bypass_by_default` | 有 `cache-control` 就按它来，**没有就不缓存** | **默认选这个** |
| ② `respect_origin` | 有 `cache-control` 就按它来，**没有就按 Cloudflare 默认行为**（200 响应缓存 **120 分钟**） | 只在明确知道「没给头的响应也该缓存」时才用 |
| ③ `override_origin` | 完全忽略源站头，用下拉框里指定的 TTL | **不要拿它当全局设置**（会把入口 HTML 一起缓存起来） |

「遵循源站的 Cache-Control」= 选项 ②。**它是对的起点，但单独用不够**，
原因见 4.3。

### 4.2 默认行为够用吗

Cloudflare 的默认缓存行为（官方文档
[Default cache behavior](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/)）：

- **按文件扩展名**决定缓存哪些，**不看 MIME**、**也不看路径**；
- 默认缓存：`JS`、`CSS`、`SVG`、`WOFF/WOFF2/TTF`、`PDF`、`XLS/XLSX`、`EXE`、图片等；
- **默认不缓存 HTML 与 JSON**；
- 源站给了 `Cache-Control: private / no-store / no-cache / max-age=0` 的，
  **不会被用来满足一次「不回源就返回」的请求**（`no-store` 则是彻底不存）；
  给了 `public` 且 `max-age>0` 的**缓存**；
- **源站一个 `Cache-Control` 都没给**时：按状态码走默认 Edge TTL ——
  **200 / 206 / 301 缓存 120 分钟**、302 / 303 20 分钟、404 / 410 3 分钟；
- 免费 / 专业 / 商业版默认开启 OCC，也就是「尊重源站的 Cache-Control」。

按这套默认行为，本项目各路径的**实际**结果：

| 路径 | 源站头 | Cloudflare 默认 | 判定 |
| --- | --- | --- | --- |
| `/assets/*.js` `/assets/*.css` | `public, max-age=31536000, immutable` | 缓存一年 | ✅ 正是想要的 |
| `/`（入口 HTML） | `no-cache` | 不发陈旧内容 | ✅ |
| `/runtime-config.js` | `no-cache, must-revalidate` | 不发陈旧内容，但每次都回源重验 | ✅ 但多一跳 |
| `/api/**`（JSON） | 多数**没有** `Cache-Control`；少数 `private, no-store` / `no-store` | 默认不缓存（JSON 不在扩展名列表里） | ✅ |
| `/api/plugins/assets/**` | **`public, max-age=31536000, immutable`** | **缓存一年** | ⚠️ 见 4.3 |
| `/api/books/**.xlsx`（导出） | **一个 `Cache-Control` 都没有** | **缓存 120 分钟** | ⚠️ 见 4.3 |
| `/downloads/*.exe` | **一个 `Cache-Control` 都没有** | **缓存 120 分钟** | ⚠️ 见 4.3 |

> **官方两页的口径略有出入**：[Default cache behavior](https://developers.cloudflare.com/cache/concepts/default-cache-behavior/) 写「`no-cache`
> → 不缓存」，[Origin Cache Control](https://developers.cloudflare.com/cache/concepts/cache-control/)
> 的指令表写「OCC 开启时 `no-cache` → **缓存但每次回源重验**、不发陈旧」。
> **两种口径下都不会发陈旧内容**，差别只在于边缘要不要存一份。本节的规则
> 在两种解读下都成立。

### 4.3 必须自己加的 Cache Rule

**光靠「遵循源站的 Cache-Control」（Edge TTL 选项 ②）不够**，两个原因：

1. **它管的是「缓存多久」，管不了「哪些 URL 有资格被缓存」。** 资格是由
   扩展名启发式 + 源站响应头**共同**决定的 —— 而本项目有三处响应头的
   口径不对（上表三个 ⚠️），换哪个 Edge TTL 选项都救不了。
2. **它不管「源站没给 `Cache-Control` 时怎么办」** —— 选项 ② 的答案是
   「按 Cloudflare 默认行为缓存 120 分钟」，而这正是导出 `.xlsx` 与
   `/downloads/*.exe` 的下场。选选项 ① 就没有这个问题。

**最小集：三条规则，缺一不可**（Caching → Cache Rules → Create rule）：

| # | 匹配表达式 | 缓存资格 | Edge TTL |
| --- | --- | --- | --- |
| ① 业务接口 | `starts_with(http.request.uri.path, "/api/")` | **Bypass cache** | — |
| ② 运行时配置 | `http.request.uri.path eq "/runtime-config.js"` | **Bypass cache** | — |
| ③ 静态资源 | `starts_with(http.request.uri.path, "/assets/")` | **Eligible for cache** | ① `bypass_by_default` |

**表达式怎么写**：`starts with` / `ends with` 在「编辑表达式」里是**函数**，
要写成 `starts_with(http.request.uri.path, "/api/")`；而 `eq` / `matches` /
`wildcard` 是**操作符**，写在中间（`http.request.uri.path eq "..."`）。
用「表达式构建器」点选的话，选「URI Path」+「starts with」+ 填 `/api/`，
生成的就是上面那个函数调用。

**同一批规则的另两种写法**（按你习惯挑）：

| 规则 | `wildcard` 通配符（**全套餐**） | `matches` 正则（**仅 Business / Enterprise**） |
| --- | --- | --- |
| ① | `http.request.uri.path wildcard "/api/*"` | `http.request.uri.path matches r"^/api/"` |
| ② | `http.request.uri.path wildcard "/runtime-config.js"` | `http.request.uri.path matches r"^/runtime-config\.js$"` |
| ③ | `http.request.uri.path wildcard "/assets/*"` | `http.request.uri.path matches r"^/assets/"` |
| ④ | `not http.request.uri.path wildcard "/assets/*"` | `not http.request.uri.path matches r"^/assets/"` |

四个必须知道的差别：

- **正则要 Business / Enterprise 套餐。** 官方文档写明「Access to the
  `matches` operator requires a Cloudflare Business or Enterprise plan」——
  免费版 / Pro 版**用不了** `matches`，只能 `wildcard` 或 `starts_with()`。
- **`matches` 是部分匹配，`wildcard` 是全匹配** —— 所以正则**必须自带 `^`**。
  这条在本项目**不是形式要求，是要命的**：插件资产的路径是
  `/api/plugins/assets/{id}/{digest}/frontend/t/{token}/…`，**里面就含
  `/assets/`**！不写 `^` 的话它会**同时**命中 ③（`/assets/` → Eligible for
  cache），而多条规则都命中时**靠后的一条赢** —— ③ 排在 ① 后面，于是插件
  资产被缓存一年，① 的 bypass 白配了。写上 `^` 之后它只命中 ①，正确。
- **正则用 `r"..."` 原始字符串写。** 原始字符串里没有转义序列，内容原样交给
  正则引擎，所以 `.` 直接写成 `\.` 就行（官方文档也推荐这么做：「the quoted
  string syntax has complex escaping rules and can lead to unexpected
  behaviors」）。若坚持用普通双引号 `"..."`，同一个反斜杠要写两遍
  （`"^/runtime-config\\.js$"`）—— **这就是为什么要用 `r"..."`**。
- **语法就是 RE2**（[RE2 官方语法表](https://github.com/google/re2/blob/main/doc/syntax.txt)；
  Cloudflare 用的是 Rust 正则引擎，与 RE2 同源）。逐条对照：

  | 想要 | RE2 写法 | 能不能 |
  | --- | --- | --- |
  | 字面点 | `\.` | ✅ |
  | 整段文本的首 / 尾 | `^` / `$` | ✅（默认 `m=false`，锚的是**文本**首尾，不是行首行尾） |
  | 分组 | `(...)` / `(?:...)` | ✅ |
  | 重复 | `*` `+` `?` `{n,m}` | ✅（但 `{n,m}` 的上下限不能超过 1000） |
  | 忽略大小写 | `(?i)` 或标志 `i` | ✅（但路径**应该**区分大小写，别加） |
  | **反向引用** | `\1` `\g{1}` `\k<name>` | ❌ **不支持** |
  | **环视** | `(?=...)` `(?!...)` `(?<=...)` `(?<!)` | ❌ **不支持** |
  | **占有量词** | `x*+` `x++` | ❌ **不支持** |

  每条规则最多 64 个正则。

**规则的叠加顺序**：官方口径是「**最后一条匹配的规则获胜**」（多条都命中时，
靠后的覆盖靠前的设置；「bypass vs eligible」这种冲突也是后者赢）。
本节这四条**互斥或结论一致**（③④ 互斥，①② 与 ④ 都是 Bypass），
所以**顺序怎么排都不影响结果**；但按 ①→④ 排更符合读法。

⚠️ 这个「互斥」是**靠 `^` 撑着的** —— 插件资产路径里含 `/assets/`，
③ 一旦漏了 `^`，它就会跟 ① 同时命中（见上面那条）。所以 `^` **不是可选的**。

**配完自己验一遍**（本地就能跑，不用等 CDN 生效）：

```bash
printf '%s\n' /api/books/1/vouchers /api/plugins/assets/p/abc/frontend/t/tok/main.js \
  /api/books/1/vouchers.xlsx /assets/index-CUpwOnfx.js /runtime-config.js \
  /downloads/setup.exe /foo/assets/bar.js \
  | rg -e '^/api/' -e '^/assets/' -e '^/runtime-config\.js$'
```

`rg` 底层就是 Rust 正则引擎（与 Cloudflare 同源），所以它验出来的行为就是
CDN 上会发生的。期望结果：命中 `/api/` 三条 + `/assets/` 一条 +
`/runtime-config.js` 一条；**`/foo/assets/bar.js` 不该出现** ——
它出现了就说明 `^` 漏了。

> **地址栏里的 `#` 不用管。** 本项目是 **hash 路由**
> （`createWebHashHistory()`，`frontend/src/router/index.js`），地址栏形如
> `https://acct.xins.live/#/books`。但 **fragment 永远不会进 HTTP 请求**
> （RFC 3986）—— 浏览器发出去的是 `GET /`，CDN 看到的路径就是 `/`。
> 后端 API 走的是**另一条独立请求**（`GET /api/books/...`，由 `apiBase`
> 拼出，见 `frontend/src/config.js`），路径里同样没有 `#`。
> 所以按 `/api/`、`/assets/` 匹配**完全不受 hash 路由影响**。

**①为什么必须加**：

- **插件资产**（`/api/plugins/assets/{plugin_id}/{digest}/frontend/t/{token}/…`，
  见 `backend/app/plugins/api.py`）返回的是
  `Cache-Control: public, max-age=31536000, immutable`。它在 `/api/` 路径下，
  但 **Cloudflare 的默认判定只看扩展名**（`.js` / `.css` / `.png` 都在列表里），
  **路径前缀不算** —— 于是它会被**缓存一年**。
  后果：票据是**短期凭据**（`verify_asset_token` 校验），边缘缓存一年等于把
  「短期」这个语义**抹掉** —— 票据过期后同一条 URL 仍然打得开，边缘直接命中，
  压根不回源做票据校验。
  它也躲不过 OCC 那条兜底：浏览器是用 `import()` 动态加载插件资产的
  （`frontend/src/plugins/runtime.js`），**模块脚本不带 `Authorization` 头**，
  OCC 的「带 Authorization 的响应必须同时有 `public` / `s-maxage` /
  `must-revalidate` 才缓存」对它不适用。
- **导出接口**（`/api/books/{id}/vouchers.xlsx`、`balance.xlsx`、
  `general-ledger.xlsx`、`report.xlsx`、`detail-ledger.xlsx`、`openings.xlsx`）
  **一个 `Cache-Control` 都没设**，只设了 `Content-Disposition`。
  `.xlsx` 在默认缓存扩展名列表里 → 按默认 Edge TTL **缓存 120 分钟**。
  现在之所以没出事，靠的是 axios 带上 `Authorization` 头 + OCC 那条
  **行为**兜着。**行为不是规则** —— 哪天有人给导出接口加一句看起来很合理的
  `Cache-Control: public, max-age=600`，它就变成「把 A 账套的账目发给 B」。
- 代价：插件资源每次都回源。插件包不大，源站只吃插件那一部分流量，
  **可以接受**，而且是刻意的：浏览器侧那个 `immutable` **不要动**，
  否则票据过期后插件页会崩。
- 想再稳一档的话，可以把 `backend/app/plugins/api.py` 里那个响应头从
  `public, max-age=31536000, immutable` 改成 `private, max-age=31536000, immutable`
  —— 浏览器照样缓存一年，但共享缓存按 RFC 7234 一律不存（`private` 的本义）。
  规则 ① 已经够了，这一步是可选的加固。

**②为什么必须加**：`runtime-config.js` 就是 `.js`，在默认缓存扩展名列表里。
源站已经给了 `no-cache, must-revalidate`，所以它**不会**发陈旧内容（改了配置
照样生效）—— 但边缘仍会存一份，**每次访问都要回源重验一次**。显式 bypass
让它每次真回源，不占边缘存储，也少一次条件请求往返。
另外它决定「API 在哪」「静态资源在哪」，**每套部署都不同**，不该让任何
一个中间层去缓存它。

**③怎么配**：Edge TTL 选 **① `bypass_by_default`**（有 `cache-control` 就按
它来，没有就不缓存）。`/assets/` 的响应带着
`public, max-age=31536000, immutable`，所以它照样缓存一年 —— 与选项 ②
在这里结果相同，但 ① 更保守：将来新增什么路径忘了设头，就是不缓存，
而不是默默缓存 120 分钟。
（这条规则本身是**把默认行为显式写出来**：默认行为本来也会缓存 `/assets/`。
留着它的价值是「显式优于隐式」—— 将来谁动 Cache Level 或加一条宽泛的
Bypass 规则时，不会把静态资源误伤掉。）

**推荐形态：再加一条兜底，做成路径白名单**

上面三条覆盖了所有 ⚠️，但**没被任何规则命中的路径**（`/`、`/downloads/`、
`/docs` 等）仍然走默认行为 —— `/downloads/*.exe` 会被缓存 120 分钟。
想彻底关掉的话，再加一条规则：

| # | 匹配表达式 | 缓存资格 | Edge TTL |
| --- | --- | --- | --- |
| ④ 兜底 | `not starts_with(http.request.uri.path, "/assets/")` | **Bypass cache** | — |

（等价的其它写法：`not http.request.uri.path wildcard "/assets/*"`、
`not http.request.uri.path matches r"^/assets/"`。）

加上它之后，**缓存资格就从「扩展名启发式」变成了路径白名单** —— 只有
`/assets/` 进缓存，其余一律不进。这是本项目更想要的形态：`/downloads/`
里的 exe 几十 MB，让它每次回源反而更符合「省源站带宽」的目标。

### 4.4 必须关掉的开关

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
| **发布后界面没变**（还是上一版） | 入口 HTML 被 CDN 缓存了 | `curl -sI / \| grep -i cf-cache-status`；若是 HIT，说明兜底规则（4.3 的 ④）没配 |
| **改了 runtime-config 不生效** | `/runtime-config.js` 被缓存 | 同上；4.3 的规则 ② 没配 |
| 插件票据过期后资源**还能打开** | 插件资产被边缘缓存了一年 | `curl -sI <插件资产> \| grep -i cf-cache-status`；4.3 的规则 ① 没配 |
| 某台机器登不进去、别人正常 | IP 维度的节流误伤（CDN 边缘 IP 分桶） | 装 `cwq-cloudflare-realip.conf`；查后端「登录防护」页面 |
| 偶发 **524** | 接口耗时超 125 秒 | 看 Cloudflare 的 524 报表与源站慢日志 |
| MCP 连一会儿就断 | SSE 静默 125 秒被边缘掐断 | MCP 走 DNS-only 子域 |
| 源站 IP 被人直接访问 | 安全组没只放行 Cloudflare 网段 | 收紧安全组 |
| 源站日志里的 IP 全是 Cloudflare 地址 | 没装 realip 配置 | 见第 3 节 |
