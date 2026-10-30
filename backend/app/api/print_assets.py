"""打印用的静态资源（随应用发布的字体）。

**为什么字体要由后端发，而不是放进前端 ``public/``。**

1. **打印页是后端生成的**：``@font-face`` 的 ``src`` 是后端拼出来的，
   放在前端目录里就得让后端知道前端的部署路径 —— 动静分离的部署里那是
   另一套地址，发布脚本还得改写它。
2. **后端知道自己的对外地址**：``/api/print/fonts/xxx`` 是**根相对**路径，
   在页面里按当前文档的源解析 —— 预览 iframe（``srcdoc``）、打印窗口
   （``document.write`` 写进 ``about:blank``）、直接打开打印页链接，
   三种情形都落在同一个源上。
3. **Content-Type 与缓存策略由应用说了算**：``font/woff2`` 与
   ``text/css`` 不必去动 nginx 的 mime.types。

字体本身见 ``app/printing/fonts.py``（为什么要有它、怎么分片）。

**URL 里为什么带版本号。** 字体是静态资源，要能放心地 ``immutable`` 缓存
一年，前提是「同一个 URL 的字节永远不变」—— 而换一版权体时分片还是
``nsc-000.woff2`` 这个名字。版本号（分片内容的哈希）进了 URL，版本一变
URL 就变，缓存才敢一直留着。细节见 ``app/printing/fonts.py::font_version``。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from app.printing import fonts as pf
from app.printing.fonts import (EMBEDDED_FONTS, FONT_DIR, FONT_FILE_PREFIX,
                                 font_face_css)

router = APIRouter(prefix="/api/print", tags=["打印资源"])

#: 字体文件名的唯一合法形状。**白名单而不是「过滤非法字符」** ——
#: 路径穿越（``..``、``/``、绝对路径）根本进不来。
_FONT_NAME_RE = re.compile(rf"^{FONT_FILE_PREFIX}-\d{{3}}\.woff2$")

#: 登记在册的样式表文件名（键是去掉 .css 的名字）。
_CSS_NAMES = {spec["css"][:-4] for spec in EMBEDDED_FONTS.values()}

#: 静态资源的缓存头。
#:
#: **可以 immutable 缓存一年** —— URL 里带着资源版本号
#: （``/api/print/fonts/v/<版本>/…``），版本变了 URL 就变，
#: 所以「同一个 URL 的字节永远不变」成立。
#:
#: 这条头**必须真的发到浏览器**：生产 nginx 的 ``location /api/`` 把
#: Cache-Control 藏掉、改成 ``no-store``（那是为「按登录用户区分的接口」
#: 防串数据），字体不是那种东西。为此 nginx 单独给字体开了一条 location
#: （见 ``deploy/nginx/*.conf``），别把它并回 ``/api/``。
_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


def _version_ok(version: str) -> bool:
    """这个版本号是不是当前随包发布的字体版本。

    **对不上就 404** —— 不发「版本号随便给、内容永远是当前这份」的响应：
    那会让「同一个 URL 的字节永远不变」这个前提失效（缓存里可能混着
    新旧两份），而混起来的字形比回退到系统字体更难查。
    """
    return any(version == pf.font_version(k) for k in EMBEDDED_FONTS)


@router.get(f"/fonts/{pf.FONT_VERSION_DIR}/{{version}}/{{name}}")
def print_font(version: str, name: str):
    """取一份打印资源：字体分片（``.woff2``）或那份 ``@font-face`` 规则（``.css``）。

    **不需要鉴权**：字体不是用户数据，是随应用发布的静态资源；而且打印页
    本身在 iframe 里加载它时未必带得上 Authorization（``srcdoc`` /
    ``document.write`` 那条链路就是先取回源码再塞进去的）。

    **一个路由而不是两个**：FastAPI 的路径参数是贪婪匹配，
    ``/fonts/{name}`` 会把 ``cwq-serif.css`` 整个吞掉 —— 分成两个路由时
    先注册的那个永远先命中，``.css`` 那个就永远轮不到（实测 404）。
    """
    if not _version_ok(version):
        raise HTTPException(404, "字体资源不存在")

    if name.endswith(".css"):
        stem = name[:-4]
        if stem not in _CSS_NAMES:
            raise HTTPException(404, "字体样式表不存在")
        css = font_face_css(stem)
        if not css:
            raise HTTPException(404, "字体样式表不存在")
        return Response(
            content=css,
            media_type="text/css; charset=utf-8",
            headers=dict(_CACHE),
        )

    if not _FONT_NAME_RE.match(name or ""):
        raise HTTPException(404, "字体文件不存在")
    path = FONT_DIR / name
    if not path.is_file():
        raise HTTPException(404, "字体文件不存在")
    return FileResponse(
        path,
        media_type="font/woff2",
        headers=dict(_CACHE),
    )
