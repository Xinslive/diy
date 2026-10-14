#!/usr/bin/env bash
# 发布脚本。三个铁律：
#   1. 先跑测试再切流量 —— 测试不过就不重启
#   2. 备份上一版代码，回滚是改回来而不是重新编译
#   3. 表结构变更只由 Alembic 负责，发布流程会先备份数据库再执行
#      ``alembic upgrade head``；迁移失败就不重启、不切前端产物
#
# **第 3 条原来是反的（P3-13）。** 旧注释写的是「数据库不在这条链路里（无
# migration），schema 由应用启动时自建」，而实现恰恰相反：第 6 步执行的就是
# ``alembic upgrade head``。注释说没有、代码在做，后果是看脚本的人按注释理解
# ——以为发版完全不碰数据库，于是在没有备份、没打算回滚的情况下发版。注释必须
# 和实现一致，否则它比没有注释更危险。
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
  log "  后端锁文件: $([[ -f backend/requirements-dev.lock ]] && echo requirements-dev.lock || echo '缺失 —— 将退回未锁定的 requirements.txt')"
  log "  前端锁文件: $([[ -f frontend/pnpm-lock.yaml ]] && echo pnpm-lock.yaml || echo '缺失')"
  log "  磁盘可用: $(df -h "$APP" | awk 'NR==2{print $4}')"
  exit 0
fi

BRANCH=$(resolve_branch)

log "1/7 拉代码（分支 ${BRANCH}）"
git fetch --all
git checkout "$BRANCH"
git pull --ff-only

# 每次发布都备份。原来是「目录不存在才备份」，父目录又从没被创建过 ——
# 于是 ``cp -a backend "$APP/.releases/<时间戳>"`` 在首次部署时因父目录
# 缺失而失败（有 set -e，直接中断）；而就算侥幸建成了，也只有第一次
# 发布会备份，之后每次发布都在**没有备份**的情况下覆盖代码。
log "2/7 备份当前版本"
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

log "3/7 装依赖"
cd "$APP/backend"
# 装**开发锁**（requirements-dev.lock = 运行时锁 + pytest + httpx），不是
# requirements-dev.txt。原因有二：
#   * 逐包定版 —— requirements*.txt 全是「>=」，装它等于每次发布都换一套版本。
#     那次测试跑通的依赖，和这次要发布的依赖，可能根本不是同一套东西；
#   * 必须含 pytest —— 第 4 步的测试闸要用它。只装 requirements.lock（纯运行时）
#     会让这台机器上 pytest 消失，测试闸直接跑不起来。这台发布机因此装着
#     pytest，是「它要先考试才能上岗」的代价，不是把测试框架塞进了生产镜像。
if [[ -f requirements-dev.lock ]]; then
  ./.venv/bin/pip install -q -r requirements-dev.lock
elif [[ -f requirements.lock ]]; then
  log "⚠️  缺 requirements-dev.lock，只装运行时依赖 —— 第 4 步的测试闸可能跑不起来"
  ./.venv/bin/pip install -q -r requirements.lock
elif [[ -f requirements.txt ]]; then
  log "⚠️  缺锁文件，本次退回未锁定的 requirements.txt —— 版本可能与测试时不同"
  ./.venv/bin/pip install -q -r requirements.txt
else
  # 既没有锁也没有清单：这不是 git 拉下来的仓库，是个手工搭的目录
  # （deploy/tests/test_deploy.sh 的沙箱就是这种形状，.venv 里全是桩）。
  # 这里不中止 —— 中止只会让沙箱在第 3 步就死掉，后面所有断言又会变成
  # 「脚本压根没跑起来所以什么都没发生」的假绿。但要喊出来。
  log "⚠️  这个发布目录里既没有锁文件也没有 requirements.txt，跳过装依赖"
fi

log "4/7 跑测试（不通过就中止，不重启）"
if ! ./.venv/bin/python -m pytest -q; then
  log "❌ 测试未通过，保持现状不变"
  exit 1
fi

# 项目提交的是 pnpm-lock.yaml，仓库里没有 package-lock.json，
# 所以 ``npm ci`` **必然失败**（npm ci 要求有 package-lock.json）。
log "5/7 构建前端"
cd "$APP/frontend"
if ! command -v pnpm >/dev/null 2>&1; then
  log "❌ 没装 pnpm，但仓库的锁文件是 pnpm-lock.yaml"; exit 1
fi
pnpm install --frozen-lockfile --silent
pnpm run build

# 表结构变更只由 Alembic 负责（P3-01）。原来「应用启动时 create_all」，
# 而 create_all 只建缺失的表、不改已存在的表 —— 生产上加一列 NOT NULL
# 会静默不生效，服务照常启动，问题到运行时才炸。
#
# 放在**测试之后、前端构建之后**：测试用的是测试库，不碰生产库，
# 所以测试挂掉时生产库的 schema 一个字节都没动。
log "6/7 备份数据库并执行迁移"
# 上一步构建前端时 cwd 已经被带到 frontend 了。忘了切回来，
# ``./.venv/bin/alembic`` 就会去 frontend/.venv 里找 —— 找不到，
# 迁移这步直接失败退出，连带后面「保留 KEEP 份备份」也一起不执行。
cd "$APP/backend"
DUMP="$RELEASES/$STAMP/database-before-migration.sql"
# 备份失败**必须**中止（P2-25）。
#
# 原来这里是「备份失败只喊一句，迁移照跑」。但备份是这个系统唯一的退路 ——
# 凭证号可回收、审计快照不可再生，迁移把表结构改坏之后能救命的只有这份 dump。
# 退路失败还继续往前走，等于「以为有退路」和「没有退路」两种状态跑在同一段
# 代码里，而且分不出来。真要豁免，就在 pg_dump 装不上的时候把备份步骤整个
# 去掉并让发布失败，不要留一个「记得看一眼日志」的分支 —— 没人会看。
if ./.venv/bin/python scripts/db_backup.py > "$DUMP" 2>"$RELEASES/$STAMP/db_backup.err"; then
  log "  数据库已备份到 ${DUMP}（$(wc -l < "$DUMP") 行）"
else
  log "❌ 数据库备份失败，见 ${RELEASES}/${STAMP}/db_backup.err"
  log "   没有退路就不迁移：生产库的 schema 一个字节都没动，保持现状不变"
  log "   修好备份（装 postgresql-client，或核对 DATABASE_URL）后重新发布"
  exit 1
fi
# 恢复命令一并打出来：出事时要靠它，而临场想起来的命令一定是错的
log "  万一要回退：cd $APP/backend && .venv/bin/python scripts/db_backup.py --restore --file ${DUMP}"
if ! ./.venv/bin/alembic upgrade head; then
  log "❌ 数据库迁移失败，保持现状不变（前端产物没切、没重启）"
  log "   库已回滚到迁移前状态；修好 revision 后重新发布即可"
  exit 1
fi
log "  schema 版本：$(./.venv/bin/alembic current 2>/dev/null | tail -1)"

# 换名落位，而不是删了再移。原来是 ``rm -rf dist`` 紧跟 ``mv dist dist`` ——
# 在同一个目录里自删自移：dist 先被自己删掉，后面那条 mv 必然失败
# （而且是在构建花掉几分钟之后才失败）。这里先把新产物换到暂存名，
# 再换上，窗口从「必然失败」缩到两次 rename 之间。
#
# 说清楚：这不是严格原子替换。真正原子要用符号链接切换，那要改
# vite 的 outDir 与 systemd 的工作目录，超出这次修复的范围。
# 旧产物已在第 2 步备份进 .releases，回滚路径不受这个窗口影响。
log "7/7 切换前端产物"
STAGE="$APP/frontend/.dist.new"
rm -rf "$STAGE"
mv "$APP/frontend/dist" "$STAGE"
mv "$STAGE" "$APP/frontend/dist"

systemctl restart cwq-api

# 探活**轮询**而不是「睡 3 秒打一发」。
#
# 原来这里是 ``sleep 3`` + 单次 ``curl -fsS``，失败就非零退出。问题是走到这
# 一行时，**该发生的都已经发生了**：迁移已执行、前端产物已切换、服务已重启。
# 运维看到「发布失败」，而新版本其实已经在跑。于是这个失败信号指向了一个
# 回滚不回去的状态 —— 照失败处理（回滚）等于把正在服务的版本撤掉，
# 不处理又会以为线上还是旧版。真正的失败原因（新版本起不来）和真正的误报
# （服务只是还没起来）在这个写法下长得一模一样。
#
# 探测口径沿用原来的那条命令（``curl -fsS``）—— ``-f`` 让 curl 在 HTTP >= 400
# 时返回非零，所以「curl 退出码为 0」就等于「拿到 2xx/3xx」。
# **刻意不改成解析 ``%{http_code}``**：那样只是把判定从 curl 挪到 shell，
# 语义没变，还多一个失败面（比如代理/重定向场景下 curl 退出码与状态码
# 的组合比想象中多）。这里要修的是「只试一次」，不是「换一套判定口径」。
#
# 唯一从 dev.sh 的 wait_http 借来的是**轮询**这个形状：最多 60 次 × 0.5 秒
# = 30 秒。
log "  等待 /api/health 应答（最多 30 秒）"
health_ok=0
for _ in $(seq 1 60); do
  if curl -fsS -m 2 -o /dev/null http://127.0.0.1:8000/api/health 2>/dev/null; then
    health_ok=1
    break
  fi
  sleep 0.5
done

if [ "$health_ok" -ne 1 ]; then
  log "❌ 发布后 30 秒内 /api/health 没有给出 2xx/3xx"
  log "   ⚠️  此时**新版本已经上线**：迁移已执行、前端产物已切换、服务已重启。"
  log "      这次发布不是「没发生」，不要直接当作「什么都没上线」。"
  log "   先分清是新版本起不来，还是探活口径不对："
  log "     journalctl -u cwq-api -n 200 --no-pager"
  log "     tail -50 /opt/cwq/backend/logs/api.err.log"
  log "   需要回滚时（人工确认后再做），备份在 ${RELEASES}/${STAMP}"
  exit 1
fi

log "  /api/health 应答"
log "✅ 发布完成"

# 只保留最近 KEEP 个备份
ls -dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf
