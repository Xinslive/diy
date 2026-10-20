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


def check_real_nginx(root: Path) -> None:
    nginx = find_nginx()
    if nginx is None:
        print("== 真实 nginx -t：本机没装 nginx，跳过（结构校验仍然有效） ==")
        print("     CI 的 deploy job 会装 nginx 并真的跑一遍")
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

            # (3) 独立 nginx 形态还要 host-map（缺了它 nginx 故意起不来）。
            if conf.name == "cwq.conf":
                (tmp / "include" / "cwq-host-map.conf").write_text(
                    (root / "deploy/nginx/cwq-host-map.conf").read_text(encoding="utf-8"),
                    encoding="utf-8")
                passed, out = run_nginx_t(nginx, body, tmp)
                if passed:
                    ok(f"{label}：装上 host-map 后 nginx -t 通过")
                else:
                    no(f"{label}：装上 host-map 后 nginx -t 失败\n{out}")


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
        Path(__file__).resolve().parents[2]
    for name in ("cwq.conf", "acct.xins.live.conf"):
        conf = root / "deploy/nginx" / name
        if not conf.is_file():
            no(f"找不到 {conf}")
            continue
        check_structure(conf)
    check_real_nginx(root)

    print()
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
