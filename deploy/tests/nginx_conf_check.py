#!/usr/bin/env python3
"""nginx 配置校验：结构校验 + **真的** ``nginx -t``（装了 nginx 就跑）。

为什么要有这个脚本
------------------
它服务的是两条具体的缺陷，两条都**只有真跑一遍才抓得住**：

* **P2-3**：宝塔形态的 vhost（``acct.xins.live.conf``）以前只有 location 级的
  ``include .../cwq-rl-api*.conf``（引用 zone），**没有** http 级的
  ``include .../cwq-rl-zone*.conf``（定义 zone）。而 ``limit_req_zone`` 的
  合法上下文只有 http —— 于是「按文档启用限流」时 ``nginx -t`` 直接报
  ``unknown limit_req_zone "cwq_api"``，配置根本加载不上去。
  换句话说：**限流在这个部署形态下永远开不了**。
  本脚本按文档把那两个文件建出来，然后真的跑一次 ``nginx -t`` ——
  「按文档启用」这条路径本身必须能过。

* **P3-4/P3-5**：文件头与自身内容互相矛盾、指向一节不存在的内容；
  ``http2 on;`` 没标 nginx 版本要求（1.25.1 之前只有 ``listen ... http2``，
  老版本上 ``nginx -t`` 报 unknown directive "http2" → 整份配置不生效，
  而 reload 失败是**静默的**：旧进程继续服务，新配置一条都没上去）。

用法
----
    python deploy/tests/nginx_conf_check.py [仓库根目录]
    python deploy/tests/nginx_conf_check.py --require-nginx   # CI 用

``--require-nginx``：**必须真的装上 nginx 并跑一遍 nginx -t**，没装就判失败。
CI 的 deploy job 传这个开关 —— 否则 CI 上万一 nginx 没装上，这道闸会
「跳过 → 全绿 → 什么也没验」，变成一道假闸。

**版本闸**：本仓库的配置用了 ``http2 on;``（1.25.1 才引入的独立指令），
所以 nginx 必须 **>= 1.25.1**。版本不够时脚本**明确报错并说明
「nginx -t 的失败与配置无关」**，而不是拿旧 nginx 的报错去指认配置。

退出码 0 = 全过；非 0 = 有问题（CI 会红）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PASS = 0
FAIL = 0


def ok(msg: str) -> None:
    global PASS
    PASS += 1
    print(f"  OK   {msg}")


def no(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL {msg}")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def strip_comments(text: str) -> list[str]:
    """去掉整行注释（nginx 的注释只有 # 开头到行尾这一种）。

    只处理「整行都是注释」的情况 —— 配置里的行尾注释极少，而把行尾的
    ``# ...`` 也砍掉会误伤 ``map`` 里的 ``default "";  # 注释`` 这种。
    这里保守一点：整行注释去掉，行尾不动。
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        out.append(line)
    return out


def depth_after(lines: list[str]) -> list[tuple[int, str]]:
    """返回 [(该行结束时的花括号深度, 该行内容)]。"""
    depth = 0
    res = []
    for line in lines:
        # 行内注释里的花括号不参与配平（nginx 里 # 之后到行尾都是注释）
        code = line.split("#", 1)[0]
        depth += code.count("{") - code.count("}")
        res.append((depth, line))
    return res


def location_blocks(lines: list[str], header: str) -> list[list[str]]:
    """取出所有以 ``header`` 开头的 location 块（按花括号配平）。

    ``header`` 形如 ``"location / {"`` —— 用 ``startswith`` 匹配，
    所以 ``location / {`` 不会误取 ``location /assets/ {``。

    **要返回全部而不是第一个**：同一份配置里 ``location / {`` 可能出现两次
    —— 80 端口那个是「跳 HTTPS」，443 端口那个才是 SPA 兜底。
    只看第一个会把断言安到错误的块上（于是「加了 header 也判失败」，
    或者更糟：把它加在跳转块上反而算通过）。

    ------------------------------------------------------------------
    **块的边界必须掐准**（这里以前掐错过）
    ------------------------------------------------------------------
    原来的结束条件是 ``depth <= 0 and "{" in l2`` —— 而 location 块的
    收尾那行通常**只有一个 ``}``、不含 ``{``**，于是这个条件不成立，
    块**没有在那里结束**，而是一路吞掉了后面的块。

    后果不是「多看了几行」这么轻：像「``/api/`` 段不许带
    Access-Control-Allow-Origin」这种断言，判的其实是
    「``/api/`` **以及它后面所有块**」—— 后面某个块带了那个头，断言就会
    误报「``/api/`` 带了 ACAO」；反过来，要是在别的块里加了 ACAO 也可能
    被算成「``/api/`` 的问题」。断言指错了对象，就等于没有断言。

    正确的结束条件是**花括号配平**（depth 回到 0）：块头那行把 depth 变成
    1，收尾那行把它变回 0。
    """
    blocks: list[list[str]] = []
    for i, ln in enumerate(lines):
        if not ln.strip().startswith(header):
            continue
        depth = 0
        out: list[str] = []
        for l2 in lines[i:]:
            code = l2.split("#", 1)[0]
            depth += code.count("{") - code.count("}")
            out.append(l2)
            if depth <= 0:
                break
        blocks.append(out)
    return blocks


def balanced(lines: list[str]) -> tuple[bool, str]:
    depth = 0
    for line in lines:
        code = line.split("#", 1)[0]
        depth += code.count("{") - code.count("}")
        if depth < 0:
            return False, "有多余的右花括号"
    if depth != 0:
        return False, f"花括号不配平（结束时深度 {depth}）"
    return True, ""


def server_block_with(lines: list[str], needle: str) -> list[str] | None:
    """包含 needle 的那个 server 块（按花括号配平）；找不到返回 None。"""
    for i, ln in enumerate(lines):
        if ln.strip() != "server {":
            continue
        depth = 0
        block: list[str] = []
        for l2 in lines[i:]:
            code = l2.split("#", 1)[0]
            depth += code.count("{") - code.count("}")
            block.append(l2)
            if depth <= 0:
                break
        if any(needle in l for l in block):
            return block
    return None


def _indent_of(line: str) -> int:
    """行首空格数（tab 按 1 记）。用来区分「哪一层」写的指令。"""
    return len(line) - len(line.lstrip(" \t"))


def all_location_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    """所有 location 块：[(块头那一行, 块内所有行)]。

    ``location_blocks()`` 是「按块头精确匹配」，这里要的是「**所有** location」——
    因为要判的是一条**不变量**（凡是 API 段都得满足某个条件），
    漏掉任何一个 location，那条不变量就名存实亡。

    边界同样按花括号配平取。``location / {`` 与 ``location /assets/ {``
    用 ``startswith("location ")`` + ``endswith("{")`` 一起卡，不会互相误取。
    """
    out: list[tuple[str, list[str]]] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not (s.startswith("location ") and s.endswith("{")):
            continue
        depth = 0
        block: list[str] = []
        for l2 in lines[i:]:
            code = l2.split("#", 1)[0]
            depth += code.count("{") - code.count("}")
            block.append(l2)
            if depth <= 0:
                break
        out.append((s, block))
    return out


# --------------------------------------------------------------------------
# 结构校验
# --------------------------------------------------------------------------

def check_structure(conf: Path) -> None:
    print(f"== 结构校验：{conf.name} ==")
    raw = conf.read_text(encoding="utf-8")
    lines = strip_comments(raw)

    good, why = balanced(lines)
    if good:
        ok("花括号配平")
    else:
        no(f"花括号不配平：{why}")

    # 引号配平（每行的单引号个数必须是偶数）
    bad_quote = [ln for ln in lines if ln.count("'") % 2 or ln.count('"') % 2]
    if bad_quote:
        no(f"有引号不配平的行：{bad_quote[:2]}")
    else:
        ok("引号配平")

    depth_rows = depth_after(lines)

    # --- 限流：zone 的定义与引用必须成对 ---
    zone_include_at_http = any(
        d == 0 and "cwq-rl-zone" in ln for d, ln in depth_rows
    )
    api_include_in_location = any(
        d > 0 and "cwq-rl-api" in ln for d, ln in depth_rows
    )
    if api_include_in_location:
        if zone_include_at_http:
            ok("限流的两半都在：http 级有 zone 定义、location 级有引用")
        else:
            no("只有 location 级的 limit_req 引用，没有 http 级的 zone 定义 —— "
               "按文档启用限流时 nginx -t 会报 unknown limit_req_zone")
    else:
        ok("没有引用 limit_req zone 的 location 级 include")

    # --- http2 on; 的版本要求 ---
    if any(re.search(r"^\s*http2\s+on\s*;", ln) for ln in lines):
        if "1.25.1" in raw:
            ok("http2 on; 标了 nginx 版本要求（1.25.1+）")
        else:
            no("用了 http2 on; 却没标 nginx 版本要求 —— "
               "1.25.1 之前的 nginx 上 nginx -t 会报 unknown directive \"http2\"")
    else:
        ok("没有用独立的 http2 on; 指令")

    # --- 文件头不许指向不存在的小节 ---
    if "tuning.md 第八节" in raw and "限流" in raw:
        no("文件头把限流指向 deploy/tuning.md 第八节 —— 那一节讲的是 "
           "TRUSTED_PROXIES，与限流无关（死指针）")
    else:
        ok("没有把限流指向不存在的小节")

    # --- 入口页必须 no-cache，带哈希的资源必须 immutable ---
    #
    # 少任何一条都会变成「发布成功、界面没变」：
    #   · 入口页没有 no-cache → 浏览器按启发式规则缓存它（Chrome/Edge 拿
    #     「距 Last-Modified 的 10%」当新鲜期），发布后一两个小时内客户端
    #     拿到的还是上一个版本的 index.html，它指向旧哈希资源。
    #   · 资源没有 immutable → 每次访问都重下，白白浪费带宽。
    # 两条一起才是完整答案：**入口永远重新验证，资源永远不过期**。
    # SPA 块 = 那些把请求回退到 /index.html 的 location（80 端口那个
    # 「跳 HTTPS」的同名块不算）。断言必须落在**真正吐 HTML 的那一块**上。
    spa = [b for b in location_blocks(lines, "location / {")
           if any("/index.html" in l for l in b)]
    assets = location_blocks(lines, "location /assets/ {")
    if any(re.search(r'add_header\s+Cache-Control\s+"no-cache"', l)
           for b in spa for l in b):
        ok("SPA 入口页带 Cache-Control: no-cache（发布后客户端不会拿着旧 index.html）")
    else:
        no("SPA 入口页没有 Cache-Control: no-cache —— 浏览器会按启发式规则缓存它，"
           "发布后客户端拿到的还是上一个版本的 index.html，界面看着像「没生效」")
    if any("immutable" in l for b in assets for l in b):
        ok("带内容哈希的 /assets/ 仍然 immutable 一年")
    else:
        no("/assets/ 少了 immutable —— 入口已经每次重新验证了，资源不该再每次重下")

    # --- 动静分离：静态资源要能跨域取，运行时配置不许被缓存 ---
    #
    # 这两条是「静态资源走 CDN」能成立的前提：
    #   · /assets/ 必须带 Access-Control-Allow-Origin —— **跨域加载 ES module
    #     一律走 CORS 校验**，没有「不校验」的选项。缺了它，CDN 上的那份
    #     脚本会被浏览器直接拒绝执行，页面白屏（控制台一条 CORS 错误）。
    #   · /runtime-config.js 必须 no-cache —— 它决定「API 在哪」「静态资源
    #     在哪」，每套部署都不同。缓存住了就是「改了配置不生效」。
    if any("Access-Control-Allow-Origin" in l for b in assets for l in b):
        ok("/assets/ 带 Access-Control-Allow-Origin（CDN 上的 ES module 能跨域执行）")
    else:
        no("/assets/ 没有 Access-Control-Allow-Origin —— 资源搬到 CDN 之后浏览器会拒绝执行")

    runtime_cfg = location_blocks(lines, "location = /runtime-config.js {")
    if runtime_cfg and any("no-cache" in l for b in runtime_cfg for l in b):
        ok("运行时配置带 no-cache（改了 API/CDN 地址立刻生效）")
    else:
        no("/runtime-config.js 没有 no-cache —— 配置会被缓存住，改了不生效")

    # **/api/ 绝不能带 ACAO**：那是业务数据。放开跨源等于任何网站都能读
    # 未鉴权的接口响应。这条是「别把静态那条复制到动态那条上」的钉子。
    api_blocks = location_blocks(lines, "location /api/ {")
    if any("Access-Control-Allow-Origin" in l for b in api_blocks for l in b):
        no("/api/ 带了 Access-Control-Allow-Origin —— 业务数据不能对任意源开放")
    else:
        ok("/api/ 没有 Access-Control-Allow-Origin（业务数据不对任意源开放）")

    # --- 静态资源的 Cache-Control 只能有一条 ---
    #
    # CDN 是靠这条头决定缓存行为的。两条（`expires` 自己那条 +
    # add_header 那条）浏览器会合并，CDN 不一定 —— 可能挑错、或只认其中
    # 一条，于是「带宽没降下来」而**没有任何报错**。
    cc_lines = [l for b in assets for l in b
                if "Cache-Control" in l and not l.strip().startswith("#")]
    if len(cc_lines) <= 1:
        ok("/assets/ 只有一条 Cache-Control（CDN 按它决定缓存行为）")
    else:
        no(f"/assets/ 有 {len(cc_lines)} 条 Cache-Control —— CDN 可能挑错其中一条，"
           "而带宽没降下来时是没有任何报错的")

    # --- brotli 预压缩资源（2026-10-26 加）---
    #
    # 这几条盯的是**踩过的坑**，不是形式：
    #   · /assets/ 里必须 gzip off —— 否则 gzip 过滤器把「已经是 brotli 的
    #     字节」再压一遍，响应里出现**两个** Content-Encoding，
    #     浏览器解完 gzip 再解 brotli，页面直接坏掉；
    #   · Content-Encoding 必须由变量给出，且那个变量的判据是 **$uri**
    #     （这次实际发出的是哪一份），不能写死 br、也不能按「客户端要什么」
    #     给 —— 客户端要 br 但这个资源没有 br 版本时，写死就会把没压过的
    #     字节当 brotli 发出去；
    #   · Vary: Accept-Encoding 必须有 —— 没有它，缓存会把 br 那份发给
    #     不支持 br 的客户端；
    #   · br 树不许被直接访问。
    def _http_level_map(var: str) -> str | None:
        """某个 map 是不是在 http 级；是的话返回它的源变量名，不是返回 None。"""
        depth = 0
        pat = re.compile(r"^map\s+(\$\w+)\s+" + re.escape(var) + r"\s*\{")
        for ln in lines:
            code = ln.split("#", 1)[0].strip()
            m = pat.match(code)
            if m:
                return m.group(1) if depth == 0 else ""
            depth += code.count("{") - code.count("}")
        return None

    prefix_src = _http_level_map("$cwq_br_prefix")
    enc_src = _http_level_map("$cwq_br_encoding")
    if prefix_src == "$http_accept_encoding" and enc_src == "$uri":
        ok("brotli 的两个 map 都在 http 级，且编码头的判据是 $uri（实际发出的那份）")
    else:
        no(f"brotli 的 map 不对：前缀 {prefix_src or '缺'}、编码头 {enc_src or '缺'}"
           " —— 编码头必须由 $uri 判定（客户端要 br 但资源没有 br 版本时，"
           "按客户端给就会把没压过的字节当 brotli 发出去）")

    br_plain = location_blocks(lines, "location @cwq_assets_plain {")
    if assets:
        a = assets[0]
        if any(l.strip() == "gzip off;" for l in a):
            ok("/assets/ 关掉了 gzip（否则 brotli 文件会被再压一遍，出两个 Content-Encoding）")
        else:
            no("/assets/ 没有 gzip off —— brotli 文件会被 gzip 再压一遍，"
               "响应里两个 Content-Encoding，浏览器解出来是乱的")

        enc_hdrs = [l for l in a if "add_header" in l and "Content-Encoding" in l]
        if enc_hdrs and all("$cwq_br_encoding" in l for l in enc_hdrs):
            ok("/assets/ 的 Content-Encoding 由 $cwq_br_encoding 给出（按实际发出的那份判）")
        else:
            no(f"/assets/ 的 Content-Encoding 不是由 $cwq_br_encoding 给的：{enc_hdrs}")

        if any("$cwq_br_prefix$uri" in l for l in a) and \
                any("error_page 404 = @cwq_assets_plain;" in l for l in a):
            ok("/assets/ 会去 br 树取，取不到就落到普通分支")
        else:
            no("/assets/ 没有走 br 树（$cwq_br_prefix$uri），或没有落到 @cwq_assets_plain")

        if any("add_header Vary" in l and "Accept-Encoding" in l for l in a):
            ok("/assets/ 带 Vary: Accept-Encoding（否则缓存会把 br 那份发给不支持的客户端）")
        else:
            no("/assets/ 没有 Vary: Accept-Encoding —— 缓存会把 brotli 那份发给不支持 br 的客户端")
    else:
        no("找不到 location /assets/ 块")

    if br_plain:
        b = br_plain[0]
        if any(l.strip() == "gzip on;" for l in b):
            ok("普通分支（@cwq_assets_plain）开着 gzip（不支持 br 的客户端靠它压缩）")
        else:
            no("@cwq_assets_plain 没有 gzip on —— 不支持 br 的客户端会拿到没压缩的资源")
        if any("add_header Vary" in l and "Accept-Encoding" in l for l in b):
            ok("普通分支也带 Vary: Accept-Encoding")
        else:
            no("@cwq_assets_plain 没有 Vary: Accept-Encoding")
    else:
        no("没有 location @cwq_assets_plain —— /assets/ 取不到 br 版本时没有去处")

    br_deny = location_blocks(lines, "location ^~ /br/ {")
    if br_deny and any("return 404;" in l for l in br_deny[0]):
        ok("br 树不许被直接访问（对外发裸 brotli 字节，浏览器会当普通文件解）")
    else:
        no("没有挡掉 /br/ 的直接访问")

    # --- HTTP/3（QUIC）与 Alt-Svc 宣告 ---
    #
    # **为什么源站要开 HTTP/3**：走 ESA 的客户端与 ESA 之间已经是 HTTP/3
    # （ESA 已经在发 alt-svc: h3=":443"），源站开 h3 服务的是**绕过 CDN
    # 直连源站的那部分流量** —— 也就是按线路解析里没走 ESA 的那条线。
    #
    # **Alt-Svc 为什么必须逐个 location 写**：nginx 的 add_header 是
    # **逐层替换不是合并** —— 某一层只要写了任何一条 add_header，
    # 上一层那几条就**全部不继承**。所以只在 server 层写一条 Alt-Svc
    # 是不够的：凡是自带 add_header 的 location（/assets/、/api/ …）
    # 都会把它顶掉，于是那些路径上的响应**没有** Alt-Svc ——
    # 浏览器压根不知道可以用 h3。
    #
    # 这条断言盯的是**不变量**（凡是自带 add_header 的 location 都得
    # 宣告 Alt-Svc），而不是「某个地方有一行」—— 后者挡不住将来新增
    # 一个 location 忘了写。
    srv = server_block_with(lines, "listen 443 ssl;")
    if srv is None:
        no("找不到含 listen 443 ssl 的 server 块")
    else:
        if any(l.strip() == "listen 443 quic reuseport;" for l in srv):
            ok("HTTPS 段开了 QUIC 监听（同一端口号、不同协议：TCP 是 TLS，UDP 是 QUIC）")
        else:
            no("HTTPS 段没有 listen 443 quic reuseport —— HTTP/3 不会生效")
        if any(l.strip() == "http3 on;" for l in srv):
            ok("HTTPS 段开了 http3 on;")
        else:
            no("HTTPS 段没有 http3 on;")
        # **必须按缩进区分层级**：srv 是整个 server 块的行，**包含**
        # 里面每一个 location 的行。只判「块里有没有 Alt-Svc」的话，
        # location 里那份会把 server 层那份顶掉 —— 断言看着在盯
        # server 层，实际盯的是「整块里随便哪一处有」。
        # server 层自己的指令缩进是 4 空格，location 里的是 8 空格。
        if any(_indent_of(l) == 4 and "Alt-Svc" in l for l in srv):
            ok("HTTPS 段宣告了 Alt-Svc")
        else:
            no("HTTPS 段（server 层）没有 Alt-Svc —— 浏览器不知道可以用 h3")

        missing_alt = []
        for head, blk in all_location_blocks(lines):
            if not any(l.strip().startswith("add_header") for l in blk):
                continue
            # 同理：location 自己的 Alt-Svc 是 8 空格缩进，
            # 不能拿「块里任意深度出现 Alt-Svc」当数。
            if not any(_indent_of(l) == 8 and "Alt-Svc" in l for l in blk):
                missing_alt.append(head)
        if missing_alt:
            no(
                f"{len(missing_alt)} 个自带 add_header 的 location 没有 Alt-Svc："
                + "；".join(missing_alt)
                + "（add_header 逐层替换不继承 —— 这些路径上浏览器拿不到 h3 宣告）"
            )
        else:
            ok("凡是自带 add_header 的 location 都宣告了 Alt-Svc")

    # --- API 响应一律不可缓存 ---
    #
    # **接了 CDN 之后这条从「讲究」变成「防事故」**：/api/* 的响应是
    # **按登录用户区分**的，一旦被边缘缓存住，用户 A 的凭证会被原样发给
    # 用户 B —— 页面照常显示，只是数据串了，**而且不报错**。
    #
    # proxy_hide_header 不能省：上游自己带了 Cache-Control 时，
    # add_header 会**再发一个**，浏览器按第一个解析，行为不可预期。
    #
    # **四段都要写**：/api/、^/api/mcp、^/api/scheduler/tick、打印页。
    # 少一段就是一条缝 —— 而且 nginx 的**正则 location 不继承**同级的
    # 普通前缀 location（nginx 只挑一个最优块执行），所以 /api/ 上写了
    # 不代表 ^/api/mcp 上也有。
    api_blocks = [(h, b) for h, b in all_location_blocks(lines) if "/api" in h]
    if not api_blocks:
        no("找不到任何 /api 的 location")
    else:
        bad = []
        for head, blk in api_blocks:
            if not any("proxy_hide_header Cache-Control" in l for l in blk):
                bad.append(head + "（没藏上游的 Cache-Control）")
            elif not any('add_header Cache-Control "no-store"' in l for l in blk):
                bad.append(head + "（没有 no-store）")
        if bad:
            no("API 段没有把响应标成不可缓存：" + "；".join(bad))
        else:
            ok(f"{len(api_blocks)} 个 API 段都标了 Cache-Control: no-store")

    # --- Cloudflare 在前时的真实客户端 IP 还原 ---
    #
    # 少了它，nginx 看到的对端是 CDN 的边缘节点 IP —— 本系统那层按 IP
    # 分桶的登录节流于是按边缘节点分桶（一个节点后面成千上万个用户共用
    # 一个桶），审计日志里的 IP 也全是 CDN 的地址。**且不报错。**
    if any("cwq-cloudflare-realip" in l for l in lines):
        ok("引入了 Cloudflare 真实 IP 还原配置（通配 include：没装文件就是干净的关闭状态）")
    else:
        no("没有引入 Cloudflare 真实 IP 还原配置 —— CDN 在前时登录节流会按边缘 IP 分桶")

    # --- ESA 在前时的真实客户端 IP（凭令牌还原）---
    #
    # ESA 在前面时，后端拿到的默认是**边缘节点 IP**。后果有两条，**都不报错**：
    #   * 登录节流按边缘节点分桶 —— 一个节点后面成千上万个用户共用一个桶，
    #     一个人在连着试错，同一节点上的其他人一起被挡（误伤）；
    #   * 审计日志里的 IP 全是节点地址 —— 出事查不到人。
    #
    # ESA 回源时会带一个头 ``ali-real-client-ip``（真实客户端 IP），但
    # **源站是公开的**：直连就能打进来，自己塞一个 ``ali-real-client-ip:
    # 6.6.6.6`` 源站照单全收（实测）。所以这个头必须**过令牌闸门**才认 ——
    # 裸信它 = 把限流的分桶钥匙交给任何能连到源站的人（每个请求换一个头
    # 就换一个桶，IP 维度限流等于不存在）。
    #
    # 每条断言盯一个具体的「出事方式」：
    #   1. 闸门存在（令牌在独立文件里）；
    #   2. 令牌文件用**通配** include —— 没装是「不信任」的关闭状态，
    #      而不是 nginx 起不来；
    #   3. 令牌**不能**硬编码在站点配置里（会进版本库）；
    #   4. 兜底必须是 $remote_addr —— 令牌对不上时按 TCP 对端计；
    #   5. 后端拿到的头必须走 $cwq_client_ip；
    #   6. map 必须定义在 log_format **之前**（见下面那条注释）。

    def _map_body(src: str, var: str) -> str | None:
        """取「目标是 var 的那个 map」的块体。

        注意 map 那一行有两个变量：``map <源> <目标> {``。按「行尾是目标」
        来认，别只找 ``map <目标>`` —— 那样一个都匹配不上。
        """
        src_lines = src.splitlines()
        for i, ln in enumerate(src_lines):
            s = ln.strip()
            if s.startswith("map ") and s.rstrip().endswith(var + " {"):
                body = []
                for l2 in src_lines[i + 1:]:
                    body.append(l2)
                    if l2.rstrip() == "}":
                        break
                return "\n".join(body)
        return None

    tok_body = _map_body(raw, "$cwq_esa_token_ok")
    if tok_body is not None and "cwq-esa-token" in tok_body:
        ok("ESA 真实 IP 过了令牌闸门（令牌在独立文件里，不进版本库）")
    else:
        no("ESA 真实 IP 没有令牌闸门 —— 源站 IP 是公开的，裸信 ali-real-client-ip "
           "等于把登录节流的分桶钥匙交给任何能连到源站的人")

    if tok_body and re.search(
            r"include\s+/etc/nginx/cwq-include/cwq-esa-token\*\.conf\s*;", tok_body):
        ok("令牌文件用通配 include（没装 = 不信任的关闭状态，而不是 nginx 起不来）")
    else:
        no("令牌文件的 include 不是通配 —— 文件没装时 nginx 会直接起不来，"
           "而「还没配令牌」本该是干净的关闭状态")

    if re.search(r'"[0-9a-f]{24,}"', raw):
        no("站点配置里出现了硬编码的长十六进制串 —— 令牌不该进版本库")
    else:
        ok("站点配置里没有硬编码令牌")

    cip_body = _map_body(raw, "$cwq_client_ip")
    if cip_body and re.search(r"default\s+\$remote_addr\s*;", cip_body):
        ok("令牌对不上时按 TCP 对端计（不会退化成「相信这个头」）")
    else:
        no("$cwq_client_ip 的兜底不是 $remote_addr —— 令牌对不上时会拿"
           "边缘节点地址、甚至伪造值当客户端 IP")

    if any("proxy_set_header" in l and "$http_ali_real_client_ip" in l for l in lines):
        no("ali-real-client-ip 被直接 proxy 给后端 —— 它只能经令牌闸门"
           "（$cwq_client_ip）使用")
    else:
        ok("ali-real-client-ip 只经令牌闸门使用")

    api_hdr = [(h, b) for h, b in all_location_blocks(lines) if "/api" in h]
    bad_hdr = []
    for head, blk in api_hdr:
        if not any(re.search(r"proxy_set_header\s+X-Real-IP\s+\$cwq_client_ip\s*;", l)
                   for l in blk):
            bad_hdr.append(head + "（X-Real-IP）")
        if not any(re.search(r"proxy_set_header\s+X-Forwarded-For\s+\$cwq_client_ip\s*;", l)
                   for l in blk):
            bad_hdr.append(head + "（X-Forwarded-For）")
    if bad_hdr:
        no("后端拿到的还是边缘节点 IP：" + "；".join(bad_hdr)
           + "（登录节流会按边缘节点分桶，审计日志查不到人）")
    elif api_hdr:
        ok(f"{len(api_hdr)} 个 API 段都把真实客户端 IP 交给后端")

    if 'cip="$cwq_client_ip"' in raw:
        ok("访问日志带 cip= 字段（真实客户端 IP 肉眼可见）")
    else:
        no("访问日志没有 cip= 字段 —— 真实 IP 还原了也看不出来")

    # $cwq_client_ip 必须**在本文件里有定义**。它被 log_format 与四段
    # proxy_set_header 同时用着 —— 变量不存在的话 nginx 直接
    # ``[emerg] unknown "$cwq_client_ip" variable`` 起不来。
    # 那时的现场最坑：reload 失败 → **旧进程继续服务** → 新配置一条都没
    # 上去，而访问日志一切正常（看日志根本看不出配置没生效）。
    # 令牌键很长（48 个十六进制字符），而 nginx 默认的 map_hash_bucket_size
    # 只有 64 字节 —— 一个桶装不下「键 + 值指针 + 链表指针」（≈ 键长 + 17）。
    # 装不下时 nginx 报 ``could not build map_hash, you should increase
    # map_hash_bucket_size: 64``，**整份配置加载失败**。生产上就是这个形态。
    m_bucket = re.search(r"map_hash_bucket_size\s+(\d+)\s*;", raw)
    if not m_bucket:
        no("没有设置 map_hash_bucket_size —— 令牌键（48 个十六进制字符）比默认的 "
           "64 字节长，nginx 会 could not build map_hash，整份配置加载失败")
    else:
        size = int(m_bucket.group(1))
        if size >= 96:
            ok(f"map_hash_bucket_size 调到 {size}（令牌键 48 字符，默认的 64 装不下）")
        else:
            no(f"map_hash_bucket_size 只有 {size} —— 令牌键 48 个字符，一个桶装不下"
               "「键 + 值指针 + 链表指针」，nginx 会 could not build map_hash 起不来")

    b_idx = raw.find("map_hash_bucket_size")
    m_idx = raw.find("map $http_x_cwq_origin_token")
    if b_idx != -1 and m_idx != -1 and b_idx < m_idx:
        ok("map_hash_bucket_size 写在令牌 map 之前")
    else:
        no("map_hash_bucket_size 必须写在令牌 map 之前 —— map 创建时才读这个值")

    if _map_body(raw, "$cwq_client_ip") is not None:
        ok("$cwq_client_ip 在本文件里有定义（变量缺失会让 nginx 起不来）")
    else:
        no("本文件里没有定义 $cwq_client_ip 的 map —— log_format 与四段 "
           "proxy_set_header 都用它，nginx 会 unknown variable 起不来")


# --------------------------------------------------------------------------
# 真的 nginx -t
# --------------------------------------------------------------------------

REWRITES = [
    ("/etc/nginx/cwq-include/", "{tmp}/include/"),
    ("/etc/nginx/ssl/cwq.crt", "{tmp}/ssl/cwq.crt"),
    ("/etc/nginx/ssl/cwq.key", "{tmp}/ssl/cwq.key"),
    # ESA 回源双向校验（mTLS）的**客户端信任锚**。
    #
    # 2026-10-24 接入 ESA 时加的两份配置（cwq.conf / acct.xins.live.conf）里都有
    # ``ssl_client_certificate /etc/nginx/ssl/esa/esa-client-ca.pem;`` ——
    # nginx -t 会**真的去读那个文件**，读不到就 emerg。而这条校验在 CI 里
    # 一直是红的：REWRITES 没给它映射位置，临时目录里也没有那个文件。
    #
    # 那是上一个提交（接入 ESA）留下的 CI 红灯，与 nginx 配置本身无关
    # （生产上那份文件是放好了的）。
    ("/etc/nginx/ssl/esa/esa-client-ca.pem", "{tmp}/ssl/esa/esa-client-ca.pem"),
    ("/www/server/panel/vhost/cert/acct.xins.live/fullchain.pem", "{tmp}/ssl/fullchain.pem"),
    ("/www/server/panel/vhost/cert/acct.xins.live/privkey.pem", "{tmp}/ssl/privkey.pem"),
    ("/www/wwwlogs/", "{tmp}/logs/"),
    ("/opt/cwq/frontend/dist", "{tmp}/dist"),
    ("/www/wwwroot/acct.xins.live", "{tmp}/wwwroot"),
]

MAIN_CONF = """worker_processes 1;
error_log {tmp}/logs/error.log;
pid {tmp}/nginx.pid;
events {{ worker_connections 64; }}
http {{
    access_log {tmp}/logs/access.log;
    client_body_temp_path {tmp}/tmp/client;
    proxy_temp_path {tmp}/tmp/proxy;
    fastcgi_temp_path {tmp}/tmp/fastcgi;
    uwsgi_temp_path {tmp}/tmp/uwsgi;
    scgi_temp_path {tmp}/tmp/scgi;
{body}
}}
"""

# 按文档建出来的限流两个文件（deploy/nginx/cwq.conf 文件头「限流开关」那一节）。
RL_ZONE = """# 限流的 http 级一半：定义 zone
limit_req_zone  $binary_remote_addr zone=cwq_api:10m rate=20r/s;
limit_conn_zone $binary_remote_addr zone=cwq_api_conn:10m;
"""
# ESA 真实 IP 的令牌文件（**形状照抄生产**，值是假的）。
#
# 令牌键刻意用 48 个十六进制字符 —— 这么长是有原因的：nginx 默认的
# map_hash_bucket_size 只有 64 字节，装不下「键 + 值指针 + 链表指针」
# （≈ 键长 + 17 字节），于是 ``could not build map_hash`` → 整份配置加载失败。
# 用一个短的假令牌就测不出这个坑（短键装得下）。
ESA_TOKEN = '"0123456789abcdef0123456789abcdef0123456789abcdef" 1;\n'

RL_API = """# 限流的 location 级一半：引用 zone
limit_req  zone=cwq_api burst=40 nodelay;
limit_conn cwq_api_conn 50;
"""


def make_cert(tmp: Path) -> bool:
    """造一份自签证书。nginx -t 会真的去读证书文件。"""
    ssl = tmp / "ssl"
    ssl.mkdir(parents=True, exist_ok=True)
    if shutil.which("openssl") is None:
        return False
    rc = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(ssl / "privkey.pem"), "-out", str(ssl / "fullchain.pem"),
         "-days", "1", "-subj", "/CN=localhost"],
        capture_output=True, text=True,
    )
    if rc.returncode != 0:
        return False
    # cwq.crt / cwq.key 是独立 nginx 形态用的那一份
    (ssl / "cwq.crt").write_bytes((ssl / "fullchain.pem").read_bytes())
    (ssl / "cwq.key").write_bytes((ssl / "privkey.pem").read_bytes())
    # ESA 回源双向校验的**客户端信任锚**（ssl_client_certificate）。
    #
    # 复用同一份自签证书就够了：nginx -t 只**解析**这个文件（读不出 PEM 就
    # emerg），不会拿它去验任何东西 —— 真正的验签发生在运行时，而那是生产机
    # 上放着真证书的事。
    esa = ssl / "esa"
    esa.mkdir(parents=True, exist_ok=True)
    (esa / "esa-client-ca.pem").write_bytes((ssl / "fullchain.pem").read_bytes())
    return True


def rewrite(text: str, tmp: Path) -> str:
    for old, new in REWRITES:
        text = text.replace(old, new.format(tmp=tmp))
    return text


def run_nginx_t(nginx: str, body: str, tmp: Path) -> tuple[bool, str]:
    conf = tmp / "nginx.conf"
    conf.write_text(MAIN_CONF.format(tmp=tmp, body=body), encoding="utf-8")
    proc = subprocess.run(
        [nginx, "-t", "-c", str(conf), "-p", str(tmp)],
        capture_output=True, text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip()


def find_nginx() -> str | None:
    """找 nginx 可执行文件。

    ``shutil.which`` 只看 PATH —— 而 nginx 在多数发行版里装在
    ``/usr/sbin``（root 才有），CI runner 的 PATH 里不一定有它。
    所以再试几个常见的绝对路径。
    """
    found = shutil.which("nginx")
    if found:
        return found
    for cand in ("/usr/sbin/nginx", "/usr/local/sbin/nginx", "/usr/local/bin/nginx",
                 "/opt/homebrew/bin/nginx", "/usr/bin/nginx"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


#: 本仓库的 nginx 配置要求 nginx >= 1.25.1。
#:
#: 原因是配置里写了 ``http2 on;`` —— 这个**独立指令**是 1.25.1 才引入的
#: （1.25.1 之前只有 ``listen 443 ssl http2`` 这种老写法，而老写法从 1.25.1
#: 起弃用、1.27 起被移除）。
#:
#: **为什么这条闸必须存在。** 2026-10-21 的 CI 就是被它绊倒的：CI runner
#: 装的是 Ubuntu 24.04 的 apt nginx（**1.24.0**），它不认 ``http2 on;`` →
#: ``nginx -t`` 报 ``unknown directive "http2"`` → 5 条断言全红。
#: 那 5 条红**与配置本身无关** —— 同一份配置在生产机（nginx 1.31.3）上
#: 加载得好好的。用一把版本不对的尺子量东西，量出来的结论当然是假的。
#:
#: 所以这里不是「跳过就完事」，而是**版本不够就明确报错**：报错的人
#: 知道该换 nginx，而不是去改配置 —— 配置是对的。
NGINX_MIN_VERSION = (1, 25, 1)


def nginx_version(nginx: str) -> tuple[int, ...] | None:
    """取 nginx 的版本号。

    ``nginx -v`` 把版本号打在 **stderr** 上（nginx 1.25 之前也是）。
    """
    proc = subprocess.run([nginx, "-v"], capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"nginx/(\d+(?:\.\d+)*)", out)
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def _fmt_ver(ver: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in ver)


def _is_root() -> bool:
    """当前是不是 root。

    **nginx -t 需要 root**，而且不是「建议」而是「必须」：它要打开默认
    错误日志（发行版装的位置通常是 ``/var/log/nginx/error.log``），还要
    **真的 bind() 一下** 配置里 ``listen 80`` / ``listen 443`` 这些特权端口。

    2026-10-21 的 CI 就栽在这儿：deploy job 以 ``runner`` 身份跑这一步，
    于是 nginx 报的是

        [alert] could not open error log file: open() "/var/log/nginx/error.log" failed (13: Permission denied)
        [emerg] bind() to 0.0.0.0:80 failed (13: Permission denied)

    —— 语法明明是 ok 的（nginx 自己都说 "syntax is ok"），却因为**权限**
    判了失败。那 4 条红指认的不是配置，是身份。所以这里先问「你是不是
    root」，不是就直接把原因说清楚，别让人去改配置。
    """
    return hasattr(os, "geteuid") and os.geteuid() == 0


def check_real_nginx(root: Path, *, require_nginx: bool = False) -> None:
    nginx = find_nginx()
    if nginx is None:
        if require_nginx:
            no("本机没装 nginx —— CI 必须真的跑一遍 nginx -t（装 nginx 再验）")
        else:
            print("== 真实 nginx -t：本机没装 nginx，跳过（结构校验仍然有效） ==")
            print("     CI 的 deploy job 会装 nginx 并真的跑一遍")
        return

    # ---- 版本闸：先确认这把尺子够长，再开始量 ----
    need = _fmt_ver(NGINX_MIN_VERSION)
    ver = nginx_version(nginx)
    if ver is None:
        no(f"读不出 nginx 的版本（{nginx} -v 没输出版本号）—— "
           "无法判断这份配置能不能被它加载")
        return
    if ver < NGINX_MIN_VERSION:
        no(f"装着的 nginx 是 {_fmt_ver(ver)}，低于 {need} —— "
           "这份配置里的 `http2 on;` 是 1.25.1 才引入的独立指令，"
           "旧版 nginx 报的是 unknown directive \"http2\"，"
           "**与配置本身无关**（同一份配置在生产机的 nginx 1.31.3 上加载正常）。"
           f"装一个 >= {need} 的 nginx 再验。")
        print("     （版本不够时 nginx -t 的结果说明不了配置对错，因此不跑）")
        return
    ok(f"nginx 版本 {_fmt_ver(ver)} >= {need}（这份配置里的 http2 on; 需要它）")

    # ---- 身份闸：nginx -t 必须以 root 跑 ----
    if not _is_root():
        no("当前不是 root —— nginx -t 要打开默认错误日志"
           "（/var/log/nginx/error.log）并真的 bind() 配置里的 80/443 端口；"
           "非 root 跑出来的失败**与配置无关**（nginx 自己会说 \"syntax is ok\"，"
           "挂在的是权限）。用 sudo 跑这一步。")
        print("     （身份不对时 nginx -t 的结果说明不了配置对错，因此不跑）")
        return

    print("== 真实 nginx -t ==")
    configs = [
        ("独立 nginx 形态", root / "deploy/nginx/cwq.conf"),
        ("宝塔形态", root / "deploy/nginx/acct.xins.live.conf"),
    ]
    for label, conf in configs:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            for d in ("include", "logs", "dist", "wwwroot", "tmp"):
                (tmp / d).mkdir(parents=True, exist_ok=True)
            if not make_cert(tmp):
                no(f"{label}：造不出自签证书（openssl 不可用），跳过 nginx -t")
                continue

            body = rewrite(conf.read_text(encoding="utf-8"), tmp)

            # **独立形态的前置条件**：host-map（80 端口跳转白名单）。
            #
            # cwq.conf 里写的是 ``$cwq_https_redirect``，而这个变量由
            # cwq-host-map.conf 的 ``map`` 生成 —— 那份配置里明写着
            # 「这份配置若整个缺失，nginx 会带着『$cwq_https_redirect 未定义』
            # 启动失败 —— **这是故意的**」。
            #
            # 所以 host-map 是**前置条件**，不是可选增强：先装上，再跑下面
            # 那两条限流用例。以前脚本把它放在最后才装，于是前两条是在验一个
            # **刻意损坏**的状态（2026-10-21 在 nginx 1.31.7 上暴露：
            # ``unknown "cwq_https_redirect" variable``，两条假红）。
            host_map = tmp / "include" / "cwq-host-map.conf"
            if conf.name == "cwq.conf":
                host_map.write_text(
                    (root / "deploy/nginx/cwq-host-map.conf").read_text(encoding="utf-8"),
                    encoding="utf-8")

            # (1) 限流**关闭**（默认状态）：两个限流文件都不存在。
            #     通配 include 在没有匹配文件时不报错 —— 这一条必须成立，
            #     否则「默认关闭」本身就是坏的。
            passed, out = run_nginx_t(nginx, body, tmp)
            if passed:
                ok(f"{label}：限流关闭时 nginx -t 通过")
            else:
                no(f"{label}：限流关闭时 nginx -t 失败\n{out}")

            # (2) 限流**按文档打开**：建出那两个文件，再 nginx -t。
            #     这一条就是 P2-3 的验收：以前宝塔形态在这里必然失败。
            (tmp / "include" / "cwq-rl-zone.conf").write_text(RL_ZONE, encoding="utf-8")
            (tmp / "include" / "cwq-rl-api.conf").write_text(RL_API, encoding="utf-8")
            passed, out = run_nginx_t(nginx, body, tmp)
            if passed:
                ok(f"{label}：按文档启用限流后 nginx -t 通过")
            else:
                no(f"{label}：按文档启用限流后 nginx -t 失败 —— "
                   "限流开关在这一形态下开不了\n{out}")

            # (3) **装了令牌文件**的真实形态（生产就是这个形态）。
            #     令牌键是 48 个十六进制字符，比 nginx 默认的
            #     map_hash_bucket_size（64 字节）长 —— 装不下时 nginx 报
            #     ``could not build map_hash``，**整份配置加载失败**，
            #     而失败现场很隐蔽：reload 失败 → 旧进程继续服务 →
            #     新配置一条都没上去，访问日志一切正常。
            #     所以这里要用**真的长**的假令牌，不能用短的糊弄过去。
            (tmp / "include" / "cwq-esa-token.conf").write_text(ESA_TOKEN, encoding="utf-8")
            passed, out = run_nginx_t(nginx, body, tmp)
            if passed:
                ok(f"{label}：装了令牌文件后 nginx -t 通过")
            else:
                no(f"{label}：装了令牌文件后 nginx -t 失败 —— 令牌键比 "
                   "map_hash_bucket_size 还长，配置根本加载不上去\n{out}")

            # (3) 独立形态：**拿掉 host-map，nginx 必须起不来**。
            #     这是配置刻意设计的 fail-closed —— 少了跳转白名单就宁可
            #     不服务，也不给开放重开口子。这条断言锁住这个设计：
            #     将来谁把「缺了也能起」改成默认（为了「更宽容」），这里会红。
            if conf.name == "cwq.conf":
                host_map.unlink()
                passed, out = run_nginx_t(nginx, body, tmp)
                if not passed:
                    ok(f"{label}：缺 host-map 时 nginx 拒绝启动（配置刻意 fail-closed）")
                else:
                    no(f"{label}：缺 host-map 时 nginx 竟然通过了 —— "
                       "少了 80 端口跳转白名单，等于开了开放重定向的口子")


def main() -> int:
    # ``--require-nginx``：**必须真的装上 nginx 并跑一遍 nginx -t**。
    # CI 的 deploy job 传这个开关 —— 没有它的话，CI 上万一 nginx 没装上，
    # 这道闸会「跳过 → 全绿 → 什么也没验」，变成一道假闸。
    require_nginx = "--require-nginx" in sys.argv[1:]
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 and \
        not sys.argv[1].startswith("--") else \
        Path(__file__).resolve().parents[2]
    for name in ("cwq.conf", "acct.xins.live.conf"):
        conf = root / "deploy/nginx" / name
        if not conf.is_file():
            no(f"找不到 {conf}")
            continue
        check_structure(conf)
    check_real_nginx(root, require_nginx=require_nginx)

    print()
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
