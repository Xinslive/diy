"""生产机护栏的钉子：1.6 GB 的机器上，「出事时先出谁」必须是确定的。

这些护栏全部是 **systemd / 运行时参数**，不是 Python 代码 ——
所以它们的回归**不会被任何 Python 测试抓到**，只能在这里钉住。

钉住的东西：

1. ``cwq-api.service`` 的内存上下限（MemoryHigh / MemoryMax）。
   **原来这一行是注释掉的** —— API 的 cgroup 没有任何内存上限，
   于是整机 OOM killer 挑谁被杀变成随机事件，而它挑的是**占用最多的**那个
   进程，也就是 PostgreSQL。PG 被杀 = 未提交事务回滚 + 连接被断开，
   症状是「偶发 500 / 连接重置」，根因却在应用这边，根本查不到。

2. 重启风暴闸（StartLimitBurst + Restart=on-failure）。
   ``Restart=always`` + ``RestartSec=3`` 意味着内存吃满时每 3 秒
   「一杀一拉」一轮，每轮都要重新 fork worker、重新导入框架、重新建连接池 ——
   **服务一次都没真正起来**，CPU 与 I/O 却全耗在重启上。

3. uvicorn 的在途请求上限（--limit-concurrency）。
   **排队中的请求照样占内存**：已解析的 body、连接、协程栈。
   不限并发 = 峰值内存 = 并发数 × 单请求占用。

4. 备份的临时文件不能落在 /tmp。
   ``cwq-api.service`` 开着 ``PrivateTmp=true``，服务里的 /tmp 是
   **内存盘（tmpfs）** —— 全量账务备份落进去就是直接吃内存，且永不换出。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
API_UNIT = REPO / "deploy" / "systemd" / "cwq-api.service"
START_SERVER = REPO / "backend" / "scripts" / "start_server.py"
BACKUP_SERVICE = REPO / "backend" / "app" / "backup" / "service.py"
BACKUP_EXPORT = REPO / "backend" / "app" / "backup" / "export.py"


def _unit_text() -> str:
    return API_UNIT.read_text(encoding="utf-8")


def _unit_directive(name: str) -> str | None:
    """取 unit 里某个键的值（只看 ``[Service]``/``[Unit]`` 段的有效赋值行）。

    刻意**只认没被注释掉的行** —— 注释掉的那行等于没有，这正是
    ``# MemoryMax=768M`` 那个洞的成因。
    """
    for line in _unit_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(f"{name}="):
            return stripped.split("=", 1)[1].strip()
    return None


# --------------------------------------------------------------------- 内存上下限


def test_api有内存硬上限():
    """MemoryMax 必须存在（且没被注释掉）。

    没有它，API 的 cgroup 可以一直涨到把整机的内存吃光 ——
    然后由整机 OOM killer 挑人被杀，而它挑的是占用最多的（PG）。
    """
    assert _unit_directive("MemoryMax") is not None, (
        "cwq-api.service 没有生效的 MemoryMax —— 那一行现在是注释掉的，"
        "等于 API 的 cgroup 没有任何内存上限")


def test_api有内存软上限且比硬上限低():
    """MemoryHigh 是「先让它慢下来」，MemoryMax 是「最后才杀它」。

    两档都要有：只有硬上限的话，进程会一路冲到上限再被杀，
    而软上限给出的那段回收期才是真正能救命的。
    """
    high = _unit_directive("MemoryHigh")
    max_ = _unit_directive("MemoryMax")
    assert high is not None, "没有 MemoryHigh —— 进程会一路冲到硬上限才被杀"
    assert max_ is not None, "没有 MemoryMax"
    to_mb = lambda v: int(re.sub(r"[^\d]", "", v))  # noqa: E731
    assert to_mb(high) < to_mb(max_), (
        f"MemoryHigh({high}) 必须小于 MemoryMax({max_}) —— "
        "软上限是硬上限之前那段「先回收、再杀」的缓冲")


def test_内存上限对1gb级的机器是合理的():
    """上限必须**明显低于** 1.6 GB 那台机器的总内存。

    那一台还跑着 11+ 子域与 3 个 Docker 容器。API 拿到 512 MB 已经是
    留给它的份额，再高就会把容器挤到 swap。
    """
    max_ = _unit_directive("MemoryMax") or ""
    mb = int(re.sub(r"[^\d]", "", max_) or 0)
    assert 0 < mb <= 512, (
        f"MemoryMax={max_} 对 1.6 GB 的生产机太大了 —— "
        "那一台还跑着 11+ 子域与 3 个 Docker 容器")


# --------------------------------------------------------------------- 重启风暴


def test_不是无脑always重启():
    """``Restart=always`` 在内存吃满时是灾难。

    每 3 秒「一杀一拉」一轮：fork worker、导入框架、建连接池 ——
    服务一次都没真正起来。改成 on-failure 之后，崩溃/OOM 仍然自动恢复，
    而**持续性的资源不足会显形**（单元进入 failed，journal 里看得到）。
    """
    assert _unit_directive("Restart") == "on-failure", (
        "Restart 应该是 on-failure —— always 会在持续 OOM 时变成重启风暴")


def test_有重启风暴闸():
    """``StartLimitBurst`` 必须存在且是个小数字。

    systemd 的默认值是 5 次 / 10 秒 —— 对一台 1.6 GB 的机器，
    「持续 OOM」会在半分钟内把机器的 CPU 全耗在重启上。
    收紧成 5 分钟 5 次：偶发崩溃仍然自动恢复，持续失败则**显形**。
    """
    burst = _unit_directive("StartLimitBurst")
    assert burst is not None, "没有 StartLimitBurst —— 重启风暴没有闸"
    assert int(burst) <= 5, f"StartLimitBurst={burst} 太大"
    interval = _unit_directive("StartLimitIntervalSec")
    assert interval is not None, "没有 StartLimitIntervalSec"
    assert int(re.sub(r"[^\d]", "", interval)) >= 60, (
        "StartLimitIntervalSec 太短 —— 那不是闸，是节流")


def test_oom时整个单元一起收掉():
    """``OOMPolicy=kill``：cgroup 被 OOM 命中时整个单元一起走。

    默认的 ``stop`` 只杀主进程，两个 worker 可能活下来变成孤儿 ——
    端口还占着、请求没人应答，而 systemd 认为服务已经停了。
    """
    assert _unit_directive("OOMPolicy") == "kill", (
        "OOMPolicy 应该是 kill —— 否则 worker 会变成孤儿，端口还占着")


# --------------------------------------------------------------------- 在途请求


def test_有在途请求上限():
    """``--limit-concurrency`` 必须给。

    排队中的请求照样占内存（已解析的 body、连接、协程栈），
    所以「不限并发」= 峰值内存 = 并发数 × 单请求占用。
    """
    src = START_SERVER.read_text(encoding="utf-8")
    assert "--limit-concurrency" in src, (
        "uvicorn 没有 --limit-concurrency —— 请求可以无限排队，"
        "而排队中的每个请求都占着内存")
    assert "--limit-max-requests" in src, (
        "uvicorn 没有 --limit-max-requests —— worker 永不回收，"
        "任何慢泄漏都会让常驻内存单调上升")


def test_访问日志不记两遍():
    """uvicorn 这层要关掉访问日志 —— nginx 已经记了同一批请求。

    同一批请求记两遍，每一遍都要做一次格式化 + 一次写盘，
    在只有 2 vCPU 的机器上是白付的固定开销。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("cwq_start_server", START_SERVER)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    uvicorn_cmd = launcher.launch_command("uvicorn", host="127.0.0.1", port=8000, workers=2)
    assert "--no-access-log" in uvicorn_cmd, (
        "uvicorn 的访问日志没有关 —— 它与 nginx 的那份完全重复："
        f"{uvicorn_cmd}")

    # gunicorn 那条路本来就没有访问日志，**不要**把它打开。
    # 这里看的是**真正跑起来的命令**，不是源文件里的注释 ——
    # 注释里提到某个参数名是正常的事（解释为什么不加它）。
    gunicorn_cmd = launcher.launch_command("gunicorn", host="127.0.0.1", port=8000, workers=2)
    assert "--access-logfile" not in gunicorn_cmd, (
        "gunicorn 那条路的访问日志被打开了 —— 它与 nginx 的那份重复："
        f"{gunicorn_cmd}")


def test_并发上限是可配的():
    """上限要能从 .env 调 —— 生产机的真实并发与压测环境不一样。

    写死在代码里就是「又一个随手写的数字」。
    """
    src = START_SERVER.read_text(encoding="utf-8")
    assert "CWQ_API_MAX_CONCURRENCY" in src, (
        "并发上限应当可以用环境变量调 —— 真实并发与压测环境不一样")
    assert "CWQ_API_MAX_REQUESTS" in src


# --------------------------------------------------------------------- tmpfs 备份


def test_备份不落到系统临时目录():
    """备份的工作目录**绝不能**是系统临时目录。

    ``PrivateTmp=true`` 让服务里的 /tmp 变成内存盘（tmpfs）：
    全量账务备份落进去 = 直接吃内存，而且不换出。

    **缺省值必须是磁盘上的目录**（应用自己的 ``backend/data/backup-work``）——
    落在磁盘上就没有内存风险，不需要「没配就报错」那种一刀切
    （那会把一条配置纪律变成使用障碍：开发者点一下「立即备份」就报错）。
    """
    src = BACKUP_SERVICE.read_text(encoding="utf-8")
    workdir = src.split("def _workdir", 1)[1].split("\ndef ", 1)[0]
    assert "gettempdir" not in workdir, (
        "_workdir 还在回退到 tempfile.gettempdir() —— "
        "PrivateTmp=true 之下那是内存盘")
    # 缺省值必须是应用目录下的一个磁盘路径。
    assert "BACKEND_ROOT" in workdir, (
        "_workdir 的缺省值必须落在应用自己的目录里（磁盘、且在 ReadWritePaths 白名单里）")
    # 仍然允许运维把它挪到别处。
    assert "CWQ_BACKUP_WORK_DIR" in workdir, (
        "应当仍然允许用 CWQ_BACKUP_WORK_DIR 把工作目录挪到别处")


def test_解密的明文快照不落系统临时目录():
    """恢复时解出来的明文快照，临时文件必须落在磁盘目录。

    快照大小 = 它占的内存；落在 tmpfs 上就是直接吃内存。
    """
    src = BACKUP_EXPORT.read_text(encoding="utf-8")
    assert "mkstemp(" in src
    # 每个 mkstemp 都必须带 dir=
    for m in re.finditer(r"mkstemp\(([^)]*)\)", src, re.S):
        args = m.group(1)
        assert "dir=" in args, (
            f"mkstemp 没有指定 dir=：{m.group(0)[:80]} —— "
            "不指定就落在系统临时目录（内存盘）")
