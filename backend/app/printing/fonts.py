"""随应用一起发布的打印字体（自托管 webfont）。

**为什么要有它。** 打印页要的是**所见即所得**：在 A 机器上设计、在 B 机器上
印，纸上的样子必须一模一样。可字体是**系统给的** —— 模板里的「宋体」在
Windows 上是 SimSun（半角空格 0.5em），在 macOS 上回退成 Songti SC
（半角空格 0.2em）。字宽不同、空格宽度不同、字形也不同，同一份模板换个环境
印出来就变样。

2026-10-30 用户就撞上了这个：「本地开发端看着很协调，生产的空格太宽了」。

**版式**能靠单位钉死（位置用百分比、字号用 pt、行高固定、字距用全角空格
U+3000 —— 全角空格的 advance 恒为 1em，与字体无关）；**字形**钉不死：只能
把字体本身一起带上，让每台机器渲染的是同一份字节。

所以这里放一份**思源宋体（Noto Serif SC）**，SIL OFL 1.1 授权，可商用、可
再分发（授权书随字体一起放在资源目录里，见 ``OFL.txt``）。

**怎么分片。** 完整字体有几 MB，一页凭证只用得到几百个字。做法与 Google
Fonts 相同：按 Unicode 区段切成 101 片，每片配一条 ``unicode-range``，
浏览器只下载这一页真正用到的那几片。**101 片是同一个可变字体**的 101 个片段
（字重由 wght 轴给），一共 5.7 MB。

**怎么用。** 模板的 ``font.embed`` 打开时，渲染层把 ``@font-face`` 规则内联进
打印页的 ``<style>``，并把字体族换成这一份。**默认关闭** —— 用户手里调好的
版式（尤其「凭证纸专用」）一个字都不该被动。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: 字体资源目录（随应用一起打包进镜像；Dockerfile 里 COPY app）。
FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

#: 内嵌字体登记处。键是模板里 ``font.embed_font`` 的取值。
#:
#: 现在只有一份（思源宋体）。**登记成表而不是写死**，是为了再加字体时
#: 不用改渲染层 —— 换字体只是多一条登记。
EMBEDDED_FONTS: dict[str, dict[str, str]] = {
    "cwq-serif": {
        # CSS 里对外的字体族名（font-family 用它）。
        "family": "CWQ Serif",
        # 界面上给人看的名字。
        "label": "思源宋体",
        # @font-face 规则所在的 CSS 文件。
        "css": "cwq-serif.css",
    },
}

#: ``font.embed`` 为真但没指定 ``embed_font`` 时用哪一份。
DEFAULT_EMBEDDED = "cwq-serif"

#: 字体文件在磁盘上的名字（不含后缀）。
FONT_FILE_PREFIX = "nsc"


def embedded_key(tpl: dict | None) -> str | None:
    """这份模板要不要用随应用发布的字体？用哪一份？

    返回 ``None`` 表示不用（用系统字体）。**只认登记在册的键** ——
    模板是用户可改的数据，键认不出来就当没开，绝不因此把字体路径拼进 CSS。
    """
    font = (tpl or {}).get("font")
    if not isinstance(font, dict):
        return None
    if not font.get("embed"):
        return None
    key = str(font.get("embed_font") or DEFAULT_EMBEDDED).strip()
    return key if key in EMBEDDED_FONTS else None


def embedded_family(tpl: dict | None) -> str:
    """要用内嵌字体时的 CSS 字体族名；不用则空串。"""
    key = embedded_key(tpl)
    return EMBEDDED_FONTS[key]["family"] if key else ""


#: 字体资源在站点上的路径前缀。**根相对**：页面在哪儿，字体就从哪儿取 ——
#: 预览 iframe（srcdoc）、打印窗口（about:blank）、直接打开打印页链接，
#: 三种情形都落在同一个源上。
FONT_URL_PREFIX = "/api/print/fonts"


@lru_cache(maxsize=4)
def _font_face_css(key: str) -> str:
    path = FONT_DIR / EMBEDDED_FONTS[key]["css"]
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        # 资源缺失（镜像没带上 / 被人删了）不该让整页打印崩掉 ——
        # 没有 @font-face 规则，浏览器就回退到系统字体，跟以前一样。
        return ""


def font_face_css(key: str) -> str:
    """那份 ``@font-face`` 规则的原文（由字体路由发给浏览器）。"""
    return _font_face_css(key)


def font_stylesheet_link(tpl: dict | None) -> str:
    """打印页 ``<head>`` 里那条 ``<link>``；不用内嵌字体则空串。

    **用 ``<link>`` 而不是把规则内联进 ``<style>``**：101 条 ``@font-face``
    带上一长串 ``unicode-range``，内联的话是 **100 KB 起步** —— 预览页
    （只有 3 张凭证）会从 7 KB 涨到 110 KB，而每次改一个坐标都要重取一次。
    ``<link>`` 让浏览器自己缓存：同一份 CSS 只下一次，之后每次预览都命中缓存。

    代价是打印页的 CSP 要放开 ``style-src 'self'``（见
    ``printing/template.py::print_csp_meta``）。这一条开的是「同源样式表」，
    不是脚本（``script-src 'none'`` 不动）—— 同源样式表做不了 XSS。
    """
    key = embedded_key(tpl)
    if not key:
        return ""
    name = EMBEDDED_FONTS[key]["css"]
    return f'<link rel="stylesheet" href="{FONT_URL_PREFIX}/{name}">'
