#!/usr/bin/env bash
# 发布脚本。四个铁律：
#   1. 先验证再切流量 —— 产物取不到就不重启
#   2. 备份上一版，回滚是改回来而不是重新编译
#   3. 表结构变更只由 Alembic 负责，迁移前必须先备份数据库
#   4. 备份失败就不迁移
#
# ---------------------------------------------------------------------------
# **第 1 条的铁律变了，闸门没变弱。**
# ---------------------------------------------------------------------------
# 原来这里是「git pull → pytest -q → pnpm build → 迁移 → 切产物 → 重启」，
# 也就是**生产机自己就是构建机**。实测（压测基线报告 2026-10-15）那条路径是
# 生产机上最重的一步：pytest.ini 默认 ``-n 6``，6 个并行 worker 各自要建一个
# 独立 PG 库（conftest 按 PYTEST_XDIST_WORKER 加库名后缀），再叠 pnpm install
# 与 vite build。它要求生产机常驻 Node.js / pnpm / pytest 三套只为发版存在的
# 工具链，还让发版尖峰有机会把机器压垮。
#
# 现在改成：CI（.github/workflows/ci.yml 的 build-artifacts job）出产物，
# 生产机只负责取产物 → 装运行时依赖 → 迁移 → 切产物 → 重启。
#
# **「先跑测试再切流量」并没有被削弱，只是闸门换了位置。** build-artifacts
# job 的 ``needs: [backend, frontend]`` 意味着：产物存在 = 后端全量 pytest
# 与前端测试/构建都已通过。所以这里的「取产物」就是新的闸门 ——
# **取不到就 exit 1，什么都不动**。backend job 里那道「护栏必须真的被执行」
# 和「用例数下限」的校验，同样因为这条 needs 而成为上线前置条件。
#
# **部署机不再需要** node / pnpm / pytest / postgresql-client 之外的构建工具：
# 只有 pg_dump（备份用）与 git（可选，仅用于记录版本）还需要。
set -euo pipefail

APP=${APP_DIR:-/opt/cwq}
KEEP=3
# 分支不写死（仓库默认分支改名时，写死的名字会让发布脚本在 ``set -e`` 下
# 直接失败 —— 而那是最不该失败的时候）。发布模式不再 checkout 分支，
# 这个变量保留是为了 ``--check-only`` 与运维排查时仍能回答「现在跑的是哪条线」。
: "${DEPLOY_BRANCH:=}"
BRANCH="$DEPLOY_BRANCH"
RELEASES="$APP/.releases"

# ---------------------------------------------------------------------------
# 产物从哪来
# ---------------------------------------------------------------------------
# 默认用 GitHub CLI 从 Actions 拉本次 commit 的产物。装 gh 并登录一次
# （GH_TOKEN 环境变量，或 ``gh auth login``）即可。
#
# **可以完全绕开 gh**：把三个文件（两个 tar.gz + manifest.txt + SHA256SUMS）
# 下载到任意目录，然后用 ``ARTIFACT_DIR`` 指过去。这条路径让部署可以
# 走内网镜像、对象存储、或人工上传 —— 脚本不关心来源，只校验产物本身。
: "${ARTIFACT_DIR:=}"
: "${REPO:=$(cd "$APP" 2>/dev/null && git config --get remote.origin.url || echo '')}"
: "${ARTIFACT_REF:=main}"

log() { echo "[$(date '+%F %T')] $*"; }

die() { echo "[$(date '+%F %T')] ❌ $*" >&2; exit 1; }

# --------------------------------------------------------------------------
# 产物的取得与校验
# --------------------------------------------------------------------------

fetch_artifacts() {
  local dest="$1"

  if [[ -n "$ARTIFACT_DIR" ]]; then
    # 人工投放：只要四个文件在就算数，来源不追究
    local missing=0
    for f in manifest.txt SHA256SUMS; do
      [[ -f "$ARTIFACT_DIR/$f" ]] || { echo "  缺 $f"; missing=1; }
    done
    compgen -G "$ARTIFACT_DIR/*.tar.gz" > /dev/null || { echo "  缺 *.tar.gz"; missing=1; }
    [[ $missing -eq 0 ]] || die "ARTIFACT_DIR=${ARTIFACT_DIR} 里没有完整的一套产物。
   内容应当是：cwq-backend-<sha>.tar.gz、cwq-frontend-<sha>.tar.gz、
   manifest.txt、SHA256SUMS（build-artifacts job 产出）。"
    cp "$ARTIFACT_DIR"/*.tar.gz "$ARTIFACT_DIR"/manifest.txt \
       "$ARTIFACT_DIR"/SHA256SUMS "$dest"/
    log "  从 ARTIFACT_DIR 取得产物"
    return
  fi

  command -v gh > /dev/null 2>&1 || die "没装 gh，也没有指定 ARTIFACT_DIR。
   二选一：
     1) 装 gh 并设好 GH_TOKEN，然后重跑
     2) 把 build-artifacts 的产物下载到任意目录，用 ARTIFACT_DIR=... 指定"

  log "  从 GitHub Actions 拉取 ${ARTIFACT_REF} 的产物"
  # ``--repo`` 显式给，不依赖当前目录是不是 git 仓库 —— 部署目录可能只是
  # 一个被 rsync 过来的产物，没有 .git，那 gh 会因为认不出仓库而失败。
  ( cd "$dest" && gh run download --repo "$REPO" --name "cwq-deploy-$(gh run view --repo "$REPO" --json headSha -q .headSha)" ) \
    || die "取产物失败 —— 这一步取不到就意味着 CI 没全绿（build-artifacts 的 needs
   是 [backend, frontend]）。**没有任何东西被改动，服务仍是上一版。**"
}

verify_artifacts() {
  local dir="$1"
  # sha256 校验：确认拿到的是 CI 出品的那一份，而不是传输损坏或中途被换过。
  ( cd "$dir" && sha256sum --check SHA256SUMS ) \
    || die "产物校验和不匹配 —— 文件在传输中损坏或被替换过。**没有改动任何东西。**"
  log "  产物校验和通过"
  # 清单里的版本要跟 manifest 自洽：文件名带 sha，manifest 也记同一个 sha。
  local sha
  sha=$(sed -nE 's/^sha=//p' "$dir/manifest.txt")
  [[ -n "$sha" ]] || die "manifest.txt 里没有 sha= 那一行，产物不是预期格式。"
  ls "$dir"/*-"${sha}".tar.gz > /dev/null 2>&1 \
    || die "产物文件名里的 sha 与 manifest 不一致（manifest: ${sha}）。**没有改动任何东西。**"
  log "  产物版本 ${sha}（commit $(sed -nE 's/^commit=//p' "$dir/manifest.txt")）"
}

# --------------------------------------------------------------------------
# --check-only 必须**真的只检查**（原来就有的语义，不许悄悄改成真发布）
# --------------------------------------------------------------------------
if [[ "${1:-}" == "--check-only" ]]; then
  log "只做检查，不发布"
  log "  当前分支: $(git rev-parse --abbrev-current HEAD 2>/dev/null || echo 未知)"
  log "  产物来源: ${ARTIFACT_DIR:-（未指定，将从 GitHub Actions 拉取）}"
  log "  后端依赖: $([[ -x "$APP/backend/.venv/bin/python" ]] && echo 已就绪 || echo '缺失 .venv')"
  log "  后端锁文件: $([[ -f "$APP/backend/requirements.lock" ]] && echo requirements.lock || echo '缺失')"
  log "  Node 工具链: $(command -v pnpm >/dev/null && echo '⚠️ 仍装着（发版已不再需要，可以卸）' || echo '未安装（符合预期）')"
  log "  pytest: $(command -v pytest >/dev/null && echo '⚠️ 仍装着（发版已不再需要，可以卸）' || echo '未安装（符合预期）')"
  log "  磁盘可用: $(df -h "$APP" | awk 'NR==2{print $4}')"
  exit 0
fi

cd "$APP"

STAGE="$APP/.artifact.$$"
mkdir -p "$STAGE"

# 每次发布都备份。原来是「目录不存在才备份」，父目录又从没被创建过 ——
# 于是 ``cp -a backend "$APP/.releases/<时间戳>"`` 在首次部署时因父目录
# 缺失而失败（有 set -e，直接中断）；而就算侥幸建成了，也只有第一次
# 发布会备份，之后每次发布都在**没有备份**的情况下覆盖代码。
log "1/7 取 CI 产物（测试闸门：取不到就是 CI 没全绿）"
STAMP=$(date '+%Y%m%d-%H%M%S')
n=1
while [[ -e "$RELEASES/$STAMP" ]]; do
  STAMP="$(date '+%Y%m%d-%H%M%S')-$n"
  n=$((n + 1))
  [[ "$n" -gt 100 ]] && { die "备份目录名反复冲突，中止"; }
done

fetch_artifacts "$STAGE"
verify_artifacts "$STAGE"
SHA=$(sed -nE 's/^sha=//p' "$STAGE/manifest.txt")
log "  取到产物 ${SHA}"

log "2/7 备份当前版本"
# 备份目录在**产物取到之后**才建。建早了的后果很小（多一个空目录），
# 但它意味着「取不到产物」这件事在磁盘上留了痕迹 —— 而取不到产物正是
# 「CI 没全绿」这条闸门触发的场合，那一刻应该**一个字节都没动**。
mkdir -p "$RELEASES"
mkdir -p "$RELEASES/$STAMP"
for d in backend frontend/dist; do
  if [[ -e "$APP/$d" ]]; then
    cp -a "$APP/$d" "$RELEASES/$STAMP/$(basename "$d")"
  fi
done
# 产物本身也存一份：回滚时不必重新下载，产物被清理掉也还能退回去
cp -a "$STAGE" "$RELEASES/$STAMP/artifact"
echo "  已备份到 ${RELEASES}/${STAMP}"

# --------------------------------------------------------------------------
# 装依赖：只装 requirements.lock
# --------------------------------------------------------------------------
# 原来装的是 requirements-dev.lock —— 因为第 4 步的测试闸要用 pytest。
# 测试闸移到 CI 之后，生产机就不需要测试框架了，于是这里也换成运行时锁。
# 这条改动不是顺手：dev 锁里带 pytest / httpx，生产机上装不装测试框架
# 曾经是「说不清这台机器装的东西是给谁用的」。
log "3/7 装运行时依赖"
cd "$APP/backend"
if [[ -f requirements.lock ]]; then
  ./.venv/bin/pip install -q -r requirements.lock
else
  die "缺 requirements.lock —— 没有锁文件就无法保证「装出来的就是测试时那一版」。
   **当前版本仍在服务，未做任何改动。**"
fi

# --------------------------------------------------------------------------
# 表结构变更只由 Alembic 负责（P3-01）
# --------------------------------------------------------------------------
# 原来这里跑的是「应用启动时 create_all」，而 create_all 只建缺失的表、
# 不改已存在的表 —— 生产上加一列 NOT NULL 会静默不生效，服务照常启动，
# 问题到运行时才炸。现在由 CI 产出的迁移 + 这里的 upgrade head 负责。
#
# 顺序仍然是「备份数据库 → 迁移 → 切产物 → 重启」，且任何一步失败都不切产物。
log "4/7 备份数据库并执行迁移"
DUMP="$RELEASES/$STAMP/database-before-migration.sql"
# 备份失败**必须中止**（P2-25）。
# 原来这里是「备份失败只喊一句，迁移照跑」。但备份是这个系统唯一的退路 ——
# 凭证号可回收、审计快照不可再生，迁移把表结构改坏之后能救命的只有这份 dump。
# 退路失败还继续往前走，等于「以为有退路」和「没有退路」两种状态跑在同一段
# 代码上，而且分不出来。真要豁免，就把 pg_dump 装不上的时候把备份步骤整个
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
log "  万一要退：cd $APP/backend && .venv/bin/python scripts/db_backup.py --restore --file ${DUMP}"
if ! ./.venv/bin/alembic upgrade head; then
  log "❌ 数据库迁移失败，保持现状不变（前端产物没切、没重启）"
  log "   库已回滚到迁移前状态；修好 revision 后重新发布即可"
  exit 1
fi
log "  schema 版本：$(./.venv/bin/alembic current 2>/dev/null | tail -1)"

log "5/7 切换代码与前端产物"
# 代码换名落位而不是就地覆盖：就地 mv 会在解压与替换之间留一个「代码在、
# 依赖不在」的窗口。替换本身用 tar 直接落到目标目录，失败时旧版本还在
# .releases 里可退。
tar -xzf "$STAGE/cwq-backend-${SHA}.tar.gz" -C "$APP/backend"
log "  代码已切到 ${SHA}"

# 前端：解压到暂存名，再**换名**换上。
#
# **这里踩过一个坑，值得写下来。** 直觉写法是
# ``mv "$STAGE/dist" "$APP/frontend/dist"`` —— 但目标 dist **已经存在**，
# ``mv`` 对已存在的目录是「移进去」而不是「替换掉」，于是产物落成
# ``dist/dist/index.html``，而根目录那份旧 index.html 还在原地。
# 脚本一路绿灯、服务照常起来、探活照常通过 —— 前端却还是旧的。
# 沙箱演练（deploy/tests/test_deploy.sh 第 7 组）就是为这条加的断言。
#
# 所以顺序必须是：旧的让开 → 新的顶上 → 再删旧的。
FRONT_IN="$APP/frontend/.dist.incoming"
FRONT_OLD="$APP/frontend/.dist.old"
rm -rf "$FRONT_IN" "$FRONT_OLD"
mkdir -p "$FRONT_IN"
tar -xzf "$STAGE/cwq-frontend-${SHA}.tar.gz" -C "$FRONT_IN"
if [[ ! -d "$FRONT_IN/dist" ]]; then
  die "前端产物里没有 dist/ —— 解压出来的东西不对。**前端产物没切、代码没重启。**"
fi
mv "$APP/frontend/dist" "$FRONT_OLD"
mv "$FRONT_IN/dist" "$APP/frontend/dist"
rm -rf "$FRONT_OLD" "$FRONT_IN"

log "6/7 重启服务"
systemctl restart cwq-api

# 探活**轮询**而不是「睡 3 秒打一发」。
# 原来这里是 ``sleep 3`` + 单次 ``curl -fsS``，失败就非零退出。问题是走到这
# 一行时「该发生的都已经发生了」：迁移已执行、产物已切换、服务已重启。
# 运维看到「发布失败」，而新版本其实已经在跑 —— 于是这个失败信号指向了一个
# 回滚不回去的状态。按失败处理（回滚）等于把正在服务的版本撤掉，不处理又
# 会以为线上还是旧版。所以这里轮询，把「是不是真失败」和「只是没起来」分开。
log "  等待 /api/health 应答（最多 30 秒）"
health_ok=0
for _ in $(seq 1 60); do
  curl -fsS -m 2 -o /dev/null http://127.0.0.1:8000/api/health 2>/dev/null && { health_ok=1; break; }
  sleep 0.5
done

if [ "$health_ok" -ne 1 ]; then
  log "❌ 发布后 30 秒内 /api/health 没有给出 2xx/3xx"
  log "   ⚠️  此时**新版本已经上线**：迁移已执行、产物已切换、服务已重启。"
  log "      这次发布不是「没发生」，不要直接当作「什么都没上线」。"
  log "   先分清是新版本起不来，还是探活口径不对："
  log "     journalctl -u cwq-api -n 200 --no-pager"
  log "     tail -50 /opt/cwq/backend/logs/api.err.log"
  log "   需要回滚时（人工确认后再做）：备份在 ${RELEASES}/${STAMP}"
  log "     产物也在 ${RELEASES}/${STAMP}/artifact（回滚不必重新下载）"
  exit 1
fi

log "  /api/health 应答"

# ---------------------------------------------------------------------------
# 7/7 发布后体检。三项都只提醒、不阻断 ——
# 它们各自都有**故意不硬失败**的理由，硬失败会把「发布失败」指向一个
# 其实不需要回滚的状态（与上面探活那段同一个顾虑）。
# ---------------------------------------------------------------------------
log "7/7 发布后体检"

# (1) 启用定时备份。
#
# 原来的备份只在发布流程里跑（上面第 4/7 步），也就是说
# **一个月不发布，那一个月就没有任何新备份** —— 而发布恰恰不是备份的理由。
# 迁移是最需要退路的时候，可退路的存在与否却取决于「最近有没有人发版」。
# 这里把启用动作放进发布脚本，是刻意的：**发布是运维唯一会来看日志的时刻**，
# 借它把定时器点上，比写一份没人执行的文档有用。
#
# 产物是 CI 打的 tar，deploy/systemd/ 不在其中，所以先看这台机器上有没有：
# 没有就把两条命令原样打出来（不猜路径、不静默跳过）。
enable_backup_timer() {
  local src=""
  for d in "$APP/deploy/systemd" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd"; do
    if [ -f "$d/cwq-backup.timer" ]; then src="$d"; break; fi
  done

  if [ -n "$src" ]; then
    mkdir -p /opt/cwq/backups
    # daemon-reload 必须排在 enable 之前，否则 systemd 认的还是旧缓存 ——
    # 首次安装时 unit 名不存在，enable 会报「unit not found」。
    cp "$src/cwq-backup.service" "$src/cwq-backup.timer" /etc/systemd/system/ \
      || return 1
    systemctl daemon-reload || return 1
  fi

  systemctl enable --now cwq-backup.timer
}

if enable_backup_timer >/dev/null 2>&1; then
  next_run=$(systemctl show cwq-backup.timer -p NextElapseUSecRealtime --value 2>/dev/null || echo "")
  log "  定时备份已启用${next_run:+（下次：${next_run}）}，备份目录 /opt/cwq/backups"
else
  log "  ⚠️  定时备份启用失败 —— 当前备份仍然只发生在发版时"
  log "     手动补一次（把 deploy/systemd/ 两个文件拷过去）："
  log "       mkdir -p /opt/cwq/backups"
  log "       cp deploy/systemd/cwq-backup.{service,timer} /etc/systemd/system/"
  log "       systemctl daemon-reload && systemctl enable --now cwq-backup.timer"
  log "     然后验证真的产出文件：systemctl start cwq-backup.service && ls -l /opt/cwq/backups/"
fi

# (2) TRUSTED_PROXIES 没配 → IP 维度的登录节流形同虚设
#
# 不配时后端按 fail-closed 处理：**忽略** X-Forwarded-For，
# 于是全站共用 127.0.0.1 一个节流桶。后果不是「多一道防护」变弱，
# 而是这个桶会被**全站所有人共用** —— 一个来源反复试错，
# 会把整个公司（都经由同一个 nginx）一起拖进节流窗口。
#
# 它与 nginx 侧的 proxy_set_header X-Forwarded-For 是**一对**：
# 只配一边等于没配（配了 app 侧而 nginx 不发头 → 全是直连 IP；
# nginx 发了头而 app 不配 → 头被忽略）。
if grep -qE '^[[:space:]]*TRUSTED_PROXIES[[:space:]]*=[[:space:]]*"?[[:space:]]*"?[[:space:]]*$' "$APP/backend/.env" 2>/dev/null; then
  log "  ⚠️  TRUSTED_PROXIES 是空的 —— IP 维度登录节流当前形同虚设（全站共用一个桶）"
  log "     配法见 deploy/tuning.md 第八节。改完要重启 cwq-api 才生效。"
else
  log "  TRUSTED_PROXIES 已配置"
fi

log "✅ 发布完成（${SHA}）"

# 暂存目录：产物已经解压进 ${APP}，这里只是下载暂存
rm -rf "$STAGE"

# 只保留最近 KEEP 份备份
ls -dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf
