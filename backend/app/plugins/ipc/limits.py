"""worker 进程**自己**给自己上的资源与权限限制。

**为什么是 worker 自己上，而不是宿主上**
----------------------------------------
宿主用 ``subprocess`` 起 worker 时本来可以用 ``preexec_fn`` 设限，但：

* ``preexec_fn`` 在**多线程**进程里是不安全的（Python 文档明确警告）——
  宿主是多线程的（FastAPI 的线程池 + 插件租约心跳线程）；
* 而 worker 自己是单线程的、刚起来的，**在它自己的代码里设限是最稳的**。

真正彻底的形态是 systemd（``ProtectSystem=strict`` / ``PrivateTmp`` /
``RestrictAddressFamilies=AF_UNIX`` / ``TasksMax=``），那需要**部署侧**把 worker
放到独立的 unit 里 —— 宿主就不能自己 spawn 它了。这是阶段 3 剩下的部分，
本模块是「不依赖 systemd 也能拿到的那一半」。

**故意不设的两项**
------------------
* ``RLIMIT_NPROC``：它是**按用户**计的（同一个 uid 的所有进程算在一起），
  在 worker 里设它会**连带限制宿主**（宿主也会 fork 线程与 worker）——
  一个「给插件限资源」的动作变成「把整个服务限死」。要用它得用 systemd 的
  ``TasksMax=``（按 unit 计）✓。
* ``RLIMIT_AS`` / ``RLIMIT_FSIZE``：worker 自己要写字节信封的临时文件
  （上传与导入批次可以到 GB 级），限文件大小会直接把上传功能打死；
  限地址空间则可能让 Python 自己在分配时就炸。两者都需要按实际负载定值，
  没有实测依据之前**不猜**。

**设了的项**
------------
* ``RLIMIT_CORE = 0``：不产生 core dump。core dump 里可能有账套数据与令牌 ——
  插件崩了就把这些写进磁盘，是一条真实的数据泄漏路径。
* ``RLIMIT_NOFILE``：文件描述符上限（取「当前值」与「配置值」的**小者**，
  绝不抬高）。
* ``PR_SET_NO_NEW_PRIVS``（Linux）：禁止进程通过 setuid 文件提权。插件代码
  虽然拿不到 ``os.system`` / ``exec``（沙箱那一层），但这是**第二道**，
  而且不依赖沙箱是否被绕过。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

#: 默认的文件描述符上限。够用（worker 只有一个 socket + 几个临时文件），
#: 又足够小到「插件疯狂开文件」不会把宿主的描述符表拖垮。
DEFAULT_MAX_OPEN_FILES = 256


def _resource():
    """``resource`` 模块（POSIX 才有）。"""
    try:
        import resource
    except ImportError:  # pragma: no cover —— Windows
        return None
    return resource


def _set_no_new_privs() -> bool:
    """Linux 上设 ``PR_SET_NO_NEW_PRIVS``（禁止 setuid 提权）。

    非 Linux 返回 ``False``（不是失败，只是这一层没有）。
    """
    if not sys_platform_is_linux():
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_NO_NEW_PRIVS = 38
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            log.warning("worker 没能设置 no_new_privs（errno=%s）", ctypes.get_errno())
            return False
        return True
    except Exception:  # noqa: BLE001 —— 拿不到 libc 就算了，不影响别的限制
        log.warning("worker 设置 no_new_privs 失败", exc_info=True)
        return False


def sys_platform_is_linux() -> bool:
    return os.uname().sysname == "Linux" if hasattr(os, "uname") else False


def apply_worker_limits(*, max_open_files: int = DEFAULT_MAX_OPEN_FILES) -> dict:
    """把限制落到当前进程上，返回**实际生效**的清单。

    返回的清单会随 ``hello`` 消息发回宿主 —— 这样「限制到底生效了没有」
    是**可观测**的，而不是「代码里写了就当它生效了」。
    """
    applied: dict = {}
    resource = _resource()
    if resource is not None:
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            applied["core"] = 0
        except (ValueError, OSError) as exc:
            log.warning("worker 没能限制 core dump：%s", exc)
        if max_open_files > 0:
            try:
                soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                target = max_open_files if hard == resource.RLIM_INFINITY else min(max_open_files, hard)
                # **只降不升**：抬上限会把宿主的设置顶掉。
                if soft == resource.RLIM_INFINITY or soft > target:
                    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
                applied["nofile"] = target
            except (ValueError, OSError) as exc:
                log.warning("worker 没能限制文件描述符：%s", exc)
    applied["no_new_privs"] = _set_no_new_privs()
    return applied
