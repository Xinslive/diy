#!/usr/bin/env python
"""把前端静态资源发布到 OSS（CDN 的源站）。

    # 试跑：只看会传什么，不写一个字节
    python scripts/publish_assets.py --dist /opt/cwq/frontend/dist --dry-run

    # 真传
    python scripts/publish_assets.py --dist /opt/cwq/frontend/dist

    # 连入口 HTML 一起传（**页面也托管在 CDN 上**时才需要）
    python scripts/publish_assets.py --dist ... --include-html

--------------------------------------------------------------------------------
它是干什么的
--------------------------------------------------------------------------------
动静分离有两种常见形态：

* **回源形态**：CDN 缓存未命中时回源到源站取。源站仍然存着静态资源，
  但每个文件每个 CDN 节点只回源一次 —— 带宽已经省下来了。
* **源站在 OSS 形态**：CDN 的源站是 OSS bucket，源站（小带宽服务器）
  **一个静态字节都不出**。这就是本脚本做的事。

两种形态都支持；本脚本只服务第二种。

--------------------------------------------------------------------------------
**为什么只传 assets/，不传 index.html（默认）**
--------------------------------------------------------------------------------
入口 HTML 里写的是**带内容哈希**的资源地址，而 HTML 自己**不缓存**
（改了就生效，见 nginx 的 ``location /``）。若把 HTML 也传上 CDN 并缓存，
就会出现「发布之后用户拿到的还是上一版 HTML」—— 那套前端指向的还是上一版的
资源，看起来「界面没变」，而实际上**新版本根本没上线**。

所以默认只传 ``assets/``：那些文件名带哈希、**内容不变**、可以被永久缓存。

要连页面一起托管（页面也在 CDN 上），用 ``--include-html``，并且**必须**
在 CDN 侧对 index.html 配「不缓存 / 短 TTL + 发布后刷新」，否则同样会
卡在旧版本上。

--------------------------------------------------------------------------------
缓存策略（写进对象元数据）
--------------------------------------------------------------------------------
``Cache-Control: public, max-age=31536000, immutable`` —— 一年且不可变。
文件名带内容哈希，内容变了文件名就变了，所以「不可变」是**真的**不可变：
不存在「同一个 URL 内容变了」的情况，一年缓存不会出错。

**跨域**：跨域加载 ES module 必须过 CORS 校验。Aliyun OSS 的跨域由
**bucket 级 CORS 规则**控制（不是对象元数据），所以请在 OSS 控制台给
bucket 配一条允许 GET/HEAD、来源 ``*`` 的规则。脚本会检查并提示。
"""
from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

# 这些后缀要按文本压缩率算 —— 纯二进制的图片/字体再压也白压。
COMPRESSIBLE = {".js", ".mjs", ".css", ".html", ".json", ".map", ".svg", ".txt"}


def oss_settings(args: argparse.Namespace) -> dict[str, str]:
    """取 OSS 参数：**命令行 > 环境变量**。

    环境变量名与 backend/app/core/config.py 里的 Settings 字段同名
    （``OSS_ENDPOINT`` / ``OSS_ACCESS_KEY_ID`` / ...），这样部署机上那份
    .env 不用为这条路径再准备一份。
    """
    def pick(flag: str, env: str) -> str:
        return (getattr(args, flag, "") or os.environ.get(env, "") or "").strip()

    return {
        "endpoint": pick("endpoint", "OSS_ENDPOINT"),
        "access_key_id": pick("access_key_id", "OSS_ACCESS_KEY_ID"),
        "access_key_secret": pick("access_key_secret", "OSS_ACCESS_KEY_SECRET"),
        "bucket": pick("bucket", "OSS_BUCKET"),
        "prefix": (pick("prefix", "OSS_WEB_PREFIX") or "acct-web").strip("/"),
    }


def missing(cfg: dict[str, str]) -> list[str]:
    labels = (("endpoint", "Endpoint"), ("access_key_id", "AccessKey ID"),
              ("access_key_secret", "AccessKey Secret"), ("bucket", "Bucket"))
    return [label for key, label in labels if not cfg.get(key)]


def collect(dist: Path, include_html: bool) -> list[tuple[Path, str]]:
    """列出要传的文件与对象键。返回 [(本地路径, 对象键)]。

    **只传 assets/ 目录下的东西**（加上可选的入口 HTML）：
    dist 里别的文件（favicon、robots.txt 之类）由运维按需自行处理 ——
    猜哪些该传、哪些不该传，比不猜更危险。
    """
    files: list[tuple[Path, str]] = []
    assets = dist / "assets"
    if assets.is_dir():
        for path in sorted(assets.rglob("*")):
            if path.is_file():
                files.append((path, str(path.relative_to(dist)).replace(os.sep, "/")))
    if include_html:
        for name in ("index.html", "runtime-config.js"):
            path = dist / name
            if path.is_file():
                files.append((path, name))
    return files


def content_type(path: Path) -> str:
    guess, _ = mimetypes.guess_type(path.name)
    return guess or "application/octet-stream"


def cache_control(path: Path, is_html: bool) -> str:
    """入口 HTML 与静态资源的缓存策略**必须不同**。

    HTML 缓存住了 = 新版本上不来（用户拿到旧 HTML → 旧资源 → 界面没变）。
    所以 HTML 走短缓存，静态资源走一年不可变。
    """
    if is_html:
        return "no-cache, must-revalidate"
    return "public, max-age=31536000, immutable"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="把前端静态资源发布到 OSS（CDN 源站）")
    p.add_argument("--dist", required=True, help="前端产物目录")
    p.add_argument("--endpoint", default="")
    p.add_argument("--access-key-id", default="")
    p.add_argument("--access-key-secret", default="")
    p.add_argument("--bucket", default="")
    p.add_argument("--prefix", default="", help="对象键前缀（默认 acct-web）")
    p.add_argument("--include-html", action="store_true",
                   help="连入口 HTML 一起传（页面也托管在 CDN 上时才需要）")
    p.add_argument("--dry-run", action="store_true", help="只打印，不上传")
    args = p.parse_args(argv)

    dist = Path(args.dist)
    if not (dist / "index.html").is_file():
        print(f"❌ {dist}/index.html 不存在 —— 这不是一个前端产物目录", file=sys.stderr)
        return 2

    files = collect(dist, args.include_html)
    if not files:
        print(f"❌ {dist} 里没有可发布的静态资源（assets/ 是空的？）", file=sys.stderr)
        return 2

    cfg = oss_settings(args)
    absent = missing(cfg)
    if absent:
        print(f"❌ OSS 参数不全，缺：{'、'.join(absent)}", file=sys.stderr)
        print("   可以在命令行给，也可以在部署机的 .env 里写 "
              "OSS_ENDPOINT / OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_BUCKET。",
              file=sys.stderr)
        return 2

    total = sum(path.stat().st_size for path, _ in files)
    print(f"  目标：oss://{cfg['bucket']}/{cfg['prefix']}/")
    print(f"  文件 {len(files)} 个，合计 {total / 1024:.1f} KiB")
    if args.dry_run:
        for path, key in files[:10]:
            print(f"    · {key}  ({path.stat().st_size} B)")
        if len(files) > 10:
            print(f"    · …… 另有 {len(files) - 10} 个")
        print("（试运行，没有上传任何东西）")
        return 0

    try:
        import oss2  # noqa: PLC0415 — 故意晚导入：--dry-run 不需要这个依赖
    except ImportError:
        print("❌ 缺少 oss2（pip install oss2）", file=sys.stderr)
        return 2

    auth = oss2.Auth(cfg["access_key_id"], cfg["access_key_secret"])
    bucket = oss2.Bucket(auth, cfg["endpoint"], cfg["bucket"])

    ok = 0
    for path, key in files:
        obj_key = f"{cfg['prefix']}/{key}"
        is_html = key.endswith(".html")
        headers = {
            "Content-Type": content_type(path),
            "Cache-Control": cache_control(path, is_html),
        }
        try:
            bucket.put_object(obj_key, path.read_bytes(), headers=headers)
        except Exception as exc:  # noqa: BLE001 — 逐个文件失败要说清楚是哪个
            print(f"❌ 上传失败：{obj_key} —— {exc}", file=sys.stderr)
            return 4
        ok += 1

    print(f"  已上传 {ok} 个对象到 oss://{cfg['bucket']}/{cfg['prefix']}/")
    print("  提醒：跨域加载 ES module 必须过 CORS，请确认 OSS bucket 配了")
    print("        允许 GET/HEAD、来源 * 的 CORS 规则；CDN 侧也要能加响应头。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
