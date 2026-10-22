# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包描述 —— 把 launcher.py 打成 CWQ.exe。

用法（在 tools/desktop-shell 下）：
    pyinstaller launcher.spec                 # 单文件（默认）
    set CWQ_SHELL_ONEDIR=1 && pyinstaller launcher.spec   # 目录模式

**为什么默认单文件（--onefile）**：这个壳的全部交付物就是「一个 exe」。
目录模式要发一个文件夹、用户还得知道别删里面的东西；单文件是发一个
文件、双击即用。

**为什么同时保留目录模式的开关**：单文件每次启动都把自己解包到临时目录，
这既是杀软「内存写入」启发式的靶子，也是杀软误报的常见来源。
如果真的被拦了、或者用户反馈启动慢，改一个环境变量就能切成目录模式
重来 —— 这比「改 spec 文件」轻得多。

几个关键参数及其理由：

``console=False``
    GUI 程序不该在双击后闪一个黑框。但代价是**出错的 print 用户看不见** ——
    所以 launcher.py 里所有面向用户的错误都走 ``show_message()``（系统消息框），
    且日志落到 ``%LOCALAPPDATA%\\财务记账系统\\logs\\shell.log``。

``upx=False``
    UPX 壳的特征是杀软规则库的重点命中项，而且它在这个壳上省不了多少体积
    （壳本身只有几 MB）。关掉。

``icon=`` / ``version=``
    图标与 VERSIONINFO。VERSIONINFO 不是装饰 —— 杀软误报申诉会检查
    FileDescription / CompanyName / FileVersion，见 version_info.txt 里的说明。

``excludes=``
    这个壳只用标准库，把用不到的重型标准库剔掉能显著减小体积。
    列表里的每一项都是**确认过不依赖**的（launcher.py 只 import
    argparse/ctypes/json/os/platform/re/subprocess/sys/time/webbrowser 等）。
    删 ``sqlite3`` 是因为 webbrowser 不走它，而 ``venv``/``idlelib``/
    ``turtledemo`` 纯粹是顺带的。
"""
import os
from pathlib import Path

# PyInstaller 注入的全局变量：spec 文件所在目录
SPEC_DIR = Path(SPECPATH).resolve()
REPO_ROOT = SPEC_DIR.parents[1]

# 目录模式开关：CI 与本地脚本都用环境变量切，避免改 spec 文件
ONEDIR = os.environ.get("CWQ_SHELL_ONEDIR") == "1"

ICON = SPEC_DIR / "shell.ico"
VERSION_FILE = SPEC_DIR / "version_info.txt"

# 图标不存在就现场生成 —— 少一个「忘了先跑 make_icon.py」的失败点。
# make_icon.py 是纯标准库，运行时依赖为零。
if not ICON.is_file():
    import subprocess
    import sys

    subprocess.run(
        [sys.executable, str(SPEC_DIR / "make_icon.py"), str(ICON)],
        check=True,
    )

EXCLUDES = [
    # GUI 无关的重型标准库
    "tkinter",
    "turtledemo",
    "idlelib",
    "test",
    "unittest",
    "pydoc_data",
    # 打包/开发工具链
    "distutils",
    "lib2to3",
    "ensurepip",
    "venv",
    # 用不到
    "sqlite3",
    "pdb",
    "doctest",
]

a = Analysis(
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    # 图标要进产物：exe 的图标由 bootloader 读取，运行时不需要，
    # 但把它一并带上便于排查「图标没出来」。
    datas=[(str(ICON), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if ONEDIR:
    # 目录模式：exe 不含依赖，依赖由 COLLECT 摊在旁边的 _internal 里。
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="CWQ",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON),
        version=str(VERSION_FILE),
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="CWQ",
    )
else:
    # 单文件模式（默认）：依赖与数据全部塞进 exe。
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="CWQ",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(ICON),
        version=str(VERSION_FILE),
    )
