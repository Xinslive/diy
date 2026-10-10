#!/usr/bin/env bash
# 发布脚本。三个铁律：
#   1. 先跑测试再切流量 —— 测试不过就不重启
#   2. 备份上一版代码，回滚是改回来而不是重新编译
#   3. 数据库不在这条链路里（无 migration），schema 由应用启动时自建
set -euo pipefail

APP=/opt/cwq
KEEP=3

log() { echo "[$(date '+%F %T')] $*"; }

cd "$APP"

if [[ "${1:-}" == "--check-only" ]]; then
  log "只做检查，不发布"
fi

log "1/5 拉代码"
git fetch --all
git checkout main
git pull --ff-only

log "2/5 备份当前版本"
if [[ ! -d "$APP/.releases" ]]; then
  cp -a backend "$APP/.releases/$(date '+%Y%m%d-%H%M%S')"
fi

log "3/5 装依赖"
cd backend
./.venv/bin/pip install -q -r requirements.txt

log "4/5 跑测试（不通过就中止，不重启）"
if ! ./.venv/bin/python -m pytest -q; then
  log "❌ 测试未通过，保持现状不变"
  exit 1
fi

log "5/5 构建前端"
cd "$APP/frontend"
npm ci --silent
npm run build
rm -rf "$APP/frontend/dist"
mv dist "$APP/frontend/dist"

systemctl restart cwq-api
sleep 3
curl -fsS http://127.0.0.1:8000/api/health >/dev/null

log "✅ 发布完成"

# 只保留最近 KEEP 个备份
ls -dt "$APP/.releases"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf
