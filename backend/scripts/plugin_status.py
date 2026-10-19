#!/usr/bin/env python3
"""部署用插件 barrier 检查；与核心 /api/ready 分开，不执行代码或 DDL。"""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from app.core.config import settings
from app.core.db import SessionLocal
from app.plugins.models import PluginRegistry
from app.plugins.workers import status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wait', type=float, default=0)
    args = parser.parse_args()
    if not settings.plugin_enabled:
        print(json.dumps({'enabled': False, 'plugins': []}))
        return 0
    deadline = time.monotonic() + max(0, args.wait)
    while True:
        with SessionLocal() as db:
            rows = []
            for plugin in db.scalars(select(PluginRegistry).where(PluginRegistry.deleted_at.is_(None))):
                state, code = status(db, plugin)
                rows.append({'id': plugin.plugin_id, 'status': state, 'error_code': code})
        if all(row['status'] in {'READY', 'DISABLED'} for row in rows):
            print(json.dumps({'enabled': True, 'plugins': rows}))
            return 0
        if time.monotonic() >= deadline:
            print(json.dumps({'enabled': True, 'plugins': rows}))
            return 1
        time.sleep(.25)


if __name__ == '__main__':
    raise SystemExit(main())
