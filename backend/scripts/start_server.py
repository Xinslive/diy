#!/usr/bin/env python3
"""Supervisor entry point: one restart generation inherited by every child worker."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import time


def launch_command(mode, *, host, port, workers):
    if not 1 <= workers <= 64:
        raise ValueError('CWQ_API_WORKERS 必须在 1 到 64 之间')
    if mode == 'uvicorn':
        return [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', host,
                '--port', str(port), '--workers', str(workers), '--timeout-keep-alive', '120']
    return [sys.executable, '-m', 'gunicorn', 'app.main:app', '-k', 'uvicorn.workers.UvicornWorker',
            '--bind', f'{host}:{port}', '--workers', str(workers), '--timeout', '120']


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
