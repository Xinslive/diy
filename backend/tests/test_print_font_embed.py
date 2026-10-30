"""「锁定字体」：把字体本身也钉死，换台机器印出来一模一样。

**这件事是怎么来的。** 2026-10-30 用户发现「本地开发端看着很协调，生产的
空格太宽了」—— 同一份模板，换台机器印出来就不一样。根因是**字体是系统给的**：
模板里写「宋体」，Windows 上是 SimSun（半角空格 0.5em），macOS 上回退成
Songti SC（半角空格 0.2em），字宽、空格宽度、字形全都不同。

**分两层解决。**

* **版式**（位置 / 间距 / 换行）已经与字体无关了：位置用百分比、字号用 pt、
  行高固定、字距用全角空格 U+3000（advance 恒为 1em）。
* **字形**钉不死 —— 只能把字体随应用一起发布，让每台机器渲染的是同一份字节。

所以这里守着「锁定字体」这一层：模板里 ``font.embed`` 打开时，打印页改用
随应用发布的思源宋体（Noto Serif SC），并放开 CSP 的 ``font-src 'self'``。

**默认关。** 用户手里调好的版式（尤其「凭证纸专用」—— 用户明说「这是我好久
不容易调出来的」）一个字都不该被动。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.printing import fonts as pf
from app.printing.template import (
    print_csp_meta,
    render_voucher_sheet,
    validate_template,
)

FONT_DIR = Path(pf.FONT_DIR)
MANIFEST = FONT_DIR / "manifest.json"


# --------------------------------------------------------------------- 资源


def test_字体资源齐全且与清单一致():
    """**资源必须与清单逐字节对得上。**

    少一片、传坏了、被人手改了 —— 都要在这里红，而不是等到某个用户打印时
    才发现字库缺字（那时现场早没了：用户只会说「印出来少了个字」）。

    校验逻辑与 ``scripts/fetch_print_font.py --check`` 是同一套。
    """
    assert MANIFEST.exists(), "字体清单没了 —— 跑 scripts/fetch_print_font.py --download"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    slices = manifest["slices"]
    assert slices, "字体清单里一片都没有"

    for item in slices:
        p = FONT_DIR / item["name"]
        assert p.exists(), f"缺字体分片 {item['name']}"
        data = p.read_bytes()
        assert len(data) == item["bytes"], f"{item['name']} 字节数不对"
        assert hashlib.sha256(data).hexdigest() == item["sha256"], \
            f"{item['name']} 内容与清单对不上"

    # 授权书必须随字体一起分发（OFL 1.1 的硬要求）。
    assert (FONT_DIR / "OFL.txt").exists(), "OFL 授权书不见了 —— 字体不能再分发"


def test_样式表里的分片与磁盘上的对得上():
    """``@font-face`` 里引用的每个分片都得真的存在。

    少一个就是**运行时才发现**：页面上少一片字，浏览器悄悄回退到系统字体，
    而用户看到的纸还是「打印机里那份」—— 症状与没开锁定字体一模一样。
    """
    css = (FONT_DIR / "cwq-serif.css").read_text(encoding="utf-8")
    refs = re.findall(r"url\('/api/print/fonts/([^']+)'\)", css)
    assert len(refs) == 101, f"分片数不对：{len(refs)}"
    for name in refs:
        assert (FONT_DIR / name).exists(), f"样式表引用了不存在的分片 {name}"


def test_样式表里的字体族与代码里的一致():
    """CSS 里的族名与 ``EMBEDDED_FONTS`` 里登记的必须是同一个。

    对不上时 ``font-family: "CWQ Serif"`` 匹配不到任何 ``@font-face`` ——
    浏览器静默回退到系统字体，锁定字体形同没开，**而且不报错**。
    """
    css = (FONT_DIR / "cwq-serif.css").read_text(encoding="utf-8")
    family = pf.EMBEDDED_FONTS["cwq-serif"]["family"]
    assert f"font-family: '{family}'" in css, "样式表里的族名与登记的不一致"


# --------------------------------------------------------------------- 模板字段


def test_默认不开锁定字体():
    """**默认关。** 模板里没有这个字段时，打印页与以前一模一样。"""
    tpl = validate_template("VOUCHER", {})
    assert tpl["font"].get("embed") is False
    html = render_voucher_sheet(tpl, {}, [], 1, 1)
    assert "@font-face" not in html
    assert "cwq-serif.css" not in html


def test_打开后打印页用上内嵌字体():
    tpl = validate_template("VOUCHER", {"font": {"embed": True}})
    assert tpl["font"]["embed"] is True
    html = render_voucher_sheet(tpl, {}, [], 1, 1)
    assert 'href="/api/print/fonts/cwq-serif.css"' in html
    # 字体族必须排在最前面 —— 排在后面等于没开（系统字体先命中了）。
    assert 'font-family: "CWQ Serif", "宋体"' in html


def test_关掉时字体族还是老样子():
    """开了再关，必须回到原样 —— 开关不能只进不退。"""
    tpl = validate_template("VOUCHER", {"font": {"embed": False}})
    html = render_voucher_sheet(tpl, {}, [], 1, 1)
    assert 'font-family: "宋体", "SimSun", "Songti SC", serif;' in html
    assert "cwq-serif.css" not in html


def test_字符串假值不许当成开():
    """``"false"`` 必须是 False。

    ``bool("false")`` 是 True —— 那就成了「明明关了却开着」，用户看着界面上的
    关，纸上却是另一副字。
    """
    for raw in ("false", "0", "", "no"):
        tpl = validate_template("VOUCHER", {"font": {"embed": raw}})
        assert tpl["font"]["embed"] is False, f"{raw!r} 被当成了开"
    for raw in ("true", "1", True, 1):
        tpl = validate_template("VOUCHER", {"font": {"embed": raw}})
        assert tpl["font"]["embed"] is True, f"{raw!r} 被当成了关"


def test_认不出来的字体键不许留在模板里():
    """``embed_font`` 认不出来就当没开 —— 绝不据此拼一个来路不明的路径。"""
    tpl = validate_template("VOUCHER", {"font": {"embed": True, "embed_font": "../evil"}})
    assert tpl["font"]["embed_font"] == pf.DEFAULT_EMBEDDED
    html = render_voucher_sheet(tpl, {}, [], 1, 1)
    assert "/api/print/fonts/" in html
    assert "evil" not in html


def test_关掉时不留多余的字体键():
    tpl = validate_template("VOUCHER", {"font": {"embed": False, "embed_font": "cwq-serif"}})
    assert "embed_font" not in tpl["font"]


# --------------------------------------------------------------------- CSP


def test_字体内嵌时CSP放开字体():
    """字体文件必须从本站取 —— ``font-src 'none'`` 拦得住。"""
    csp = print_csp_meta({"font": {"embed": True}})
    assert "font-src 'self'" in csp
    assert "style-src 'unsafe-inline' 'self'" in csp
    # 脚本永远不放开：打印页会被塞进 iframe，模板又是用户可改的数据。
    assert "script-src 'none'" in csp


def test_没开锁定字体时CSP一点没松():
    """不开锁字体时 CSP 与从前**逐字相同** —— 这一层不该为此付出任何代价。"""
    csp = print_csp_meta({"font": {"embed": False}})
    assert "font-src 'none'" in csp
    assert "style-src 'unsafe-inline';" in csp
    assert "connect-src 'none'" in csp
    assert "form-action 'none'" in csp
    assert "base-uri 'none'" in csp


# --------------------------------------------------------------------- 字体路由


@pytest.fixture(scope="module")
def font_client():
    with TestClient(fastapi_app, raise_server_exceptions=False) as c:
        yield c


def test_字体分片发得出去(font_client):
    r = font_client.get("/api/print/fonts/nsc-000.woff2")
    assert r.status_code == 200
    assert r.headers["content-type"] == "font/woff2"
    assert len(r.content) > 0


def test_字体样式表发得出去(font_client):
    r = font_client.get("/api/print/fonts/cwq-serif.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "@font-face" in r.text


def test_路径穿越进不来(font_client):
    """白名单而不是「过滤非法字符」。"""
    for bad in ("..%2Fsecret", "nsc-000.woff2%00.png", "%2e%2e%2fmanifest.json"):
        r = font_client.get(f"/api/print/fonts/{bad}")
        assert r.status_code == 404, f"{bad} 竟然没被挡住"


def test_不存在的分片是404(font_client):
    assert font_client.get("/api/print/fonts/nsc-999.woff2").status_code == 404
    assert font_client.get("/api/print/fonts/evil.css").status_code == 404
