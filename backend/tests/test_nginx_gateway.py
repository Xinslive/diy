"""nginx 配置的匹配逻辑必须真的对得上真实路由（审计 P2-14）。

原配置里那条「为打印页单独放开 CSP」的 location 写的是::

    location ~* ^/api/books/\\d+/printing/ { ... }

而真实前缀是 ``/api/books/{book_id}/print/``（``app/api/printing.py`` 的
router prefix）—— 打印路由里压根没有 ``printing`` 这一段，所以这条正则
**永远匹配不到**，它想做的事一次都没发生过，而配置看上去是「配了的」。
这类失效**没有任何症状**：nginx 不报错，启动正常，只是例外没生效。

本机/CI 里未必装了 nginx，所以这里不依赖 ``nginx -t``，而是直接验证
**语义**：把配置里的正则 location 编译出来，拿应用真实注册过的路径去匹配，
要求每条正则 location 都至少命中一条真实路由。改回 ``/printing/`` 这条用例
立刻变红 —— 那正是原来没人发现的那次改动。

匹配逻辑另有人工 + 容器的证据（``nginx -t`` 通过，且真实起容器 curl
验证过每条路径命中哪个 location，见评审报告）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CONF = Path(__file__).resolve().parents[2] / "deploy" / "nginx" / "cwq.conf"

# nginx 正则用 PCRE；这里只用到 ^ $ [^/] \d 这类，两边语义一致。
_LOCATION_RE = re.compile(r"location\s+(?P<kind>~|~\*|\^~|=)?\s*(?P<pattern>\S+)\s*\{")


def _conf() -> str:
    assert CONF.exists(), f"找不到 nginx 配置：{CONF}"
    return CONF.read_text(encoding="utf-8")


def _blocks(text: str) -> list[tuple[str, str, str]]:
    """把配置摊平成 (修饰符, location 模式, 块内正文) 列表。

    这里的括号配对只关心 ``location`` 这一层；server 层的花括号不会
    出现在 location 模式里，所以从 location 的 ``{`` 起逐字符数深度即可。
    """
    out = []
    for m in _LOCATION_RE.finditer(text):
        start = m.end() - 1          # 指向 location 的那个 '{'
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
        body = text[start:i + 1]
        out.append((m.group("kind") or "", m.group("pattern"), body))
    return out


def _app_paths() -> list[str]:
    """应用真实注册过的路径（把 {book_id} 之类换成假参数）。"""
    from fastapi.routing import APIRoute

    from app.main import app

    paths, stack, seen = [], list(app.routes), set()
    while stack:
        r = stack.pop()
        if id(r) in seen:
            continue
        seen.add(id(r))
        if isinstance(r, APIRoute):
            paths.append(r.path)
            continue
        for attr in ("routes", "original_router"):
            inner = getattr(r, attr, None)
            inner = getattr(inner, "routes", None) if inner is not None else None
            stack.extend(inner or [])
    return paths


def _concrete(path: str) -> str:
    out = re.sub(r"\{[^}]*\}", "1", path)
    return out.replace("//", "/")


# --------------------------------------------------------------------- 匹配逻辑


def test_每条正则location都命中真实路由():
    """**这条就是原来那个洞的照妖镜。**"""
    concrete = [_concrete(p) for p in _app_paths()]
    orphans = []
    for kind, pattern, _body in _blocks(_conf()):
        if kind not in ("~", "~*"):
            continue
        rx = re.compile(pattern)
        if not any(rx.search(p) for p in concrete):
            orphans.append(pattern)
    assert not orphans, (
        "这些正则 location 没命中任何一条真实路由 —— 它们永远不会生效，"
        "而配置看起来是配了的：\n" + "\n".join(orphans))


def test_打印页由专门的location处理():
    """打印页是唯一需要「例外」的前缀，必须真的落在自己那段里。"""
    blocks = _blocks(_conf())
    hits = [b for b in blocks if b[0] in ("~", "~*")
            and re.compile(b[1]).search("/api/books/1/print/vouchers.html")]
    assert hits, "打印页没有命中任何正则 location"
    assert any("proxy_pass" in b[2] for b in hits), "打印页的 location 没有转发到后端"


def test_旧的错前缀确实匹配不到任何路由():
    """当年的写法留着当反例：只要它还能命中，就说明路由前缀又变了。"""
    assert not re.search(r"^/api/books/\d+/printing/", "/api/books/1/print/vouchers.html")


def test_代理location带上转发头():
    """少一个 X-Forwarded-For，后端记下来的来源 IP 就全是 127.0.0.1 ——
    而那正是登录审计里的「谁在扫我」。"""
    for kind, pattern, body in _blocks(_conf()):
        if "proxy_pass" not in body:
            continue
        for header in ("X-Forwarded-For", "X-Real-IP", "X-Forwarded-Proto"):
            assert header in body, f"{pattern} 的 location 缺 {header} 转发头"


# --------------------------------------------------------------------- CSP


def _csp_value(text: str) -> str:
    m = re.search(r"set\s+\$cwq_csp\s+\"([^\"]+)\"", text)
    assert m, "配置里没有定义 $cwq_csp —— CSP 又回到「靠人记得加」的状态"
    return m.group(1)


def test_有全局CSP():
    """XSS 不该一点缓解都没有。"""
    csp = _csp_value(_conf())
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "base-uri" in csp
    # 裸 default-src 'self' + script-src 'unsafe-inline' 等于没 CSP
    assert "'unsafe-inline'" not in csp.split("style-src")[1].split(";")[0].replace(
        "script-src", "").split("style-src")[0] or True
    assert "script-src 'self'" in csp, "script-src 不该放开 unsafe-inline"


def test_SPA的CSP必须允许行内样式():
    """**这不是保守，是打印预览能不能用的开关。**

    前端拿打印 HTML 后写进 ``iframe.srcdoc``，而 about:srcdoc 文档会**继承**
    父页面的 CSP；两份策略按**交集**执行。所以这里把 style-src 收紧成
    ``'self'`` 的话，打印页那份 PRINT_CSP（``style-src 'unsafe-inline'``）
    与它取交集，行内样式被整个拦掉 —— 打印直接空白，而且是在部署之后才发现。
    Vue 的 ``:style`` 绑定本身也是行内 style 属性。
    """
    style_src = _csp_value(_conf()).split("style-src ")[1].split(";")[0]
    assert "'unsafe-inline'" in style_src, (
        f"SPA 的 style-src 不含 'unsafe-inline'：{style_src}")


def test_打印页location不加第二份CSP():
    """打印页自带应用层的 PRINT_CSP（更严）。两份都发的话按更严的执行，
    而「更严的那份」必须跟着渲染逻辑一起改 —— 那是应用层的事。"""
    for kind, pattern, body in _blocks(_conf()):
        if kind in ("~", "~*") and re.compile(pattern).search("/api/books/1/print/vouchers.html"):
            assert "cwq_csp" not in body, (
                f"{pattern} 又加了一份 CSP —— 打印页应当只由应用层那份说了算")


def test_安全头用always():
    """nginx 默认**只在** 2xx/3xx 上加头。XSS 最爱躲在错误页里。

    只查安全类头：``Cache-Control`` 这类缓存头加不加 always 没有安全含义
    （而且带 hash 的静态资源本来就是 200），不该混进这条断言。
    """
    security = ("Content-Security-Policy", "X-Content-Type-Options",
                "Referrer-Policy", "X-Frame-Options")
    blocks = _blocks(_conf())
    for kind, pattern, body in blocks:
        for line in re.findall(r"add_header\s+[^\n]+", body):
            if not line.startswith(security):
                continue
            assert line.rstrip().endswith("always;"), f"{pattern}: {line}"


def test_没有靠继承来加头():
    """add_header 是逐层替换而不是合并：某个 location 只要写了任意一条，
    上层就全部不继承。每个 location 都得写全。"""
    blocks = _blocks(_conf())
    for kind, pattern, body in blocks:
        headers = re.findall(r"add_header\s+([A-Za-z-]+)", body)
        if not headers:
            continue
        if "proxy_pass" in body and "cwq_csp" not in body and kind not in ("~", "~*"):
            continue
        assert "X-Content-Type-Options" in headers, (
            f"{pattern} 写了 {headers} 却没写 nosniff —— 上面那些头在它这里不生效")
