#!/usr/bin/env python3
"""替身浏览器 —— 把收到的命令行参数写进文件，然后立刻退出。

**为什么需要它。**
桌面壳唯一对外的副作用是 ``spawn()``：把浏览器进程拉起来。它坏掉的
症状是「双击什么也不发生」—— 没有报错、没有弹窗、没有非零退出码，
比崩溃还难查。而 ``--dry-run`` 在 ``main()`` 里是**提前返回**的，压根
走不到 ``spawn()``，所以只跑 dry-run 永远验不到这条路径。

**为什么必须是真 exe。**
Windows 的 ``CreateProcess`` 会校验 PE 头：把 ``.bat`` 改名成
``msedge.exe``，在创建进程时报「%1 不是有效的 Win32 应用程序」（错误 193）。
而壳传给浏览器的第一个参数是 ``--app=...``，``.bat`` 收到它就炸 ——
于是验到的是「spawn 抛异常」，不是「spawn 成功并把参数交出去了」。
所以这个替身要用 PyInstaller 打成**真 exe**。

**它只做一件事**：把 ``sys.argv`` 写成 UTF-8 JSON，写完就退出。
它不拉起任何窗口 —— 它是替身，不是浏览器。

用法（由 CI 或本地脚本调用）：

    python -m PyInstaller --onefile --name msedge tools/desktop-shell/stub_browser.py

记录文件的路径通过环境变量 ``CWQ_STUB_RECORD_FILE`` 传进来 —— 命令行
参数是**壳**决定的，替身没法自己指定「记到哪儿去」。
"""
from __future__ import annotations

import json
import os
import sys

# 记录文件的路径由父进程（探针）通过环境变量指定。
RECORD_ENV = "CWQ_STUB_RECORD_FILE"


def main() -> int:
    target = os.environ.get(RECORD_ENV)
    if not target:
        # 没有指定记录文件就安静退出。万一这个 exe 被误当成真浏览器启动了，
        # 不该在用户的终端里吐一堆噪音。
        return 0

    try:
        # **先写临时文件再原子替换**：探针是轮询这个文件的，
        # 读到写了一半的 JSON 会解析失败，然后报一个「替身没记录到参数」
        # 的假故障。`os.replace` 在 Windows 上是原子的。
        staging = f"{target}.part"
        with open(staging, "w", encoding="utf-8") as handle:
            json.dump(sys.argv, handle, ensure_ascii=False)
        os.replace(staging, target)
    except OSError:
        # 记录失败不是致命错误：替身已经完成了它的使命（被拉起来了）。
        # 探针会因超时而报错，那时的错误信息里会带上这个路径，更有用。
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
