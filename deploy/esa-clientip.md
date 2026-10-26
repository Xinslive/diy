# ESA 在前时的真实客户端 IP（凭令牌还原，不用付费的回源网段）

> 目标形态：源站后面挂了 ESA，nginx 与后端拿到的仍然是**用户的真实 IP**，
> 而不是 ESA 的边缘节点地址。
>
> ```
> 浏览器 ──HTTPS──▶ ESA 边缘
>                    └── 回源（HTTPS + mTLS）──▶ nginx ──▶ FastAPI
>                        ↑ 回源时 ESA 带上两个头：
>                          ali-real-client-ip = 真实客户端 IP
>                          X-CWQ-Origin-Token = 只有 ESA 知道的令牌
> ```
>
> 适用站点：`acct.xins.live`（源站 `43.138.178.174`，nginx 1.26.3）。

---

## 0. 为什么需要它

ESA 在前面时，nginx 看到的 TCP 对端是 **ESA 的边缘节点 IP**。后果有两条，
**都不报错**，只是安静地错了：

| 后果 | 具体表现 |
| --- | --- |
| **登录节流按边缘节点分桶** | `LOGIN_IP_MAX_FAILURES=30` / 15 分钟窗口。同一节点后面成千上万个用户共用一个桶 —— 一个人在连着试错，同一节点上的其他人一起被挡（误伤） |
| **审计日志里全是节点地址** | `login_audit` 等表记的是 `113.96.129.x` 这种地址 → 出事查不到人 |

顺带一句：审计那一条其实是**可修可不修**的 —— 审计用的 `_client_ip` 取
`X-Forwarded-For` 的**最左**元素，而对普通浏览器来说 ESA 发的 XFF 就是真实
IP（只有客户端自己伪造 XFF 时才会歪）。**真正非修不可的是节流**，因为它取
`X-Real-IP`（= nginx 看到的 TCP 对端 = 边缘节点）。

---

## 1. 机制：三个 map + 一道令牌闸门

源站侧的配置在 `deploy/nginx/cwq.conf` 与 `deploy/nginx/acct.xins.live.conf`
里（两份模板各一份，生产那一份手工同步）：

```
ali-real-client-ip ──▶ $cwq_esa_client_ip     解析出一个 IP（取最右那个）
X-CWQ-Origin-Token ─▶ $cwq_esa_token_ok      令牌对不对（0/1）
                            │
                            ▼
                    $cwq_client_ip = 令牌对上 ? ESA 说的 IP : $remote_addr
```

`$cwq_client_ip` 会同时写进两处：

| 写给谁 | 头 | 作用 |
| --- | --- | --- |
| 后端 | `X-Real-IP` | 节流按**真实用户 IP** 分桶（不再按边缘节点） |
| 后端 | `X-Forwarded-For` | 审计日志记**真实用户 IP**（不再记节点地址） |

### 1.1 为什么不能直接信 `ali-real-client-ip`

**源站 IP 是公开的**：`43.138.178.174` 直连就能打进来，而且源站自己会把这个头
原样转给后端。实测（2026-10-26）直连时自己塞一个头：

```
$ curl -H "Host: acct.xins.live" -H "ali-real-client-ip: 6.6.6.6" https://43.138.178.174/
→ 源站日志：ali="6.6.6.6"      # 照单全收
```

裸信这个头 = 把「登录节流的分桶钥匙」交给任何能连到源站的人：攻击者每个请求
换一个头就换一个桶，IP 维度限流等于不存在。

### 1.2 闸门为什么是「令牌」而不是 mTLS

源站已经开了回源双向校验（mTLS），看上去可以直接拿 `$ssl_client_verify` 当
闸门 —— **不行**。源站装的那个信任锚是：

```
subject=C=CN, ST=Zhejiang, L=HangZhou, O=Alibaba Cloud, OU=Alibaba Cloud SSL Certificate Authority
```

这是**阿里云的公共 SSL 签发 CA**（ESA 的客户端证书链是
`Alibaba Cloud ESA Origin Certificate` → `Alibaba Cloud ESA Origin CA` →
**这一支**），也就是说它给全阿里的 HTTPS 站点签发过证书。任何拿到阿里云
证书的人都可以把它当客户端证书递过来 —— nginx 默认不校验证书用途
（clientAuth），于是「握手成功」根本不能证明「对方是 ESA」。

**令牌没有这个问题**：它只有一个来源（你自己的 ESA 控制台），别人拿不到。
以后若买了 ESA 的「源站防护」（付费），它给的回源网段可以做第二道闸，
但**不要**用它替掉令牌 —— 网段会变，漏了新区段会静默退化。

---

## 2. 源站侧（✅ 已就位）

| 项 | 位置 |
| --- | --- |
| 三个 map + `map_hash_bucket_size` | 站点配置的 http 级（`deploy/nginx/*.conf`） |
| 令牌文件 | `/etc/nginx/cwq-include/cwq-esa-token.conf`（`root:root` `600`，**不进版本库**） |
| 访问日志 | 多两个字段：`cip=`（我们信的 IP）、`ali=`（ESA 说的 IP） |

令牌文件里只有一行，形如：

```nginx
"<48 个十六进制字符>" 1;
```

### 2.1 为什么还要 `map_hash_bucket_size 128`

令牌键 48 个字符，而 nginx 默认的 `map_hash_bucket_size` 只有 **64 字节** ——
一个桶装不下「键 + 值指针 + 链表指针」（≈ 键长 + 17 字节）。装不下时：

```
nginx: [emerg] could not build map_hash, you should increase map_hash_bucket_size: 64
nginx: configuration file /etc/nginx/nginx.conf test failed
```

**整份配置加载失败**。现场很隐蔽：`reload` 失败 → **旧进程继续服务** → 新配置
一条都没上去，而访问日志一切正常。所以这个值必须显式调大，并且**写在令牌 map
之前**（map 创建时才读它）。CI 里专门有一条用例装上**真的长**的假令牌跑
`nginx -t`（`deploy/tests/nginx_conf_check.py`），就是为了守住这个坑。

### 2.2 令牌文件不在时是什么样

`include` 用的是**通配**（`cwq-esa-token*.conf`）：

| 状态 | 行为 |
| --- | --- |
| 文件在 | 令牌比对生效 |
| 文件不在 | **不报错**，`$cwq_esa_token_ok` 恒为 0 → 一律按 TCP 对端计 |

也就是「还没配令牌」是一个**干净的关闭状态**，而不是 nginx 起不来。

---

## 3. ESA 控制台侧（⬜ 需要你做一步）

**站点配置 → 回源配置 → 回源请求头（或「自定义回源请求头」）**，加一条：

| 字段 | 值 |
| --- | --- |
| 头名 | `X-CWQ-Origin-Token` |
| 值 | 令牌（见下） |

`ali-real-client-ip` **不用管** —— ESA 默认就会在回源时带上它（实测确认）。

令牌值：`/etc/nginx/cwq-include/cwq-esa-token.conf` 里那一行中间那段。
（它不在仓库里 —— 仓库只放机制，不放密钥。）

加完之后，源站日志里同一个请求的 `cip=` 应该变成**真实用户 IP**，
而 `ali=` 一直是同一个值：

```
# 之前（令牌还没配）
... cip="112.48.144.38" ali="223.104.53.152" mtls=SUCCESS ...
# 之后
... cip="223.104.53.152" ali="223.104.53.152" mtls=SUCCESS ...
```

---

## 4. 怎么验证

```bash
# 走 ESA（域名）—— cip 应该是你的公网 IP
curl -sI https://acct.xins.live/ >/dev/null
tail -n 1 /var/log/nginx/acct.xins.live.log | grep -o 'cip="[^"]*" ali="[^"]*"'

# 直连源站并伪造 ali-real-client-ip —— cip 必须仍然是真实对端，不能是 6.6.6.6
curl -sI -H "Host: acct.xins.live" -H "ali-real-client-ip: 6.6.6.6" \
     https://43.138.178.174/ -k >/dev/null
tail -n 1 /var/log/nginx/acct.xins.live.log | grep -o 'cip="[^"]*" ali="[^"]*"'
# 期望：cip="<你的公网 IP>" ali="6.6.6.6"
```

第二条是**安全边界**的验收：直连伪造时 `ali` 会被记下来（可观测），
但 `cip` 绝不能变成 `6.6.6.6`。

---

## 5. 回滚

```bash
# 1) 去掉令牌（源站立刻退回「按 TCP 对端计」，配置不用动）
rm -f /etc/nginx/cwq-include/cwq-esa-token.conf   # 通配 include：文件不在不报错

# 2) 连机制一起撤（配置有备份）
cp -a /etc/nginx/conf.d/acct.xins.live.conf.bak-esa-clientip-<时间戳> \
       /etc/nginx/conf.d/acct.xins.live.conf
nginx -t && nginx -s reload
```

> 改 nginx 永远 `nginx -t` 通过再 `nginx -s reload`，**不要 restart** ——
> 这台机上还跑着另外十几个站点，restart 失败会连累它们。

---

## 6. 已知限制

1. **令牌在 ESA 控制台里是明文**。控制台能看的人就能拿到令牌 —— 拿到令牌的人
   可以伪造源站请求里的 `ali-real-client-ip`。这比「裸信」好得多（要先进控制台），
   但不是零风险。
2. **直连源站的流量拿不到真实 IP**（那是它自己的 TCP 对端，本来就对）。
3. **ESA 若改了头名**（`ali-real-client-ip`），源站这侧要同步改 map 的键。
4. **mTLS 那边**：`ssl_verify_client` 仍是 `optional`（过渡态），
   详见 `deploy/esa-mtls.md`。

---

## 7. 相关文件

| 文件 | 作用 |
| --- | --- |
| `deploy/nginx/cwq.conf` / `deploy/nginx/acct.xins.live.conf` | 三个 map + `proxy_set_header` |
| `deploy/tests/nginx_conf_check.py` | 9 条断言守住这套机制（令牌闸门、通配 include、不硬编码、兜底、桶大小…） |
| `deploy/esa-mtls.md` | 回源双向校验（mTLS）那件事 |
| `ARCHITECTURE.md` §11.17.6 | 信任链的分工（CDN 网段/令牌写 nginx，后端只认 nginx） |
