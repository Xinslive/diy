#!/usr/bin/env bash
# 发布脚本。三个铁律：
#   1. 先跑测试再切流量 —— 测试不过就不重启
#   2. 备份上一版代码，回滚是改回来而不是重新编译
#   3. 数据库不在这条链路里（无 migration），schema 由应用启动时自建
set -euo pipefail

APP=${APP_DIR:-/opt/cwq}
KEEP=3
# 分支不要写死。仓库默认分支改名（main/master 之争）时，写死的名字会在
# ``set -e`` 下直接让发布失败 —— 而这恰恰是**最不该失败的时候**
# （运维正在发布，脚本却因为分支名对不上而中断）。
# 优先用显式指定，其次 remote HEAD，最后退回 main/master 挨个试。
: "${DEPLOY_BRANCH:=}"
BRANCH="$DEPLOY_BRANCH"
RELEASES="$APP/.releases"

log() { echo "[$(date '+%F %T')] $*"; }

resolve_branch() {
  if [[ -n "$BRANCH" ]]; then echo "$BRANCH"; return; fi
  # remote HEAD 是「远端默认分支」，跟着仓库走，不会过期
  local head
  if head=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null); then
    echo "${head#origin/}"; return
  fi
  for cand in main master; do
    if git show-ref --verify --quiet "refs/remotes/origin/$cand"; then
      echo "$cand"; return
    fi
  done
  log "❌ 找不到可用的远端分支，请设置 DEPLOY_BRANCH"; exit 1
}

cd "$APP"

# --check-only 必须**真的只检查**。原来这里只 log 了一句就往下走，
# 于是运维以为在做发布前预检，实际执行了完整发布流程 ——
# 备份、装依赖、跑测试、构建、重启服务，一样没少。
if [[ "${1:-}" == "--check-only" ]]; then
  log "只做检查，不发布"
  log "  当前分支: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 未知)"
  log "  待发布分支: $(resolve_branch)"
  log "  后端依赖: $([[ -x ./.venv/bin/python ]] && echo 已就绪 || echo '缺失 .venv')"
  log "  前端锁文件: $([[ -f frontend/pnpm-lock.yaml ]] && echo pnpm-lock.yaml || echo '缺失')"
  log "  磁盘可用: $(df -h "$APP" | awk 'NR==2{print $4}')"
  exit 0
fi

BRANCH=$(resolve_branch)

log "1/6 拉代码（分支 ${BRANCH}）"
git fetch --all
git checkout "$BRANCH"
git pull --ff-only

# 每次发布都备份。原来是「目录不存在才备份」，父目录又从没被创建过 ——
# 于是 ``cp -a backend "$APP/.releases/<时间戳>"`` 在首次部署时因父目录
# 缺失而失败（有 set -e，直接中断）；而就算侥幸建成了，也只有第一次
# 发布会备份，之后每次发布都在**没有备份**的情况下覆盖代码。
log "2/6 备份当前版本"
mkdir -p "$RELEASES"
# 时间戳只到秒：同一秒里连发两次（比如上一��失败后立刻重试）会撞成
# 同一个目录，第二次把第一次的备份覆盖掉 —— 于是「有备份」这件事
# 看着成立，真要回滚时拿到的却是新版本。加个序号后缀躲开。
STAMP=$(date '+%Y%m%d-%H%M%S')
n=1
while [[ -e "$RELEASES/$STAMP" ]]; do
  STAMP="$(date '+%Y%m%d-%H%M%S')-$n"
  n=$((n + 1))
  [[ "$n" -gt 100 ]] && { log "❌ 备份目录名反复冲突，中止"; exit 1; }
done
mkdir -p "$RELEASES/$STAMP"
for d in backend frontend/dist; do
  if [[ -e "$APP/$d" ]]; then
    cp -a "$APP/$d" "$RELEASES/$STAMP/$(basename "$d")"
  fi
done
log "  已备份到 ${RELEASES}/${STAMP}"

log "3/6 装依赖"
cd "$APP/backend"
./.venv/bin/pip install -q -r requirements.txt

log "4/6 跑测试（不通过就中止，不重启）"
if ! ./.venv/bin/python -m pytest -q; then
  log "❌ 测试未通过，保持现状不变"
  exit 1
fi

# 项目提交的是 pnpm-lock.yaml，仓库里没有 package-lock.json，
# 所以 ``npm ci`` **必然失败**（npm ci 要求有 package-lock.json）。
log "5/6 构建前端"
cd "$APP/frontend"
if ! command -v pnpm >/dev/null 2>&1; then
  log "❌ 没装 pnpm，但仓库的锁文件是 pnpm-lock.yaml"; exit 1
fi
pnpm install --frozen-lockfile --silent
pnpm run build

# 换名落位，而不是删了再移。原来是 ``rm -rf dist`` 紧跟 ``mv dist dist`` ——
# 在同一个目录里自删自移：dist 先被自己删掉，后面那条 mv 必然失败
# （而且是在构建花掉几分钟之后才失败）。这里先把新产物换到暂存名，
# 再换上，窗口从「必然失败」缩到两次 rename 之间。
#
# 说清楚：这不是严格原子替换。真正原子要用符号链接切换，那要改
# vite 的 outDir 与 systemd 的工作目录，超出这次修复的范围。
# 旧产物已在第 2 步备份进 .releases，回滚路径不受这个窗口影响。
log "6/6 切换前端产物"
STAGE="$APP/frontend/.dist.new"
rm -rf "$STAGE"
mv "$APP/frontend/dist" "$STAGE"
mv "$STAGE" "$APP/frontend/dist"

systemctl restart cwq-api
sleep 3
curl -fsS http://127.0.0.1:8000/api/health >/dev/null

log "✅ 发布完成"

# 只保留最近 KEEP 个备份
ls -dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf
