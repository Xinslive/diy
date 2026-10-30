#!/usr/bin/env python
"""把「随应用发布的打印字体」抓下来（自托管 webfont）。

    # 验一遍仓库里那份还对不对（默认动作，不联网）
    python scripts/fetch_print_font.py

    # 重新抓（字体版本换了才用；会覆盖 assets/fonts/ 下的文件）
    python scripts/fetch_print_font.py --download

    # 只按现有清单重生成样式表（改了 CSS 规则时用；不联网、不重下 5.7 MB）
    python scripts/fetch_print_font.py --css-only

--------------------------------------------------------------------------------
为什么要有这个字体
--------------------------------------------------------------------------------
打印页要的是**所见即所得**：在 A 机器上设计、在 B 机器上印，纸上的样子必须
一模一样。可字体是**系统给的** —— 「宋体」在 Windows 上是 SimSun，在
macOS 上回退成 Songti SC，两者的字宽、空格宽度、字形都不一样。换台机器印
出来就变样，用户看到的预览和纸上的纸不是一回事。

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
几十个字**。Google Fonts 的做法是按 Unicode 区段把字体切成 101 片，每片
配一条 ``unicode-range``：浏览器只下载这一页真正用到的那几片，其余一片
都不取。

这 101 片是**同一个可变字体**的 101 个片段（regular 与 bold 共用同一份
文件，字重由可变字体的 ``wght`` 轴给），所以一共只有 5.7 MB。

--------------------------------------------------------------------------------
**不要再按字频重新切一遍**（2026-10-30 实测）
--------------------------------------------------------------------------------
用户看到「一页凭证要下 12 个分片 / 849 KB，而页面上只有 52 个字」，自然会
想到「切得更细 / 按字频切」。这两条路都试过了，**都更差**：

* **cn-font-split**（也就是 chinese-font.netlify.app 那个在线分包工具背后的
  库）：按字频切出来 396 片、共 17.1 MB，一页凭证要点 **23 次**、
  **682 KB** —— 请求数比 101 片那套还多，字节数只省了 20%。
* **fontTools 自己按字频切**（97 片、每片 320 字、保留 wght 轴）：一页凭证
  **1825 KB**，比原来的 848 KB **更差**。

**为什么更差。** 一个 CJK 字在这份可变字体里压完还有 ~370 B。一页凭证那
52 个字在**字频序上摊得很开**（最靠后的一个字排在第 2072 位），所以无论怎么
切，浏览器都得把「覆盖到那个名次」的所有分片整片整片地拉下来 —— 换句话说，
**字节数由「最冷僻的那个字的名次」决定，跟怎么切无关**。要真正省字节，只
有两条路：把字做得更小（静态字重，放弃可变轴），或者**按页现做子集**
（运行期裁剪）。前者与「加粗字必须是真加粗」冲突，后者是一次架构改造。

所以分片这块**不动**，真正要修的是另外两件事：

* ``font-display`` 从 ``block`` 改成 ``swap``（见 ``_css_text``）——
  ``block`` 在字体到位前**一个字都不画**，用户看到的就是「卡住了」。
* **缓存**（资源版本号进 URL + ``immutable``）—— 见 ``version_of``。

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

#: 字体资源在站点上的路径前缀。**与 ``app/printing/fonts.py`` 必须一致**。
FONT_URL_PREFIX = "/api/print/fonts"
#: URL 里的版本段。URL 形如 ``/api/print/fonts/v/<版本>/nsc-000.woff2``。
FONT_VERSION_DIR = "v"

#: **CSS 生成规则的版本号。改 CSS 的生成规则时必须 +1。**
#:
#: 资源版本号（VERSION）由它参与计算 —— 不 +1 的话，浏览器会一直用着缓存里
#: 的旧样式表，新规则（哪怕只是 ``font-display`` 变了）永远不生效。
#: 这是一个「改了但看不见」的坑，所以把它做成一个必须显式维护的常量。
CSS_RECIPE = "swap2"

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


def version_of(entries: list[dict]) -> str:
    """资源版本号：CSS 规则版本 + 每个分片的 sha256，压成 12 位十六进制。

    **为什么要版本号。** 字体是静态资源，而静态资源要能放心地
    ``immutable`` 缓存一年 —— 前提是「同一个 URL 的字节永远不变」。
    没有版本号就做不到：换一版权体，分片还是 ``nsc-000.woff2`` 这个名字，
    浏览器缓存里那份旧字节会被一直发下去（而且不报错）。

    **版本号进 URL 而不是只进响应头**：响应头变了浏览器要重新校验，
    URL 变了才是「另一样东西」—— URL 里带版本，缓存才能一路命中。

    参与计算的是**分片的 sha256**（内容）与 **CSS 规则版本**（生成方式）：
    两者任一变了，版本就变，URL 就变。
    """
    h = hashlib.sha256()
    h.update(CSS_RECIPE.encode("utf-8"))
    for item in entries:
        h.update(item["name"].encode("utf-8"))
        h.update(item["sha256"].encode("utf-8"))
    return h.hexdigest()[:12]


def _css_text(entries: list[dict], version: str) -> str:
    """生成与分片一一对应的 ``@font-face``。

    **``font-display: swap`` 而不是 ``block``。**

    ``block`` 的语义是「字体到位前不画字」—— 好处是全程只有一份排版，
    坏处是**字体没到之前页面上一个字都没有**。用户看到的正是「打开锁定字体
    之后预览卡住了」，而卡住的那几秒里其实一切都在正常下载。

    ``swap`` 先用系统字体排一遍、字体到位后换一遍。那一瞬间的排版确实
    是「另一份」，但它**不会流到纸面上**：预览在字体到位后重量一次
    （``PrintPreview.vue`` / ``PrintStage.vue`` 里的 ``fontsReady``），
    打印前也等字体（``api/print.js`` 里的 ``printWindow``）——
    纸上永远是内嵌字体那一版。

    所以这里换 ``swap``：省掉的是「白屏等字体」的那几秒，付出的是预览里
    一闪而过的系统字体字形（而且只在字体还没进缓存的第一次出现）。
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
        " * font-display:swap —— 字体没到位时先用系统字体画，不留白屏。",
        " *   最终版式由「字体到位后重量」+「打印前等字体」保证，纸上永远是",
        " *   这一份字形。",
        " *",
        f" * 资源版本：{version}（内容哈希，内容或规则变了就会变）。",
        " */",
    ]
    body = []
    for item in entries:
        name = item["name"]
        urange = item["unicode_range"]
        body.append(
            "@font-face {\n"
            f"  font-family: '{FAMILY_NAME}';\n"
            "  font-style: normal;\n"
            "  font-weight: 400 900;\n"
            "  font-display: swap;\n"
            f"  src: url('{FONT_URL_PREFIX}/{FONT_VERSION_DIR}/{version}/{name}') format('woff2');\n"
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

    entries = []
    for i, (url, urange) in enumerate(slices):
        name = f"{PREFIX}-{i:03d}.woff2"
        data = _get(url)
        (FONT_DIR / name).write_bytes(data)
        entries.append({
            "name": name,
            "url": url,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "unicode_range": urange,
        })
        print(f"  {name}  {len(data)/1024:.1f} KB")

    version = version_of(entries)
    (FONT_DIR / f"{CSS_NAME}.css").write_text(_css_text(entries, version), encoding="utf-8")
    (FONT_DIR / "manifest.json").write_text(
        json.dumps({
            "family": FAMILY_NAME,
            "source": "Noto Serif SC (Google Fonts)",
            "license": "SIL Open Font License 1.1",
            "css_recipe": CSS_RECIPE,
            "version": version,
            "slices": entries,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (FONT_DIR / "OFL.txt").write_bytes(_get(OFL_URL))
    print(f"写好了：{FONT_DIR}（资源版本 {version}）")
    return 0


def regenerate_css() -> int:
    """只按现有清单重生成样式表。**不联网、不重下分片。**

    什么时候用：改了 CSS 的生成规则（``font-display``、版本号算法…）之后。
    为了改一行 ``font-display`` 重下 5.7 MB，太不划算。
    """
    mpath = FONT_DIR / "manifest.json"
    if not mpath.exists():
        print(f"!! 没有 {mpath} —— 跑一次 --download", file=sys.stderr)
        return 1
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    entries = manifest.get("slices") or []
    if not entries:
        print("!! 清单里一片都没有", file=sys.stderr)
        return 1
    version = version_of(entries)
    (FONT_DIR / f"{CSS_NAME}.css").write_text(_css_text(entries, version), encoding="utf-8")
    manifest["css_recipe"] = CSS_RECIPE
    manifest["version"] = version
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"样式表重生成好了：资源版本 {version}")
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

    # **样式表必须与当前生成规则逐字一致。**
    # 手改了它、或者改了规则（CSS_RECIPE）忘了重生成，都在这里红 ——
    # 这两种情况的症状都是「浏览器拿着旧规则缓存，用户看到的是老样子」，
    # 现场早就没了。
    version = version_of(manifest["slices"])
    if manifest.get("version") != version:
        print(f"!! 清单里的版本号是 {manifest.get('version')}，按当前规则应为 {version}"
              " —— 跑 --css-only")
        bad += 1
    css_path = FONT_DIR / f"{CSS_NAME}.css"
    if css_path.exists():
        if css_path.read_text(encoding="utf-8") != _css_text(manifest["slices"], version):
            print("!! 样式表与当前生成规则对不上（多半是手改了，或改了规则忘了重生成）"
                  " —— 跑 --css-only")
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
    ap.add_argument("--css-only", action="store_true",
                    help="只按现有清单重生成样式表（不联网、不重下分片）")
    args = ap.parse_args()
    if args.download:
        return download()
    if args.css_only:
        return regenerate_css()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
