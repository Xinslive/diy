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
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from app.printing.fonts import (EMBEDDED_FONTS, FONT_DIR, FONT_FILE_PREFIX,
                                 font_face_css)

router = APIRouter(prefix="/api/print", tags=["打印资源"])

#: 字体文件名的唯一合法形状。**白名单而不是「过滤非法字符」** ——
#: 路径穿越（``..``、``/``、绝对路径）根本进不来。
_FONT_NAME_RE = re.compile(rf"^{FONT_FILE_PREFIX}-\d{{3}}\.woff2$")

#: 登记在册的样式表文件名（键是去掉 .css 的名字）。
_CSS_NAMES = {spec["css"][:-4] for spec in EMBEDDED_FONTS.values()}


@router.get("/fonts/{name}")
def print_font(name: str):
    """取一份打印资源：字体分片（``.woff2``）或那份 ``@font-face`` 规则（``.css``）。

    **不需要鉴权**：字体不是用户数据，是随应用发布的静态资源；而且打印页
    本身在 iframe 里加载它时未必带得上 Authorization（``srcdoc`` /
    ``document.write`` 那条链路就是先取回源码再塞进去的）。

    **一个路由而不是两个**：FastAPI 的路径参数是贪婪匹配，
    ``/fonts/{name}`` 会把 ``cwq-serif.css`` 整个吞掉 —— 分成两个路由时
    先注册的那个永远先命中，``.css`` 那个就永远轮不到（实测 404）。
    """
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
            headers={"Cache-Control": "public, max-age=86400"},
        )

    if not _FONT_NAME_RE.match(name or ""):
        raise HTTPException(404, "字体文件不存在")
    path = FONT_DIR / name
    if not path.is_file():
        raise HTTPException(404, "字体文件不存在")
    return FileResponse(
        path,
        media_type="font/woff2",
        headers={
            # 文件名带版本（分片序号），内容变了就是另一个文件 ——
            # 但这里不写 immutable：真要换字体版本时，分片序号可能不变而
            # 字节变了，一年缓存会把旧字形一直发下去。
            "Cache-Control": "public, max-age=86400",
        },
    )
