#!/usr/bin/env bash
# 发布脚本。五个铁律：
#   1. 先验证再切流量 —— 产物取不到就不重启
#   2. 备份上一版，回滚是改回来而不是重新编译
#   3. 表结构变更只由 Alembic 负责，迁移前必须先备份数据库
#   4. 备份失败就不迁移
#   5. 依赖与迁移的依据必须是**这一版**的代码，不是上一版留在现网的文件
#
# 第 6 条铁律（2026-10-20 补）：
#   6. **切换必须原子，被打断必须能退回去** —— 换产物是一次 rename，
#      不是「旧的让开、新的顶上」两次 rename；脚本全程有 trap，
#      出事把代码与前端产物换回上一版。
#      详见下面「出事时的自动回退」那一段。
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
# 生产机只负责取产物 → 解包到暂存 → 装运行时依赖 → 迁移 → 切产物 → 重启。
#
# 顺序里的**「解包到暂存」是第 5 条铁律的兑现方式**，2026-10-18 之前它不存在，
# 于是装依赖与迁移读的都是现网里上一版留下的文件。详见第 3 步的说明。
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

# ---------------------------------------------------------------------------
# 脚本自身的目录：**必须在任何 cd 之前定死**。
# ---------------------------------------------------------------------------
# 为什么要专门记这个：后面要找 unit 模板（deploy/systemd/）时，用的曾经是
# ``$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd``。那个写法**在调用
# 时刻**才求值，而 `dirname` 拿到的是**相对路径**时（`bash ./deploy.sh`），
# `cd . && pwd` 落到的是**当时的工作目录** —— 脚本第 3 步 `cd "$APP"`、
# 第 4 步 `cd "$APP/backend"` 早就把 CWD 改掉了，于是它去找的是
# ``$APP/backend/systemd``（不存在）。
#
# 后果不是「unit 没装上」这么轻描淡写的一件事：
#   * 调度看门狗的 timer 装不上 —— 外部调度服务停掉时没有任何通知；
#   * **每日备份的 timer 在全新主机上装不上** —— 而它装不上时**不报错**
#     （老主机上旧 unit 还在，enable 照样成功），于是那台机器从上线起
#     就一直没有自动备份，直到有人手工发现。
#     2026-10-20 生产上就撞上了：发布日志说「定时备份已启用」，而
#     /etc/systemd/system 里那个 unit 的时间戳还是前一天的 —— 它压根没被
#     重装过，只是碰巧还在。
#
# 绝对路径调用时这个写法恰好是对的，所以沙箱（用绝对路径）一直是绿的，
# 缺陷只在生产上以相对路径调用时才现形。修法是**一次定死、提前取**：
# 此刻 CWD 还是调用者所在目录，`dirname ./deploy.sh` 解析出来的就是脚本
# 真正所在的地方。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP=${APP_DIR:-/opt/cwq}
# 服务账号：产物解压之后归位给谁。跟 unit 文件里的 User= 保持一致。
: "${SERVICE_USER:=cwq}"
# 定时备份的关停标记。存在即不启用（详见 enable_backup_timer 里的说明）。
: "${BACKUP_TIMER_OFF_MARKER:=$APP/.backup-timer-off}"
# unit 与备份的落地位置。参数化的理由不是洁癖：沙箱演练以非 root 身份跑，
# 往 /etc/systemd/system 与 /opt/cwq/backups 写都会 Permission denied，于是
# enable_backup_timer 直接 return 1 —— 而它以前没有任何断言，这个失败
# 一直没人看见。与 APP_DIR / ARTIFACT_DIR 同一套做法。
: "${SYSTEMD_UNIT_DIR:=/etc/systemd/system}"
: "${BACKUP_DIR:=/opt/cwq/backups}"
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

# ARTIFACT_REF 决定「取哪条线上的 CI 产物」。
#
# **它以前是个死变量。** 原先这里写死 ``:=main``，但下面 fetch_artifacts
# 里那两条 gh 命令**都没带 --branch / --ref**，于是 gh 取的是「仓库默认
# 分支的最近一次运行」。ARTIFACT_REF 只出现在一行 log 里 —— 设成什么、
# 甚至不设，取到的产物都一样。
#
# 后果不是取不到产物，而是**取到另一条线的产物却不报错**：默认分支刚跑完
# 一次 CI，而你要发的是别的分支，脚本会安安静静把默认分支那份装上去。
# 发布脚本里最不能出的错就是这种「装上了、但装的不是你要的那个」。
# （本仓库当时就没有 main 分支 —— 这个默认值从一开始就是错的。）
#
# 所以：值必须真的传进 gh 命令，且默认值不再写死。写死的分支名在换默认
# 分支时会静默指向错误的产物；这里从远端问出默认分支，问不出来就要求
# 操作者显式给，不猜。ARTIFACT_DIR 那条路不经过 gh，不需要这个值。
# 显式给 ARTIFACT_REF 就用它；否则从远端问默认分支。
# 只在走 gh 路径时才问：ARTIFACT_DIR 那条路压根不经过 gh，为了一个用不上的值
# 多打一次网络请求没有意义（离线环境还会平白卡住）。
if [[ -z "$ARTIFACT_DIR" ]]; then
  : "${ARTIFACT_REF:=$(gh repo view "$REPO" --json defaultBranchRef -q .defaultBranchRef.name 2>/dev/null || echo '')}"
fi

log() { echo "[$(date '+%F %T')] $*"; }

die() { echo "[$(date '+%F %T')] ❌ $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 出事时的自动回退（trap）
# ---------------------------------------------------------------------------
# 脚本原来**一个 trap 都没有**（``grep -n 'trap' deploy.sh`` 零命中）。
# 发布被打断时（SSH 断连、systemd TimeoutStartSec、Ctrl-C），脚本停在半路，
# 而现网停在半路的状态是：
#   * 前端：旧的让开了、新的还没顶上 → **整站 404**（不是「旧版」，是「没有」）；
#   * 后端：tar 解到一半 → 旧代码与新代码混在一起；
#   * 库：可能已经迁了一半。
# 下面这套 trap 把「停在哪一步」变成「回到哪一步」。
#
# **回退的范围是刻意划的**：代码与前端产物回退（无损、几秒），
# **库不回退**。库的回退是「把 dump 灌回去」，而 db_backup.py 的
# --restore 是**往一个空库里灌**（pg_dump 的 plain 格式不带 DROP，
# 往现有库上灌会撞 "relation already exists"）—— 真要回退必须先清空
# schema，那是一次不可逆的、会把备份之后的写入全部丢掉的操作，
# 不该由脚本在失败现场自动做。脚本只把该做的命令原样打出来。
#
# 回退到什么程度由 ROLLBACK_SCOPE 决定（每个阶段自己开/关）：
#   none     —— 不回退（服务已经起来了：新版本已经上线，回退是另一件事）
#   frontend —— 回退前端产物
#   code     —— 回退前端产物 + 后端代码
ROLLBACK_SCOPE=none
FRONT_PREV_LINK=""     # 切换前 dist 软链指向的相对路径（空 = 之前不是软链）
FRONT_PREV_DIR=""      # 切换前 dist 是真实目录时的落点（首次改造用）
DB_MIGRATED=0          # 迁移是否已经成功过（决定回退报告里怎么措辞）

# 原子换软链：一次 rename(2)，中间没有「目标不存在」的窗口。
#
# **不用 `ln -sfn`**：它是 unlink + symlink 两步，两步之间链接是空的 ——
# 正是这里要消灭的那种窗口。
# **不用裸 `mv`**：目标是**指向目录的软链**时，BSD 与 GNU 的 mv 都会把源
# 移进那个目录里（落成 dist/dist/index.html），而不是替换掉软链 ——
# 本脚本原来踩的就是这一类坑的同类。
# GNU 用 `mv -T`（--no-target-directory），BSD 用 `mv -h`：两者都是
# 「把源改名覆盖到目标上」，一次 rename，原子。
atomic_symlink_swap() {
  local target="$1" link="$2"
  local tmp="${link}.swap.$$"
  rm -f "$tmp"
  ln -s "$target" "$tmp"
  if mv -T "$tmp" "$link" 2>/dev/null; then
    return 0
  fi
  mv -h "$tmp" "$link"
}

rollback_frontend() {
  # 情形一：旧布局转换到一半（真实目录已经挪走、软链还没就位）
  if [[ -n "${FRONT_PREV_DIR:-}" && -d "${FRONT_PREV_DIR}" && ! -e "$APP/frontend/dist" ]]; then
    log "  ↩ 回退前端：把真实目录搬回原位（${FRONT_PREV_DIR}）"
    mv "${FRONT_PREV_DIR}" "$APP/frontend/dist" && FRONT_PREV_DIR=""
    return $?
  fi
  # 情形二：正常布局 —— 把软链指回上一版（同样是一次 rename，仍然原子）
  if [[ -n "${FRONT_PREV_LINK:-}" ]]; then
    log "  ↩ 回退前端：dist 软链指回 ${FRONT_PREV_LINK}"
    atomic_symlink_swap "${FRONT_PREV_LINK}" "$APP/frontend/dist"
    return $?
  fi
  return 0
}

rollback_code() {
  # 从本次快照里把后端代码换回上一版。
  #
  # **只换产物里有的那几样**（app/scripts/migrations/alembic.ini/
  # requirements.lock/pytest.ini）：.env、.venv、logs/、data/plugins
  # 是运行时状态，产物里根本没有，也不该被一次回退抹掉。
  local snap="${RELEASES:-}/${STAMP:-}/backend"
  [[ -n "${STAMP:-}" && -d "$snap" ]] || return 0
  log "  ↩ 回退后端代码：从 ${snap} 恢复"
  local p
  for p in app scripts migrations alembic.ini requirements.lock pytest.ini; do
    [[ -e "$snap/$p" ]] || continue
    rm -rf "${APP:?}/backend/$p"
    cp -a "$snap/$p" "$APP/backend/$p"
  done
  return 0
}

on_exit() {
  local rc=$?
  # 成功退出、或本阶段明确不回退 → 原样退出，别碰现网。
  [[ $rc -eq 0 || "${ROLLBACK_SCOPE}" == "none" ]] && return $rc
  # 回退过程本身不许再失败：trap 里的一次失败会把原始退出码吃掉，
  # 而「发布失败」这个信号比「回退也失败」更重要。
  set +e
  trap - EXIT
  log "❌ 发布中断（退出码 ${rc}）—— 开始回退"
  rollback_frontend || log "  ⚠️ 前端回退失败：$APP/frontend/dist 现在是 $(readlink "$APP/frontend/dist" 2>/dev/null || echo '<不存在>')"
  if [[ "$ROLLBACK_SCOPE" == "code" ]]; then
    rollback_code || log "  ⚠️ 后端代码回退失败"
  fi
  if [[ "$DB_MIGRATED" == "1" ]]; then
    log "  ⚠️ 库**已经迁到新版本**（迁移成功过），**没有回退**。"
    log "     库的回退是「清空 schema 再把 dump 灌回去」，会丢掉备份之后的写入，"
    log "     不该由脚本在失败现场自动做。"
    log "     代码与前端已换回上一版，而**上一版代码拒绝在新 schema 上启动**"
    log "     （启动闸门 fail-closed）—— **不要直接重启服务**。"
    log "     两个出路："
    log "       1) 修好之后重新发布一次（迁移幂等，alembic 会从当前 revision 接着走）—— 推荐"
    log "       2) 回退库（先清空 schema，会丢掉备份之后的写入）："
    log "          cd $APP/backend && .venv/bin/python scripts/db_backup.py --restore --file ${RELEASES:-<无>}/${STAMP:-<无>}/database-before-migration.sql"
  fi
  log "   现场保留在 ${RELEASES:-<无>}/${STAMP:-<无>} 与 ${APP}/.incoming.*"
  exit $rc
}
trap on_exit EXIT
# INT/TERM 转成退出码，让 EXIT trap 统一处理（默认行为是直接死掉，
# trap 一次都不会跑 —— 那正是「停在一半」的原因）。
trap 'exit 130' INT
trap 'exit 143' TERM

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

  # 下面这两处 die 不是走过场：REPO 为空说明 /opt/cwq 不是 git 仓库且没给
  # REPO，ARTIFACT_REF 为空说明问不出默认分支也没显式给。两种情况都只能靠
  # 猜，而猜错的代价是「装上别的分支的产物且不报错」，所以在这里停。
  [[ -n "$REPO" ]] || die "REPO 为空，gh 不知道该去哪个仓库。
   给 REPO=owner/repo，或改用 ARTIFACT_DIR=... 人工投放产物。"
  [[ -n "$ARTIFACT_REF" ]] || die "ARTIFACT_REF 为空，问不出默认分支。
   显式给 ARTIFACT_REF=<分支名>，或改用 ARTIFACT_DIR=... 人工投放产物。"

  log "  从 GitHub Actions 拉取 ${ARTIFACT_REF} 分支的产物"
  # ``--repo`` 显式给，不依赖当前目录是不是 git 仓库 —— 部署目录可能只是
  # 一个被 rsync 过来的产物，没有 .git，那 gh 会因为认不出仓库而失败。
  #
  # ``--branch`` 必须给：不给就是「默认分支的最近一次运行」，也就是上面
  # 注释里说的「装上别的分支的产物」。这里取「最近一次运行」而不是「最近
  # 一次**成功**」是有意的：若那次 build-artifacts 失败了，取产物就会失败
  # 并停在发布之前；改成挑「最近一次成功」则会在失败时静默装上更旧的版本,
  # 那是更难发现的一种错。
  local head_sha
  head_sha=$(gh run view --repo "$REPO" --branch "$ARTIFACT_REF" \
               --json headSha -q .headSha) \
    || die "在 ${ARTIFACT_REF} 分支上找不到 CI 运行记录。
   先确认这次提交已经推上去、且 Actions 跑过。**没有任何东西被改动。**"
  ( cd "$dest" && gh run download --repo "$REPO" --name "cwq-deploy-${head_sha}" ) \
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
# 回滚快照目录收成 700：里面躺着全量账务数据（备份的 dump、上一版的源码、
# 产物包）。默认 755 意味着这台机器上任何本地账号都能列目录、读文件 ——
# 而这是台多站点共用的机器，其他站点的账号也在上面。
install -d -m 700 "$RELEASES"
install -d -m 700 "$RELEASES/$STAMP"
for d in backend frontend/dist; do
  if [[ -L "$APP/$d" ]]; then
    # **dist 是软链**（版本化布局，见第 5 步）：备份的必须是它**指向的那份
    # 真实内容**，而不是软链本身。``cp -a`` 会原样复制软链，于是快照里
    # 躺着一个指向 releases/ 的悬空链接 —— 看着像备份了，回滚时才发现
    # 什么都没有。
    real=$(cd "$APP/$d" 2>/dev/null && pwd -P) || real=""
    if [[ -n "$real" && -d "$real" ]]; then
      cp -a "$real" "$RELEASES/$STAMP/$(basename "$d")"
    else
      log "  ⚠️  $APP/$d 是软链但指不到真实目录，跳过备份这一项"
    fi
  elif [[ -e "$APP/$d" ]]; then
    cp -a "$APP/$d" "$RELEASES/$STAMP/$(basename "$d")"
  fi
done
# 产物本身也存一份：回滚时不必重新下载，产物被清理掉也还能退回去
cp -a "$STAGE" "$RELEASES/$STAMP/artifact"
echo "  已备份到 ${RELEASES}/${STAMP}"

# --------------------------------------------------------------------------
# 3/7 解包新代码到暂存目录，并按**这一版**的锁文件装运行时依赖
# --------------------------------------------------------------------------
# **这个暂存目录以前不存在，而它就是 2026-10-18 那次发布失败的根因。**
#
# 原来这里是直接在 ``$APP/backend`` 里 ``pip install -r requirements.lock``，
# 下一步直接在同一个目录里 ``alembic upgrade head``。两者读的都是
# **上一版留在现网的文件**：锁文件是旧的，于是这一版新增的依赖（当时是
# rfc8785）压根没装；迁移链也是旧的，于是 alembic 的 head 停在 0021，
# 而新代码要求 0027。两步都显示成功，切到新代码之后服务才炸：
#
#     ModuleNotFoundError: No module named 'rfc8785'
#     SchemaVersionError: 数据库停在 0021_youyou_chart_of_accounts，
#                         代码要求 0027_plugin_imports
#
# **凡是一次发布同时带来新依赖和新迁移，这个缺陷必然触发**，不是偶发。
#
# 修法不是把这两步整体往后挪一格 —— 往后挪确实能让它们读到新代码，
# 但顺序仍在「切产物」之前，于是迁移一失败就留下「库已迁移、代码没换」
# 这个最糟的状态（正在跑的是旧代码，库已经是新的）。
# 所以这里先把新代码解到暂存目录：**装依赖与迁移都以暂存目录里的新代码
# 为依据，而现网目录在第 5 步之前一个字节都不动。** 失败时留下的仍是
# 「库没动、代码没换、服务没重启」，与本脚本原有的失败语义一致。
log "3/7 解包新代码到暂存目录，并按这一版的锁文件装运行时依赖"
VENV="$APP/backend/.venv"
INCOMING="$APP/.incoming.$$"
# 与 $STAGE 一样带 pid；失败时**不删** —— 出事时要能翻看「新代码到底长什么样」。
# 走完全流程时随 $STAGE 一起清掉。
rm -rf "$INCOMING"
# 权限先收到 700 再往里放东西：紧接着就会给它加一个指向 .env 的软链，
# 而这台机器上还跑着别的站点，不该在任何一个瞬间留一条通往密钥的路径。
mkdir -p "$INCOMING"
chmod 700 "$INCOMING"
# --no-same-owner 与第 5 步同一理由：不让归档里 CI runner 的 uid 落盘。
# 暂存目录是 root 建的临时目录，chown 归位在第 5 步做（产物会再解一次到现网）。
tar --no-same-owner -xzf "$STAGE/cwq-backend-${SHA}.tar.gz" -C "$INCOMING"

# 迁移必须打在**现网那个库**上，而连接串是 pydantic 按 **cwd** 找 .env 的
# （app/core/config.py 的 ``SettingsConfigDict(env_file=".env")``，相对路径）。
# 从暂存目录跑 alembic 时它读不到 ``$APP/backend/.env``，会一路退回源码里
# 写死的默认值 ``localhost:15432/cwq`` —— 那是本脚本最不能犯的错：
# 把迁移打到另一个库上，破坏性变更不可挽回（backend/alembic.ini 开头的注释
# 说的就是这件事）。
#
# 所以这里显式把现网的 .env 链过去：**一份真源**，不复制、不生成、不猜。
if [[ -f "$APP/backend/.env" ]]; then
  # 这一处 ln -sfn 与前端的换软链**不是一回事**：这里链的是暂存目录里
  # 的一次性 .env（给 alembic 读），不是对外服务的路径，没有「链接为空
  # 的窗口」那种风险。前端那次换软链必须原子，见 atomic_symlink_swap。
  ln -sfn "$APP/backend/.env" "$INCOMING/.env"
elif [[ -z "${DATABASE_URL:-}" ]]; then
  die "找不到 ${APP}/backend/.env，环境里也没有 DATABASE_URL。
   **无法确认迁移会打在哪个库上，因此不迁移。**
   现网代码、前端产物与服务均未改动，线上仍是上一版。"
fi

# 装依赖。**关键在这一行指向的是哪份锁文件** —— 「venv 装完了就等于装对了」
# 这个说法不够，锁文件是旧的时装得再成功也是错的（新依赖根本没被要求安装）。
# 解释成「装的是新锁」才是真的，装到哪一版只有锁文件本身能证明。
#
# venv 仍然用现网那个：.venv 不在产物里（build-artifacts 显式 --exclude），
# 也不该在 —— 换掉它等于让「测试时那一版」与「运行时那一版」又对不上。
if [[ ! -f "$INCOMING/requirements.lock" ]]; then
  die "产物里缺 requirements.lock —— 没有锁文件就无法保证「装出来的就是测试时那一版」。
   **现网代码、前端产物与服务均未改动。**"
fi
# ``--no-cache-dir``（2026-10-24 性能审计补上）：
#
# pip 默认把下载的 wheel 缓存在 ``~/.cache/pip``，**那个目录永不清**。
# 两次发版之间它的体积会一路涨，而它涨的是**磁盘**，不是内存 ——
# 但在小盘上这与日志占盘是同一类问题。
#
# 真正吃内存的是 pip **解析**依赖时的峰值 RSS（200~400 MB 量级），
# 而这台机只有 1.6 GB、还同时跑着现网服务。``--no-cache-dir`` 不影响解析
# 峰值，但它去掉的是「缓存无限涨盘」这一条 —— 那一条是可以零成本去掉的。
if ! "$VENV/bin/pip" install -q --no-cache-dir -r "$INCOMING/requirements.lock"; then
  log "❌ 装运行时依赖失败"
  log "   现网代码、前端产物与服务**均未改动**，线上仍是上一版在跑"
  log "   常见原因是锁文件里有这台机器装不上的包；修好后重新发布即可"
  log "   新代码留在 ${INCOMING}，可以先翻看 requirements.lock 与本机环境的差异"
  exit 1
fi
log "  已按产物里的 requirements.lock 装完运行时依赖"

# --------------------------------------------------------------------------
# 4/7 备份数据库并执行迁移（迁移链同样取自暂存目录）
# --------------------------------------------------------------------------
# 表结构变更只由 Alembic 负责（P3-01）。
# 原来这里跑的是「应用启动时 create_all」，而 create_all 只建缺失的表、
# 不改已存在的表 —— 生产上加一列 NOT NULL 会静默不生效，服务照常启动，
# 问题到运行时才炸。现在由 CI 产出的迁移 + 这里的 upgrade head 负责。
#
# **备份仍然用现网的 db_backup.py，不跟发布版本走。** 这份 dump 是迁移的
# 唯一退路，而退路本身不该由「正在发布的那一版」来决定能不能用：产物里那
# 份 db_backup.py 在这台机器上从没跑过，用一份没验证过的脚本去生成唯一的
# 退路，等于把退路的可靠性押在发布版本上。现网这一份是一直在用的那份。
log "4/7 备份数据库并执行迁移"
cd "$APP/backend"
DUMP="$RELEASES/$STAMP/database-before-migration.sql"
# 备份失败**必须中止**（P2-25）。
# 原来这里是「备份失败只喊一句，迁移照跑」。但备份是这个系统唯一的退路 ——
# 凭证号可回收、审计快照不可再生，迁移把表结构改坏之后能救命的只有这份 dump。
# 退路失败还继续往前走，等于「以为有退路」和「没有退路」两种状态跑在同一段
# 代码上，而且分不出来。真要豁免，就把 pg_dump 装不上的时候把备份步骤整个
# 去掉并让发布失败，不要留一个「记得看一眼日志」的分支 —— 没人会看。
# umask 077：dump 是全量账务数据，644 等于把这台机器上所有本地账号
# （包括同机其他站点的）都变成合法读者。用子 shell 圈住，避免把 umask
# 泄漏到后面的解压/切产物上 —— 那边的目录权限由 chown_if_possible 负责。
if ( umask 077; ./.venv/bin/python scripts/db_backup.py > "$DUMP" 2>"$RELEASES/$STAMP/db_backup.err" ); then
  log "  数据库已备份到 ${DUMP}（$(wc -l < "$DUMP") 行）"
else
  log "❌ 数据库备份失败，见 ${RELEASES}/${STAMP}/db_backup.err"
  log "   没有退路就不迁移：生产库的 schema 一个字节都没动，保持现状不变"
  log "   现网代码、前端产物与服务也都没有改动"
  log "   修好备份（装 postgresql-client，或核对 DATABASE_URL）后重新发布"
  log "   新代码留在 ${INCOMING}"
  exit 1
fi
# 恢复命令一并打出来：出事时要靠它，而临场想起来的命令一定是错的
log "  万一要退：cd $APP/backend && .venv/bin/python scripts/db_backup.py --restore --file ${DUMP}"

# 迁移在**暂存目录**里跑。两条配置各自负责一半，缺一不可：
#   ``script_location = %(here)s/migrations`` 里的 ``%(here)s`` 是
#     alembic.ini 所在目录 —— 迁移链来自这一版；
#   ``prepend_sys_path = .`` 把 ``app`` 包也解析到这一版 ——
#     迁移脚本里 ``from app...`` 拿到的是新代码而不是旧的。
# 于是 head 是「这一版要求的 head」，而不是「上一版知道的最大 head」。
#
# ---------------------------------------------------------------------------
# **为什么迁移仍然排在切产物之前（这是有意的，不是疏忽）**
# ---------------------------------------------------------------------------
# 顺序反过来（先切代码、再迁移）并不会让「库与代码不一致」消失，只会让
# 它换一个形状出现：切完代码但还没迁移时，磁盘上是新代码、库里是旧
# schema，而跑的仍是上一版 —— 那时按新代码的启动闸门去重启会直接被拒
# （schema_version.py 是 fail-closed 的：库比代码旧 → 拒绝启动）。
#
# **真正决定顺序的是启动闸门**：新代码要求库停在 head，新代码启动前
# 数据库必须已经是 head。所以库必须先到位。代价是「库已迁移、代码没换」
# 这个窗口存在 —— 脚本对此的处理不是假装它不存在，而是：
#   1. 失败时**如实说明**当前处在什么状态（见下面的失败分支）；
#   2. 给出两条明确的出路：重新发布（迁移幂等，会接着往下走）
#      或回退库（不可逆、会丢写入，所以只打印命令，不自动做）；
#   3. 之后任何一步失败，trap 把代码与前端产物换回上一版（见文件头
#      「出事时的自动回退」），所以现网不会停在「新旧混在一起」的状态。
#
# 「库已迁移、代码没换」不是本脚本能消灭的状态 —— 消灭它需要停服迁移
# 或多阶段发布，那是另一个量级的改动。这里做的是：**窗口尽量短、
# 状态如实、自动退回到可重发的状态**。
if ! ( cd "$INCOMING" && "$VENV/bin/alembic" upgrade head ); then
  log "❌ 数据库迁移失败"
  log "   现网代码、前端产物与服务**都未改动**（线上跑的仍是上一版）"
  log "   **但库可能已经迁了一半** —— 这是本次发布唯一动过的东西"
  log "     退路在 ${DUMP}"
  log "     cd $APP/backend && .venv/bin/python scripts/db_backup.py --restore --file ${DUMP}"
  log "   多数情况下**不需要动库**：迁移是幂等的，修好之后重新发布一次，"
  log "   alembic 会从当前 revision 接着往下走。"
  log "   修好 revision 后重新发布即可；新代码留在 ${INCOMING}"
  exit 1
fi
DB_MIGRATED=1
log "  schema 版本：$( cd "$INCOMING" && "$VENV/bin/alembic" current 2>/dev/null | tail -1 )"

chown_if_possible() {
  # 非致命：属主是卫生问题，不是正确性闸门。
  #
  # 它**必须**非致命，原因有二。其一，CI 的沙箱演练是以非 root 的 runner 身份
  # 跑的，那里 chown 本来就会失败；让它致命，等于把「只有 root 才能做的事」
  # 变成「整条发布脚本在 CI 上必红」。其二，这一步排在迁移**之后**，而
  # deploy.sh 自己的原则是「迁移后任何一步失败都不切产物」—— 属主不对就中止，
  # 留下的正是「库已迁移、代码没换」这个最糟的状态。代码 755 可读，服务照常跑，
  # 所以正确做法是把话说清楚，而不是在这里把发布掐死。
  if chown -R "$SERVICE_USER:$SERVICE_USER" "$1" 2>/dev/null; then
    log "  属主已归位到 ${SERVICE_USER}"
  else
    log "⚠️  属主未归位到 ${SERVICE_USER}（当前身份无权 chown）"
    log "   服务不受影响（代码 755 可读），但这不是正确的终态。补一句即可："
    log "     chown -R ${SERVICE_USER}:${SERVICE_USER} $1"
  fi
}

log "5/7 切换代码与前端产物"
# 走到这一步，说明依赖已按新锁装好、schema 已按新迁移链升到位。
# **这两件事的依据都在暂存目录里，不在现网** —— 所以现在才是第一次
# 动现网代码的时刻，前面任何一步失败都不会走到这里。
#
# 这里仍然把后端 tar 直接解到目标目录，而不是拿暂存目录换名顶上：
# 产物里只有 app/scripts/migrations/alembic.ini/requirements.lock/pytest.ini，
# **不含** .env、.venv、logs/ 和 data/plugins（build-artifacts 显式 --exclude，
# .env 与 .venv 本来就不在仓库里）。整目录换名会把这些运行时状态一起换掉 ——
# 密钥、虚拟环境、插件运行目录，正是最不该被一次发布顺手抹掉的东西。
# 解包覆盖则天然只写产物里有的那几个路径。
tar --no-same-owner -xzf "$STAGE/cwq-backend-${SHA}.tar.gz" -C "$APP/backend"
# **归档里记的是 CI 的构建身份，不是部署机的服务账号。** build-artifacts 在
# GitHub Actions 里打包，tar 逐条记下当时的 ``runner/runner``；而这一步是以
# root 跑的，GNU tar 默认**还原归档里的数字 uid/gid**。那个号落到部署机上
# 未必对应服务账号 —— 110.42.44.53 上 uid 1001 恰好是 ``mysql``，于是
# /opt/cwq/backend/app 一夜之间变成 mysql:mysql。
#
# 后果不是服务起不来：755 之下服务照样读得到、跑得起来、/api/health 也过。
# 是**代码不再属于服务账号** —— Python 写不了 __pycache__，日后任何收紧权限
# 的动作都会在最不该炸的时候炸。与 unit 里的 %F、LimitNOFILESoft 同一类：
# 看着成功，实则不对。所以这里 --no-same-owner 解压（不让归档改属主），
# 再显式 chown 给 SERVICE_USER。
chown_if_possible "$APP/backend"
# 从这里起，出事就要把代码换回上一版（见文件头「出事时的自动回退」）。
ROLLBACK_SCOPE=code
log "  代码已切到 ${SHA}"

# ---------------------------------------------------------------------------
# 前端产物：**版本化目录 + 一次 rename 换软链**
# ---------------------------------------------------------------------------
# **原来这里是两次 mv，中间 dist 整个不存在。**
#
#     mv "$APP/frontend/dist" "$FRONT_OLD"     # 旧的让开
#     mv "$FRONT_IN/dist" "$APP/frontend/dist" # 新的顶上
#
# 两次 rename(2) 之间，``$APP/frontend/dist`` **不存在**（新的还在暂存目录，
# 旧的已改名）。nginx 的 ``root /opt/cwq/frontend/dist;``（cwq.conf:161、
# acct.xins.live.conf:123）在这段时间里对**所有静态资源**返回 404 ——
# 不是「旧版」，是「没有」。脚本被 SIGINT/SIGTERM 打断（SSH 断连、
# systemd TimeoutStartSec）恰好落在两行之间就是整站白屏，而且**没有任何
# 自动恢复路径**：旧的那份躺在 $FRONT_OLD 里没人管，脚本也不会把它移回去。
#
# **现在改成一次 rename。** 布局：
#
#     $APP/frontend/releases/dist-<sha>-<时间戳>/   真正的前端内容（一份一版）
#     $APP/frontend/dist -> releases/dist-<sha>-<时间戳>   nginx 的 root 指这里
#
# 切换 = 把 ``dist`` 这个软链指向新的 release 目录，**一次 rename(2)**：
# rename 原子替换软链本身（不跟随软链、不把它当目录），中间没有
# 「目标不存在」的窗口。nginx 的 root 路径**不变**，所以这次改造
# **不需要 reload nginx**，也不需要运维改任何配置。
#
# 顺带解决两件事：
#   * 回滚变成一次 rename（指回上一版 release），而不是「把删掉的东西
#     捡回来」；
#   * 上一版的静态资源**还在盘上**（旧 release 目录保留到清理为止），
#     所以回退后旧前端引用的 /assets/* 仍然打得开 —— 旧布局下那些文件
#     在切换那一刻就没了，回滚回去也是一堆 404。
FRONT_ROOT="$APP/frontend"
FRONT_RELEASES="$FRONT_ROOT/releases"
FRONT_DIST="$FRONT_ROOT/dist"
FRONT_IN="$FRONT_ROOT/.dist.incoming"

rm -rf "$FRONT_IN"
mkdir -p "$FRONT_IN" "$FRONT_RELEASES"
tar --no-same-owner -xzf "$STAGE/cwq-frontend-${SHA}.tar.gz" -C "$FRONT_IN"
if [[ ! -d "$FRONT_IN/dist" ]]; then
  die "前端产物里没有 dist/ —— 解压出来的东西不对。**前端产物没切、代码没重启。**"
fi
# 理由同后端那一步：--no-same-owner 挡住 CI runner 的 uid，chown 把属主
# 显式交给服务账号。nginx 以 root 读它，两种属主都读得到，但只有后者是对的。
chown_if_possible "$FRONT_IN/dist"
# **目录名带 sha 也带时间戳**，不是只有 sha。
#
# 理由：同一份产物可能被**再发一次**（上次发布失败了，运维重跑）。
# 如果目录名只有 sha，第二次发布开头的 `rm -rf` 会把**当前正在服务的那份**
# 删掉 —— 而那时切换还没发生，dist 软链就指着一个不存在的目录 →
# 整站 404，而且这次发布要是又失败在前半段，站点就一直白着。
# 带时间戳之后，每次发布都落到一个**新目录**，旧的那份永远不动。
NEW_RELEASE="$FRONT_RELEASES/dist-${SHA}-${STAMP}"
rm -rf "$NEW_RELEASE"
# 同分区内改名：先让新内容在 releases/ 下**完整就位**，最后一步才换软链。
# 这一步是 rename(2)，不是复制 —— 所以不存在「拷到一半被切上去」的状态。
mv "$FRONT_IN/dist" "$NEW_RELEASE"

# 记下切换前的指向（回退要用）
if [[ -L "$FRONT_DIST" ]]; then
  FRONT_PREV_LINK="$(readlink "$FRONT_DIST")"
elif [[ -d "$FRONT_DIST" ]]; then
  # **旧布局首次转换**：dist 是真实目录。目录没法被软链原地替换
  # （rename 覆盖一个目录会失败），所以这里必须「旧的让开、新的顶上」——
  # 全流程里**唯一**还存在窗口的地方，只有两次 rename 之间那一下，
  # 而且由 trap 兜底：被打断就把旧目录搬回原位。
  LEGACY_RELEASE="$FRONT_RELEASES/dist-legacy-${STAMP}"
  mv "$FRONT_DIST" "$LEGACY_RELEASE"
  FRONT_PREV_DIR="$LEGACY_RELEASE"
  FRONT_PREV_LINK="releases/$(basename "$LEGACY_RELEASE")"
  log "  旧布局已转换为版本化目录（dist -> ${FRONT_PREV_LINK}）"
fi

# **原子换软链**：一次 rename，中间没有「目标不存在」的窗口。
atomic_symlink_swap "releases/$(basename "$NEW_RELEASE")" "$FRONT_DIST"
rm -rf "$FRONT_IN"
log "  前端产物已切到 ${SHA}（dist -> releases/$(basename "$NEW_RELEASE")）"

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
  # **这一步不回退。** 新版本已经起来了（迁移已执行、产物已切换、服务已
  # 重启），把它撤掉不是「回滚」，是「把正在跑的版本换掉」—— 而新版本
  # 可能只是慢、可能只是探活口径不对。所以明确关掉自动回退，让 trap
  # 不动现网，改为把现场与处置方式说清楚。
  ROLLBACK_SCOPE=none
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

# 主探活成功不代表插件 worker barrier 已完成；单独核验后才报告发布成功。
# 失败时新版本已经启动，保留现场、日志与备份，不能盲目反向迁移。
if ! (cd "$APP/backend" && .venv/bin/python scripts/plugin_status.py --wait 30); then
  ROLLBACK_SCOPE=none
  log "❌ 主服务存活，但插件 worker barrier 未通过；请检查插件状态与 api.err.log。"
  log "   新版本已启动；可关闭 PLUGIN_ENABLED 并重启以恢复核心服务，保留插件目录和表。"
  exit 1
fi

# 到这里新版本已经确认可用：**自动回退就此关闭**。此后脚本里的任何失败
# 都不该把已经在跑的版本换掉 —— 那是「发布后体检」那一段的语义。
ROLLBACK_SCOPE=none

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
#
# ---------------------------------------------------------------------------
# unit 按本机布局渲染（2026-10-19）
#
# **以前这里是原样 `cp`，而模板里的路径是写死的 /opt/cwq。** 这不是洁癖问题，
# 它是一个每次发版都会重演的生产故障：`APP_DIR` / `BACKUP_DIR` 从来就是可配的
# （见文件头部），但原样拷贝会把 /opt/cwq 一起灌到这台机器上，于是
# **任何一次手工修对的路径，都会在下一次发版被打回模板里的旧值**。
# 2026-10-19 在生产上就是这个形态：手工修好的 cwq-backup.* 被 00:05 那次发布
# 原样覆盖回 /opt/cwq，timer 随后每天报
# ``Failed to load environment files: No such file or directory``。
#
# 替换只有两个前缀，且**必须先长后短**（/opt/cwq/backups 在 /opt/cwq 之前）——
# 反过来的话第一步就把更长的那个吃掉了，备份目录会被替换成
# `$APP/backups` 而不是运维给定的 `$BACKUP_DIR`。这正是这两条路径要分开配的原因。
#
# 替换值来自环境变量，而路径里可能有 `&` `|` `\`（sed 的特殊字符），所以先转义。
# 未定义的变量在 ``set -u`` 下会直接让发布失败 —— 那正是它该有的形态：
# 渲染不出正确路径时宁可不装，也不要装一个指向不存在目录的 unit。
# ---------------------------------------------------------------------------
sed_repl_escape(){ printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'; }

render_unit(){
  local src="$1" dst="$2"
  sed -e "s|/opt/cwq/backups|$(sed_repl_escape "$BACKUP_DIR")|g" \
      -e "s|/opt/cwq|$(sed_repl_escape "$APP")|g" \
      "$src" > "$dst"
}

enable_backup_timer() {
  # 关停标记（2026-10-17 起）。存在即跳过，且**连 unit 都不复制**。
  #
  # 为什么不直接 systemctl stop 就完事：这一步在**每次发布**末尾都会跑
  # ``enable --now``，也就是说任何一次发版都会把 timer 重新拉起来；只 stop
  # 的话它会在下一次发布后自己复活，而且日志上显示「定时备份已启用」——
  # 正是那种「关不掉还以为关掉了」的状态。所以关停必须是**发布脚本认得的
  # 一个显式状态**，而不是一次性的动作。
  #
  # 为什么不复制 unit：只 stop 不 disable 的话服务器重启后 timer 照样起来。
  # 跳过复制 + 已 disable 才是完整的关停。
  if [ -f "$BACKUP_TIMER_OFF_MARKER" ]; then
    return 2
  fi

  local src=""
  for d in "$APP/deploy/systemd" "$SCRIPT_DIR/systemd"; do
    if [ -f "$d/cwq-backup.timer" ]; then src="$d"; break; fi
  done

  if [ -n "$src" ]; then
    mkdir -p "$BACKUP_DIR"
    # 渲染而不是 cp —— 理由见 render_unit 上面的注释。
    # daemon-reload 必须排在 enable 之前，否则 systemd 认的还是旧缓存 ——
    # 首次安装时 unit 名不存在，enable 会报「unit not found」。
    local u
    for u in cwq-backup.service cwq-backup.timer; do
      render_unit "$src/$u" "$SYSTEMD_UNIT_DIR/$u" || return 1
    done
    systemctl daemon-reload || return 1
  fi

  systemctl enable --now cwq-backup.timer
}

# ---------------------------------------------------------------------------
# 离站备份（阿里云 OSS）的定时器
# ---------------------------------------------------------------------------
# **这个 timer 以前从来没人装。** deploy/systemd/cwq-oss-backup.{service,timer}
# 在 3ae13c1（账套数据分级压缩备份到 OSS）就进了仓库，但 enable_backup_timer
# 只渲染并启用 cwq-backup.{service,timer} —— 离站备份那套单元从来没有被复制到
# /etc/systemd/system。后果是**生产上离站备份从来没排过程**：只有发版时和
# 手工触发才会备份到 OSS。
#
# 后果比「少一个 timer」严重。本机 dump（cwq-backup.timer）把库写成同一台机器
# 磁盘上的一个 .sql —— 机器整盘故障时，它和账套数据一起没了。离站备份是唯一
# 能跨机器故障活下来的那个副本，而它没跑。于是「我们有备份」这句话一直只
# 在同一块盘上成立。
#
# 什么时候不装：OSS 参数没配全时不装（每天跑一次、每天失败、没人看 journal
# —— 那比没有定时备份更危险，因为它看起来是有的）。此时给运维留一条能直接
# 抄的命令。
enable_oss_backup_timer() {
  # 关停标记：与本机 dump 同一套语义 —— 一个标记关掉**所有**定时备份。
  # 理由同 enable_backup_timer：只 stop 不 disable 的话，下次发版它自己复活。
  if [ -f "$BACKUP_TIMER_OFF_MARKER" ]; then
    return 2
  fi

  local src=""
  for d in "$APP/deploy/systemd" "$SCRIPT_DIR/systemd"; do
    if [ -f "$d/cwq-oss-backup.timer" ]; then src="$d"; break; fi
  done
  # 产物里没有这两个单元：不算失败（返回码 3），但也没装上。
  [ -n "$src" ] || return 3

  mkdir -p "$BACKUP_DIR"
  local u
  for u in cwq-oss-backup.service cwq-oss-backup.timer; do
    render_unit "$src/$u" "$SYSTEMD_UNIT_DIR/$u" || return 1
  done
  systemctl daemon-reload || return 1

  # OSS 参数配全了吗？**用应用自己的入口问，不另写一套判定。**
  # backup_to_oss.py --dry-run 会先 require_ready()：缺项就退出码 2，
  # 且一个字节都不传上去。这条命令在沙箱里是桩（退出 0），在真机上是真的。
  local state="INCOMPLETE"
  if state=$( cd "$APP/backend" && "$VENV/bin/python" scripts/backup_to_oss.py --dry-run >/dev/null 2>&1 ); then
    state="OK"
  fi
  if [ "$state" != "OK" ]; then
    return 5
  fi

  systemctl enable --now cwq-oss-backup.timer
}

# 调度看门狗的关停标记。存在即跳过（与定时备份同一个道理：只 stop 的话
# 下一次发版会把它自己拉回来）。
#
# **用 `: "${VAR:=默认}"` 而不是 `VAR=默认`**：后者会覆盖运维从环境里传进来的
# 值，而前者是"没传才用默认"。另外 `VAR:=默认` 这种写法在 bash 里**不是
# 赋值语句**（`:=` 只在 `${...}` 展开里才有意义），会被当成命令去执行，
# 报一句莫名其妙的 "No such file or directory"。
: "${WATCHDOG_TIMER_OFF_MARKER:=$APP/.watchdog-timer-off}"

enable_watchdog_timer() {
  # 关停标记：存在即跳过，且**连 unit 都不复制**。理由同 enable_backup_timer。
  if [ -f "$WATCHDOG_TIMER_OFF_MARKER" ]; then
    return 2
  fi

  local src=""
  for d in "$APP/deploy/systemd" "$SCRIPT_DIR/systemd"; do
    if [ -f "$d/cwq-scheduler-watchdog.timer" ]; then src="$d"; break; fi
  done

  if [ -n "$src" ]; then
    local u
    for u in cwq-scheduler-watchdog.service cwq-scheduler-watchdog.timer; do
      render_unit "$src/$u" "$SYSTEMD_UNIT_DIR/$u" || return 1
    done
    systemctl daemon-reload || return 1
  fi

  systemctl enable --now cwq-scheduler-watchdog.timer
}

backup_timer_rc=0
enable_backup_timer >/dev/null 2>&1 || backup_timer_rc=$?

if [[ "$backup_timer_rc" -eq 2 ]]; then
  # 退出码 2 = 按要求保持关闭，不是失败。必须和下面的失败分支分开说，
  # 否则运维看到 ⚠️ 会去「修」一个根本没坏的东西。
  log "  定时备份按要求保持关闭（标记文件：${BACKUP_TIMER_OFF_MARKER}）"
  log "     发版时的库备份（第 4 步）不受影响，仍在执行 —— 那是迁移前的退路"
  log "     要恢复：systemctl enable --now cwq-backup.timer && 删掉那个标记文件"
elif [[ "$backup_timer_rc" -eq 0 ]]; then
  next_run=$(systemctl show cwq-backup.timer -p NextElapseUSecRealtime --value 2>/dev/null || echo "")
  log "  定时备份已启用${next_run:+（下次：${next_run}）}，备份目录 ${BACKUP_DIR}"
else
  log "  ⚠️  定时备份启用失败 —— 当前备份仍然只发生在发版时"
  log "     手动补一次。**不要直接 cp 模板**（会带着 /opt/cwq 回到这台机器），"
  log "     按本机布局渲染后装过去："
  log "       mkdir -p ${BACKUP_DIR}"
  log "       for u in cwq-backup.service cwq-backup.timer; do \\"
  log "         sed -e \"s|/opt/cwq/backups|${BACKUP_DIR}|g\" -e \"s|/opt/cwq|${APP}|g\" \\"
  log "           deploy/systemd/\$u > ${SYSTEMD_UNIT_DIR}/\$u; done"
  log "       systemctl daemon-reload && systemctl enable --now cwq-backup.timer"
  log "     然后验证真的产出文件：systemctl start cwq-backup.service && ls -l ${BACKUP_DIR}/"
fi

# (1.2) 离站备份定时器
#
# 与上面的本机 dump **分开说**：两者是不同的退路。本机 dump 写在同一块盘上，
# 机器坏了它跟着坏；离站备份在阿里云 OSS，是唯一能跨机器故障活下来的副本。
# 只有一个 timer 开着，等于只有一条退路 —— 而出事的时候才会发现是哪一条。
oss_timer_rc=0
enable_oss_backup_timer >/dev/null 2>&1 || oss_timer_rc=$?

if [[ "$oss_timer_rc" -eq 2 ]]; then
  # 与本机 dump 共用同一个关停标记：一个标记关掉所有定时备份。
  log "  离站备份按要求保持关闭（标记文件：${BACKUP_TIMER_OFF_MARKER}）"
  log "     要恢复：systemctl enable --now cwq-oss-backup.timer && 删掉那个标记文件"
elif [[ "$oss_timer_rc" -eq 0 ]]; then
  next_run=$(systemctl show cwq-oss-backup.timer -p NextElapseUSecRealtime --value 2>/dev/null || echo "")
  log "  离站备份（阿里云 OSS）已启用${next_run:+（下次：${next_run}）}"
  log "     它与本机 dump 错开半小时，两条退路都在"
elif [[ "$oss_timer_rc" -eq 3 ]]; then
  log "  ⚠️  产物里没有 cwq-oss-backup.* 单元，离站备份定时器没装上"
  log "     离站备份现在只发生在发版时与手工触发时"
elif [[ "$oss_timer_rc" -eq 5 ]]; then
  log "  ⚠️  OSS 参数没配全，离站备份定时器没启用 —— 离站备份现在只发生在手工触发时"
  log "     配好 endpoint / bucket / AccessKey（系统管理 → 数据安全）后："
  log "       systemctl enable --now cwq-oss-backup.timer"
else
  log "  ⚠️  离站备份定时器启用失败 —— 离站备份现在只发生在手工触发时"
  log "     手动补一次："
  log "       for u in cwq-oss-backup.service cwq-oss-backup.timer; do \\"
  log "         sed -e \"s|/opt/cwq/backups|${BACKUP_DIR}|g\" -e \"s|/opt/cwq|${APP}|g\" \\"
  log "           deploy/systemd/\$u > ${SYSTEMD_UNIT_DIR}/\$u; done"
  log "       systemctl daemon-reload && systemctl enable --now cwq-oss-backup.timer"
fi

# (1.5) 调度看门狗
#
# 计划任务由**外部调度服务**每分钟调一次 tick 驱动（见 README「启用定时调度」）。
# 那个服务挂了 → tick 不来 → 任务不被领取 → **所有定时任务静默停摆**，
# 而这件事应用自己发现不了（它只在被 tick 到时才醒着）。
#
# 看门狗就补这一段：每 15 分钟查一次「有任务到期没人领」，有就发告警邮件。
# 它**独立于应用**跑（systemd timer），所以它能发现"应用没被叫醒"。
watchdog_timer_rc=0
enable_watchdog_timer >/dev/null 2>&1 || watchdog_timer_rc=$?

if [[ "$watchdog_timer_rc" -eq 2 ]]; then
  log "  调度看门狗按要求保持关闭（标记文件：${WATCHDOG_TIMER_OFF_MARKER}）"
  log "     关掉它意味着「外部调度服务停了」这件事没人会被通知"
  log "     要恢复：systemctl enable --now cwq-scheduler-watchdog.timer && 删掉那个标记文件"
elif [[ "$watchdog_timer_rc" -eq 0 ]]; then
  next_run=$(systemctl show cwq-scheduler-watchdog.timer -p NextElapseUSecRealtime --value 2>/dev/null || echo "")
  log "  调度看门狗已启用${next_run:+（下次：${next_run}）}"
  log "     它每 15 分钟查一次「有计划任务到点没跑」，有就发告警邮件"
else
  log "  ⚠️  调度看门狗启用失败 —— 外部调度服务停掉时不会有任何通知"
  log "     手动装一次："
  log "       for u in cwq-scheduler-watchdog.service cwq-scheduler-watchdog.timer; do \\"
  log "         sed -e \"s|/opt/cwq|${APP}|g\" \\"
  log "           deploy/systemd/\$u > ${SYSTEMD_UNIT_DIR}/\$u; done"
  log "       systemctl daemon-reload && systemctl enable --now cwq-scheduler-watchdog.timer"
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
#
# **三种情况必须分开说，宁可漏报也不要误报。**
# 原来的写法只有两分（空 / 非空），而 grep 在**文件不存在**时退出码是 2
# （非 0）→ 走 else → 打印「已配置」。于是「.env 根本不存在」这个
# **更糟**的状态被报成了「已处理」：运维以为配了、其实没配，
# IP 维度节流退化成一个全站共用的桶。
#
# 触发路径不是假设：`deploy.sh` 第 3 步的 die 只在 `DATABASE_URL`
# 环境变量**也**为空时才拦，所以「DATABASE_URL=… deploy.sh」这条
# 完全合法的路径下 .env 可以压根不存在。
ENV_FILE="$APP/backend/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  log "  ⚠️  ${ENV_FILE} 不存在 —— **无法确认** TRUSTED_PROXIES 是否配置"
  log "     读不到配置文件却报「已配置」，是比误报更糟的方向："
  log "     生产上会以为配了、其实没配，IP 维度节流退化成一个全站共用的桶。"
  log "     配法见 deploy/tuning.md 第八节。改完要重启 cwq-api 才生效。"
elif ! grep -qE '^[[:space:]]*TRUSTED_PROXIES[[:space:]]*=' "$ENV_FILE"; then
  log "  ⚠️  ${ENV_FILE} 里**没有** TRUSTED_PROXIES 这一项 —— IP 维度登录节流当前形同虚设（全站共用一个桶）"
  log "     配法见 deploy/tuning.md 第八节。改完要重启 cwq-api 才生效。"
elif grep -qE '^[[:space:]]*TRUSTED_PROXIES[[:space:]]*=[[:space:]]*"?[[:space:]]*"?[[:space:]]*$' "$ENV_FILE"; then
  log "  ⚠️  TRUSTED_PROXIES 是空的 —— IP 维度登录节流当前形同虚设（全站共用一个桶）"
  log "     配法见 deploy/tuning.md 第八节。改完要重启 cwq-api 才生效。"
else
  log "  TRUSTED_PROXIES 已配置"
fi

log "✅ 发布完成（${SHA}）"

# 暂存目录：产物已经解压进 ${APP}，这里只是下载暂存。
# 第 3 步解新代码用的那份暂存目录（${INCOMING}）也一并清掉 ——
# 它只在发布过程中有用。
# 中途失败时这两个目录都**故意留着**，方便翻现场；产物另有一份在
# ${RELEASES}/${STAMP}/artifact，所以留着并不等于「只有现场里有新代码」。
rm -rf "$STAGE" "$INCOMING"

# 只保留最近 KEEP 份备份
ls -dt "$RELEASES"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf

# 前端的版本化目录同理只留 KEEP 份。
#
# **保留的那几份不是「垃圾」**：上一版 release 目录还在，回退（trap 里
# 把 dist 软链指回去）之后，旧前端引用的 /assets/* 仍然打得开 ——
# 旧布局下那些文件在切换那一刻就没了，回退回去也是一堆 404。
if [[ -n "${FRONT_RELEASES:-}" && -d "$FRONT_RELEASES" ]]; then
  ls -dt "$FRONT_RELEASES"/dist-* 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf
fi
