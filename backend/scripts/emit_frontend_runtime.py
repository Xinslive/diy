#!/usr/bin/env python
"""装配前端的**运行时配置**：动静分离的发布侧那一半。

    # 同源部署（默认值，什么都不改）
    python scripts/emit_frontend_runtime.py --dist /opt/cwq/frontend/dist

    # 静态资源走 CDN、API 留在源站
    python scripts/emit_frontend_runtime.py --dist ... \\
        --asset-base https://cdn.example.com/ --origin https://acct.example.com

    # 只看会做什么，不写任何文件
    python scripts/emit_frontend_runtime.py --dist ... --dry-run

--------------------------------------------------------------------------------
它做两件事
--------------------------------------------------------------------------------
1. 写 ``dist/runtime-config.js``（``window.__CWQ_RUNTIME__``）：
   **API 在哪**、**静态资源在哪**。这是前端唯一需要「运行时才知道」的两个值。

2. 改写 ``dist/index.html``：把资源地址指向静态资源基址，把配置脚本的地址
   指向源站。

--------------------------------------------------------------------------------
为什么改写的是 index.html，而不是「构建时就知道 CDN 域名」
--------------------------------------------------------------------------------
CI 每个 commit 只构建**一份**产物，而产物要能装到不同环境（生产 / 预发 /
本地）。把 CDN 域名烤进构建产物，等于「换环境要重新构建」—— 而发布流程的
约定是「取产物 → 装上去」，不重新构建。

所以：**构建产物保持中立（资源地址是 ``/assets/...``），发布时改写**。改写
只动一个文件（index.html），且是**幂等**的 —— 已经改过的文件再改一次不会
变成 ``https://cdn.x/https://cdn.x/assets/...``（见下面 rewrite_index_html）。

--------------------------------------------------------------------------------
为什么配置脚本的地址要指向**源站**而不是跟着页面走
--------------------------------------------------------------------------------
页面可能托管在 CDN 上，而 runtime-config.js 是**每台部署都不同**的东西
（API 地址、CDN 域名、版本号）。它若跟着页面走，就得靠 CDN 把它缓存成
「永不失效」—— 而那正是「改了配置不生效」的来源。指向源站则是：配置永远
从源站取，源站那一跳虽然每次页面加载都发生，但那个文件只有几百字节，
且必须 no-cache。

**这是「配置的唯一真源」原则**：配置在哪台机器上生效，就从那台机器取。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# dist 里由 Vite 产出的资源引用。**只认绝对路径形式**（构建产物就是这样），
# 相对路径（./assets/...）不匹配 —— 那说明产物不是本流程生成的，跳过即可。
_ASSET_REF = re.compile(r'((?:src|href)=")(/assets/[^"]*)(")')

# 运行时配置脚本的引用。它在 index.html 里是普通脚本（同步执行），
# 必须排在主模块之前。
_CONFIG_REF = re.compile(r'(<script[^>]*\ssrc=")(/runtime-config\.js)(")')


def normalize_base(value: str, fallback: str) -> str:
    """归一化基址：去掉结尾斜杠（API 用）/ 补上结尾斜杠（目录前缀用）。

    两个基址的形态要求不同，所以分成两个函数而不是一个：

    * ``apiBase`` 后面接的是**路径**（``/api/books``），结尾不能有斜杠，
      否则拼出 ``/api//books``；
    * ``assetBase`` 后面接的是**文件名**（``assets/index-x.js``），结尾
      必须有斜杠，否则拼出 ``https://cdn.xassets/index-x.js``。
    """
    v = (value or "").strip()
    if not v:
        return fallback
    return v.rstrip("/") or "/"


def normalize_dir_base(value: str, fallback: str) -> str:
    v = (value or "").strip()
    if not v:
        return fallback
    return v if v.endswith("/") else v + "/"


def render_runtime_config(api_base: str, asset_base: str, version: str) -> str:
    """生成 runtime-config.js 的内容。

    **刻意写成一行 JSON 字面量**：这个文件每次页面加载都要取一次，
    注释在这条路径上不产生任何价值（值只有两个）。
    """
    payload = {"apiBase": api_base, "assetBase": asset_base}
    if version:
        payload["version"] = version
    return "window.__CWQ_RUNTIME__=%s;\n" % json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def rewrite_index_html(html: str, asset_base: str, origin: str) -> tuple[str, list[str]]:
    """改写 index.html 里的资源地址。返回 (新内容, 改写说明)。

    幂等：改写后的地址不再以 ``/assets/`` 开头（绝对基址时）或不再匹配
    ``/assets/`` 前缀（路径前缀时），所以再跑一次不会二次拼接。

    路径前缀基址（``/static/``）的幂等靠「先剥离已有的基址前缀再拼」保证。
    """
    notes: list[str] = []

    def _asset(m: re.Match) -> str:
        path = m.group(2)
        notes.append(path)
        return "%s%s%s" % (m.group(1), asset_base + path.lstrip("/"), m.group(3))

    new = _ASSET_REF.sub(_asset, html)

    if origin:
        origin = origin.rstrip("/")

        def _config(m: re.Match) -> str:
            notes.append("/runtime-config.js → " + origin + "/runtime-config.js")
            return "%s%s/runtime-config.js%s" % (m.group(1), origin, m.group(3))

        new = _CONFIG_REF.sub(_config, new)

    return new, notes


def find_leftover(html: str, asset_base: str) -> list[str]:
    """找出**没有指向静态基址**的资源引用。

    规则是「每一条资源地址都必须以基址开头」：

    * 基址是 ``/``（同源）→ ``/assets/x.js`` 本来就以 ``/`` 开头 → 全部合法；
    * 基址是 ``/static/`` → 必须是 ``/static/assets/x.js``；
    * 基址是 ``https://cdn.example.com/`` → 必须是
      ``https://cdn.example.com/assets/x.js``。

    绝对基址时若还剩 ``/assets/...`` 这种相对地址，浏览器会把它解析到
    **页面所在的源**去取 —— 于是「静态资源走 CDN」这件事只做了一半：
    页面从 CDN 取 JS，CSS 却从源站取 —— 而源站正是带宽紧张的那台机。
    这个错误不报任何错，只是流量悄悄走了老路。

    **同源部署（基址 ``/``）不会误报**：那正是「资源地址本来就长这样」。
    """
    leftover = []
    for m in _ASSET_REF.finditer(html):
        ref = m.group(2)
        if not ref.startswith(asset_base):
            leftover.append(ref)
    return sorted(set(leftover))


def missing_assets(dist: Path, html: str, asset_base: str) -> list[str]:
    """列出 HTML 引用了、但 dist 里不存在的资源文件。

    参照的路径要**把基址前缀剥掉** —— 改写之后地址可能是
    ``https://cdn.example.com/assets/x.js``，直接拿它去找文件是找不到的
    （那会误报成「全部资源都缺」）。

    **只警告不拦发布**：资源可能被放在 CDN 源站（OSS）而不在 dist 里，
    那种情况下本地当然找不到。所以它是「看一眼」的信号，不是闸门。
    """
    missing: list[str] = []
    for m in _ASSET_REF.finditer(html):
        ref = m.group(2)
        if ref.startswith(asset_base):
            ref = ref[len(asset_base):]
        rel = ref.lstrip("/")
        if not rel:
            continue
        if not (dist / rel).exists():
            missing.append(rel)
    return sorted(set(missing))


def emit(dist: Path, api_base: str, asset_base: str, origin: str, version: str,
         dry_run: bool) -> int:
    index = dist / "index.html"
    if not index.is_file():
        print(f"❌ {index} 不存在 —— 这不是一个前端产物目录", file=sys.stderr)
        return 2

    html = index.read_text(encoding="utf-8")
    new_html, notes = rewrite_index_html(html, asset_base, origin)
    leftover = find_leftover(new_html, asset_base)
    missing = missing_assets(dist, new_html, asset_base)
    config = render_runtime_config(api_base, asset_base, version)

    if dry_run:
        print("（试运行，没有写任何文件）")
    else:
        (dist / "runtime-config.js").write_text(config, encoding="utf-8")
        if new_html != html:
            index.write_text(new_html, encoding="utf-8")

    print(f"  运行时配置：apiBase={api_base}  assetBase={asset_base}"
          + (f"  origin={origin}" if origin else ""))
    if notes:
        print(f"  改写了 {len(notes)} 处资源地址：")
        for n in notes[:12]:
            print(f"    · {n}")
        if len(notes) > 12:
            print(f"    · …… 另有 {len(notes) - 12} 处")
    else:
        print("  index.html 里没有需要改写的资源地址（产物可能不是本流程生成的）")

    if leftover:
        print(f"❌ index.html 里还有 {len(leftover)} 处资源地址没指向静态基址："
              f"{', '.join(leftover[:5])}", file=sys.stderr)
        print("   这些请求会打到页面所在的源（也就是带宽紧张的那台机），"
              "静态分离只做了一半。", file=sys.stderr)
        return 1

    if missing:
        print(f"⚠️  有 {len(missing)} 个被引用的资源在 dist 里不存在："
              f"{', '.join(missing[:5])}", file=sys.stderr)
        print("   （若资源已上传到 CDN 源站，这是正常的；否则页面会 404）", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="装配前端运行时配置（动静分离）")
    p.add_argument("--dist", required=True, help="前端产物目录（含 index.html）")
    p.add_argument("--api-base", default="/api",
                   help="API 基址（默认 /api，即源站同源）")
    p.add_argument("--asset-base", default="/",
                   help="静态资源基址（默认 /；CDN 形态传 https://cdn.example.com/）")
    p.add_argument("--origin", default="",
                   help="源站地址。给了就把 runtime-config.js 的引用指向它绝对地址")
    p.add_argument("--version", default="", help="版本号（写进配置，便于排查）")
    p.add_argument("--dry-run", action="store_true", help="只打印，不写文件")
    args = p.parse_args(argv)

    api_base = normalize_base(args.api_base, "/api")
    asset_base = normalize_dir_base(args.asset_base, "/")
    origin = (args.origin or "").strip().rstrip("/")

    # API 基址是绝对地址时，**必须**是 http/https —— 别的 scheme（比如
    # javascript:）写进配置等于给页面开一个后门。拦下来，别写进文件。
    if "://" in api_base and not api_base.startswith(("http://", "https://")):
        print(f"❌ api-base 只能是 http/https 开头：{api_base}", file=sys.stderr)
        return 2
    if "://" in asset_base and not asset_base.startswith(("http://", "https://")):
        print(f"❌ asset-base 只能是 http/https 开头：{asset_base}", file=sys.stderr)
        return 2

    return emit(Path(args.dist), api_base, asset_base, origin, args.version, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
