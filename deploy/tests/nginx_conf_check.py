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


# --------------------------------------------------------------------------
# 真的 nginx -t
# --------------------------------------------------------------------------

REWRITES = [
    ("/etc/nginx/cwq-include/", "{tmp}/include/"),
    ("/etc/nginx/ssl/cwq.crt", "{tmp}/ssl/cwq.crt"),
    ("/etc/nginx/ssl/cwq.key", "{tmp}/ssl/cwq.key"),
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
