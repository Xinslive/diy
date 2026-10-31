#!/usr/bin/env python3
"""把本地构建出来的插件包装进**本机 dev 库**（不走 HTTP、不用管理页）。

为什么要有这个脚本
------------------
管理页上的「安装」要管理员身份 + 近期邮箱二次验证 —— 本地调试时那套流程
是纯噪音。这里直接调宿主自己的安装器，走的仍然是**同一条**校验链：
验签 → 静态扫描 → 迁移 → 落盘 → 登记。

**已装过就升级**：升级要走宿主的正规路径 —— 先**停用**（升级要求插件
处于停止状态），再 `install(..., upgrade_id=...)`，再**启用**。
版本号必须**更高**（宿主拒绝「同版本重装」），所以每轮改动都要把
``tools/build_sharedcard_plugin.py`` 里的版本号往上抬一位。

**必须在 backend/ 下跑**：pydantic-settings 从**当前目录**读 ``.env``，
插件开关与可信公钥都在那儿。站在仓库根会读不到配置，报出来的错
（"插件平台未开启"）会让人以为是插件坏了。

用法::

    cd backend
    ../tools/install_plugin_local.py ../output/plugins/com.cwq.sharedcard-2.1.1.cwp
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def restart_server() -> None:
    """把本机开发服务重启一遍。

    升级要「等旧 worker 退出」，光停用不够 —— **worker 是进程**，
    它得真的退出（宿主才会允许升级）。开发环境里重启服务是 ``./dev.sh``。
    """
    import subprocess
    import time
    import urllib.request

    dev_sh = ROOT / "dev.sh"
    if not dev_sh.exists():
        print("（没有 dev.sh，跳过重启）")
        return
    print("重启本机服务…")
    subprocess.run([str(dev_sh), "stop"], cwd=str(ROOT), check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.Popen([str(dev_sh)], cwd=str(ROOT), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(120):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3) as response:
                if response.status == 200:
                    print("服务已就绪")
                    return
        except Exception:
            continue
    print("（服务没在 120 秒内就绪，继续往下走 —— 你可以自己再 ./dev.sh 一次）")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    package = Path(sys.argv[1]).resolve()
    if not package.exists():
        print(f"包不存在：{package}")
        return 2
    restart = "--no-restart" not in sys.argv
    os.chdir(BACKEND)
    sys.path.insert(0, str(BACKEND))

    from app.core.db import SessionLocal
    from app.models.plugin import PluginRegistry
    from app.models.system import SysUser
    from app.plugins.api import set_enabled
    from app.plugins.installer import install

    data = package.read_bytes()
    with SessionLocal() as db:
        admin = db.query(SysUser).filter(SysUser.is_admin.is_(True)).order_by(SysUser.id).first()
        if admin is None:
            print("本机库里没有管理员账号 —— 先跑一次 seed 或手工建一个。")
            return 3
        existing = db.get(PluginRegistry, "com.cwq.sharedcard")
        if existing is None:
            result = install(data, admin)
            print(f"安装结果：{result}")
        else:
            # 升级路径：宿主要求「先停用、**重启**、等旧 worker 退出」——
            # 旧 worker 还拿着旧包在跑，升级是不许的（那会让两个版本同时在跑）。
            set_enabled(db, existing.plugin_id, admin, False)
            print(f"已停用 {existing.plugin_id}（当前 {existing.version}）")
            if restart:
                restart_server()
            result = install(data, admin, upgrade_id=existing.plugin_id)
            print(f"升级结果：{result}")
            set_enabled(db, existing.plugin_id, admin, True)
            print("已重新启用")
    if restart:
        restart_server()
    print("插件已就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
