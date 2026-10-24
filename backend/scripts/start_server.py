#!/usr/bin/env python3
"""Supervisor entry point: one restart generation inherited by every child worker."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import time


def _env_int(name: str, default: int) -> int:
    """读一个正整数环境变量。

    与 ``CWQ_API_WORKERS`` 同一套规矩：**读不到就用默认值**。
    读到了却不是正整数就是配置错误 —— 那必须在启动前就失败，
    而不是让 uvicorn 收到一个空字符串或 0 然后行为异常
    （``--limit-concurrency 0`` 的含义是「拒绝一切请求」，
    那种故障在外部看就是「服务启动了但什么都访问不了」）。

    这些键刻意**不进** ``config.py``：它们是**给 systemd 读的**进程参数，
    不是应用侧配置 —— 与 ``CWQ_API_WORKERS`` 的理由完全一样
    （Python 那边读不到，留一个读不到的配置项比没有更危险）。
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f'{name} 必须是正整数，实际是 {raw!r}') from exc
    if value <= 0:
        raise ValueError(f'{name} 必须是正整数，实际是 {value}')
    return value


def launch_command(mode, *, host, port, workers):
    if not 1 <= workers <= 64:
        raise ValueError('CWQ_API_WORKERS 必须在 1 到 64 之间')
    # ---- 护栏参数（2026-10-24 性能审计给 1.6 GB 生产机加的）----
    #
    # --limit-concurrency
    #   **排队中的请求照样占内存。** uvicorn 默认不限制并发，于是慢请求
    #   （比如一次整本账的打印）会把后续请求全排在队列里，每个排队请求
    #   都攥着已解析的 body、连接与协程栈。峰值内存 = 并发数 × 单请求占用。
    #   设了上限之后，超出的请求立刻 503 —— **明确失败**比「全都慢到
    #   超时」好：用户知道要少来几个人，而不是看着页面一直转。
    #
    #   默认 32 是按「2 worker、每 worker 16 个在途」定的。
    #   **注意 MCP 的 SSE 连接会长期占住一个名额**（nginx 侧读超时 360s），
    #   所以这个数不能压得太低 —— 32 是「MCP 有人用 + 十几个人同时查账」
    #   都放得下的余量。真的不够用就调大它（``CWQ_API_MAX_CONCURRENCY``）。
    #
    # --limit-max-requests（+jitter）
    #   **worker 永不回收，账却越记越多。** 任何慢泄漏（缓存、对象池、
    #   第三方库的碎片）都会让常驻内存单调上升。定期重启 worker 是
    #   最便宜的兜底：jitter 是为了**不要所有 worker 同时重启**
    #   （同时重启 = 那一刻没人应答）。代价是每隔一段时间有一次
    #   单 worker 的短暂换血，nginx 的 keepalive 连接会自动重连。
    #
    # --no-access-log
    #   访问日志与 **nginx 的访问日志完全重复** —— 同一批请求记两遍。
    #   uvicorn 这份每请求还要做一次格式化 + 一次写盘，在只有 2 vCPU 的
    #   机器上是白付的固定开销。nginx 那份才是要长期留存的。
    #
    # --timeout-keep-alive 60
    #   与 nginx 的 ``keepalive_timeout 65`` 对齐（原来这里是 120）：
    #   65~120 秒之间会留着一批 nginx 认为已经可以复用、但后端还攥着的
    #   僵尸连接。
    max_concurrency = _env_int('CWQ_API_MAX_CONCURRENCY', 32)
    max_requests = _env_int('CWQ_API_MAX_REQUESTS', 2000)
    jitter = _env_int('CWQ_API_MAX_REQUESTS_JITTER', 200)
    if mode == 'uvicorn':
        return [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', host,
                '--port', str(port), '--workers', str(workers), '--timeout-keep-alive', '60',
                '--limit-concurrency', str(max_concurrency),
                '--limit-max-requests', str(max_requests),
                '--limit-max-requests-jitter', str(jitter),
                '--no-access-log']
    # gunicorn 的访问日志**默认就是关的**（不传 --access-logfile 就没有）——
    # 刻意保持原样，不要加 ``--access-logfile -``：nginx 那份已经记了同一批
    # 请求，app 这份记第二遍只是在 2 vCPU 的机器上多花格式化与写盘。
    return [sys.executable, '-m', 'gunicorn', 'app.main:app', '-k', 'uvicorn.workers.UvicornWorker',
            '--bind', f'{host}:{port}', '--workers', str(workers), '--timeout', '120',
            '--limit-requests', str(max_requests),
            '--limit-requests-jitter', str(jitter)]


def restart_environment(workers):
    env = os.environ.copy()
    # Always replace inherited values; generation represents this supervisor lifetime.
    env.update(CWQ_PLUGIN_GENERATION=secrets.token_hex(32),
               CWQ_PLUGIN_STARTED_AT=str(time.time_ns()), CWQ_PLUGIN_EXPECTED_WORKERS=str(workers))
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server', choices=['uvicorn', 'gunicorn'], default='uvicorn')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--workers', type=int, default=None)
    args = parser.parse_args()
    try:
        workers = args.workers if args.workers is not None else int(os.environ.get('CWQ_API_WORKERS', '2'))
        command = launch_command(args.server, host=args.host, port=args.port, workers=workers)
    except ValueError as exc:
        parser.error(str(exc))
    os.execvpe(command[0], command, restart_environment(workers))


if __name__ == '__main__':
    main()
