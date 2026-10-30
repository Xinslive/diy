#!/usr/bin/env python
"""把「随应用发布的打印字体」抓下来（自托管 webfont）。

    # 验一遍仓库里那份还对不对（默认动作，不联网）
    python scripts/fetch_print_font.py

    # 重新抓（字体版本换了才用；会覆盖 assets/fonts/ 下的文件）
    python scripts/fetch_print_font.py --download

--------------------------------------------------------------------------------
为什么要有这个字体
--------------------------------------------------------------------------------
打印页要的是**所见即所得**：在 A 机器上设计、在 B 机器上印，纸上的样子必须
一模一样。可字体是**系统给的** —— 「宋体」在 Windows 上是 SimSun，在 macOS 上
回退成 Songti SC，两者的字宽、空格宽度、字形都不一样。换台机器印出来就变样，
用户看到的预览和纸上的纸不是一回事。

2026-10-30 用户就撞上了这个：「本地开发端看着很协调，生产的空格太宽了」。

**版式**能靠单位钉死（位置用百分比、字号用 pt、行高固定、字距用全角空格
U+3000 —— 全角空格的 advance 恒为 1em，与字体无关）；**字形**钉不死：只能
把字体本身一起带上，让每台机器渲染的是同一份字节。

所以这里放一份**思源宋体（Noto Serif SC）**：SIL OFL 1.1 授权，可商用、可
再分发（OFL 要求随字体附上授权书，见同目录 ``OFL.txt``）。

--------------------------------------------------------------------------------
为什么是「分片」而不是一个整字体文件
--------------------------------------------------------------------------------
完整的 Noto Serif SC 有几 MB，一个字一个字都用得上，但**一页凭证只用到
几百个字**。Google Fonts 的做法是按 Unicode 区段把字体切成 ~100 片，每片
配一条 ``unicode-range``：浏览器只下载这一页真正用到的那几片（通常几十到
几百 KB），其余一片都不取。

这 101 片是**同一个可变字体**的 101 个片段（regular 与 bold 共用同一份文件，
字重由可变字体的 ``wght`` 轴给），所以一共只有 5.7 MB。

--------------------------------------------------------------------------------
这份脚本留下的东西
--------------------------------------------------------------------------------
``backend/app/assets/fonts/``：

* ``nsc-XXX.woff2`` —— 101 个分片
* ``cwq-serif.css``  —— 与分片一一对应的 ``@font-face``（含 unicode-range）
* ``manifest.json``  —— 每个分片的来源 URL、字节数、sha256
* ``OFL.txt``        —— SIL OFL 1.1 授权书（OFL 要求随字体分发）

``--check``（默认）只按 manifest 里的 sha256 核一遍磁盘上的文件：少一片、
被截断、传坏了都能当场发现，而不用等到某个用户打印时才发现字库缺字。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

#: 字体资源目录（随应用一起打包进镜像）。
FONT_DIR = Path(__file__).resolve().parent.parent / "app" / "assets" / "fonts"

#: CSS 里用的字体族名。**与 ``app/printing/fonts.py`` 里的常量必须一致**。
CSS_NAME = "cwq-serif"
#: 字体族在 CSS 里对外的名字（``font-family`` 用它）。
FAMILY_NAME = "CWQ Serif"
#: 文件名（不含后缀）。
PREFIX = "nsc"

#: Google Fonts 的 CSS 接口。
#:
#: **要的是区间（``400..900``），不是列举（``400;700``）**：区间请求拿回来的是
#: **可变字体**本身（一个文件带 wght 轴，声明 ``font-weight: 400 900``），
#: 列举请求拿回来的是**两个静态实例**（两份文件，字形各自定死）。
#: 前者 101 片覆盖全部字重，后者同样 101 片但只有 400/700 两档 ——
#: 而中间的字重（500、600）浏览器只能自己合成（假粗体），字形就又变了。
CSS_URL = "https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400..900"
#: 授权书（OFL 1.1）。
OFL_URL = "https://raw.githubusercontent.com/google/fonts/main/ofl/notoserifsc/OFL.txt"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _parse_css(css: str) -> list[tuple[str, str]]:
    """从 Google 的 CSS 里抽出 ``(url, unicode-range)``，**按出现顺序**。

    同一片文件会被 regular/bold 各声明一次 —— 按 url 去重，保留第一次的
    unicode-range（两份一样）。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for block in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
        m_url = re.search(r"url\((https://[^)]+\.woff2)\)", block)
        m_range = re.search(r"unicode-range:\s*([^;]+);", block)
        if not m_url or not m_range:
            continue
        url = m_url.group(1)
        if url in seen:
            continue
        seen.add(url)
        out.append((url, m_range.group(1).strip()))
    return out


def _css_text(slices: list[tuple[str, str]]) -> str:
    """生成与分片一一对应的 ``@font-face``。

    ``font-display: block`` 而不是 Google 默认的 ``swap``：``swap`` 会先用
    系统字体排一遍版、字体到位后再换一遍 —— 那一瞬间的排版是**另一份**，
    打印出去就又「换个环境样式就变」了。``block`` 在字体到位前不画字，
    版式从头到尾只有一份。
    """
    head = [
        "/* 思源宋体（Noto Serif SC）分片 —— 由 scripts/fetch_print_font.py 生成。",
        " *",
        " * **别手改这个文件**：改了就与 manifest.json 对不上，",
        " *   scripts/fetch_print_font.py 的校验会红。",
        " *",
        " * 字重用可变字体的 wght 轴（400..900），所以 101 个分片覆盖全部字重，",
        " *   不必每个字重各存一份。",
        " *",
        " * font-display:block —— 字体到位前不画字。swap 会先按系统字体排一遍版、",
        " *   到位后再换一遍，那一瞬间的排版是另一份，正是要消灭的东西。",
        " */",
    ]
    body = []
    for i, (_url, urange) in enumerate(slices):
        body.append(
            "@font-face {\n"
            f"  font-family: '{FAMILY_NAME}';\n"
            "  font-style: normal;\n"
            "  font-weight: 400 900;\n"
            "  font-display: block;\n"
            f"  src: url('/api/print/fonts/{PREFIX}-{i:03d}.woff2') format('woff2');\n"
            f"  unicode-range: {urange};\n"
            "}"
        )
    return "\n".join(head) + "\n" + "\n".join(body) + "\n"


def download() -> int:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"取 CSS：{CSS_URL}")
    css = _get(CSS_URL).decode("utf-8")
    slices = _parse_css(css)
    if not slices:
        print("!! 没从 CSS 里解析出任何分片 —— Google 的接口格式变了？", file=sys.stderr)
        return 1
    print(f"共 {len(slices)} 个分片，开始下载…")

    manifest = []
    for i, (url, urange) in enumerate(slices):
        name = f"{PREFIX}-{i:03d}.woff2"
        data = _get(url)
        (FONT_DIR / name).write_bytes(data)
        manifest.append({
            "name": name,
            "url": url,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "unicode_range": urange,
        })
        print(f"  {name}  {len(data)/1024:.1f} KB")

    (FONT_DIR / f"{CSS_NAME}.css").write_text(_css_text(slices), encoding="utf-8")
    (FONT_DIR / "manifest.json").write_text(
        json.dumps({
            "family": FAMILY_NAME,
            "source": "Noto Serif SC (Google Fonts)",
            "license": "SIL Open Font License 1.1",
            "slices": manifest,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (FONT_DIR / "OFL.txt").write_bytes(_get(OFL_URL))
    print(f"写好了：{FONT_DIR}")
    return 0


def check() -> int:
    """按 manifest 核一遍磁盘上的文件。**不联网。**"""
    mpath = FONT_DIR / "manifest.json"
    if not mpath.exists():
        print(f"!! 没有 {mpath} —— 跑一次 --download", file=sys.stderr)
        return 1
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    bad = 0
    for item in manifest["slices"]:
        p = FONT_DIR / item["name"]
        if not p.exists():
            print(f"!! 缺 {item['name']}")
            bad += 1
            continue
        data = p.read_bytes()
        if len(data) != item["bytes"]:
            print(f"!! {item['name']} 字节数不对：{len(data)} != {item['bytes']}")
            bad += 1
            continue
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            print(f"!! {item['name']} sha256 不对")
            bad += 1
    for extra in ("cwq-serif.css", "OFL.txt"):
        if not (FONT_DIR / extra).exists():
            print(f"!! 缺 {extra}")
            bad += 1
    if bad:
        print(f"字体资源有问题 {bad} 处", file=sys.stderr)
        return 1
    total = sum(i["bytes"] for i in manifest["slices"])
    print(f"字体资源齐了：{len(manifest['slices'])} 片，共 {total/1048576:.2f} MB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="抓/验随应用发布的打印字体")
    ap.add_argument("--download", action="store_true", help="从 Google Fonts 重新抓（会覆盖）")
    args = ap.parse_args()
    return download() if args.download else check()


if __name__ == "__main__":
    raise SystemExit(main())
