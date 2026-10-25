#!/usr/bin/env bash
# 部署脚本沙箱演练（审计 P3-03）。
#
# deploy.sh 没有测试框架，但「它到底能不能跑通」恰恰是这次要钉的东西 ——
# 审计说这份脚本当前状态下无法成功完成一次发布，修完就该证明它能。
#
# 做法：在临时目录里造出**真实的产物 tarball**（不是桩），
# 用假的 pip / alembic / systemctl / curl 顶替真命令，
# **真的把 deploy.sh 跑一遍**，然后断言它做了什么。
#
# 最重要的一条是 assert_ran：脚本必须真的跑起来了，后面所有断言才有意义。
# 这个守卫是被「恒绿测试」坑出来的 —— 旧版 deploy.sh 把 APP 写死成
# /opt/cwq（不支持覆盖），在沙箱里第一行 ``cd "$APP"`` 就失败退出，
# 于是「没重启服务」「没跑测试」这些断言全都成立，一片绿。
# 但那不是脚本修好了，是脚本**根本没执行**。
#
# **活着的判据随发布流程一起改了。** 旧流程第一步是 ``git fetch``，
# 现在第一步是「取 CI 产物」—— 所以 assert_ran 盯的是产物有没有被取走，
# 不是 git 有没有被调。改判据不是放水，是让守卫跟真实的第一步对齐。
#
# 运行：bash deploy/tests/test_deploy.sh
set -uo pipefail

SCRIPT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy.sh
# 仓库根：沙箱要**真的**跑仓库里那份装配脚本，而不是拿桩糊过去。
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PASS=0; FAIL=0
ok(){ echo "  OK   $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL $1"; FAIL=$((FAIL+1)); }

DEP_N=0
FAKE_SHA="abc123def456"

# --------------------------------------------------------------------------
# 造一套真实产物（结构必须与 build-artifacts job 产出的一致）
# --------------------------------------------------------------------------
make_artifacts(){
  local out="$1"
  # 内容里**带 sha**：这样「线上到底是哪一版」才断言得出来。
  # 所有产物内容都一样的话，「回退到上一版」根本分不出「退回去了」
  # 与「压根没换过」。
  local sha="${2:-$FAKE_SHA}"
  mkdir -p "$out"
  local be="$DEPDIR/stage-backend" fe="$DEPDIR/stage-frontend"
  mkdir -p "$be/app" "$be/scripts" "$be/migrations/versions" "$fe/dist"
  echo "NEW backend code $sha" > "$be/app/main.py"
  echo "# new" > "$be/app/__init__.py"
  echo "print('backup')" > "$be/scripts/db_backup.py"
  echo "print('plugin status')" > "$be/scripts/plugin_status.py"
  # 运行时配置装配脚本：**用仓库里真的那份**。桩掉它的话，沙箱就只验证了
  # 「脚本被调用了」，而不是「配置真的被装配对了」—— 而后者才是这一步的意义。
  cp "$REPO_ROOT/backend/scripts/emit_frontend_runtime.py" "$be/scripts/emit_frontend_runtime.py"
  echo "print('pass')" > "$be/migrations/env.py"
  # 新迁移链的标记。断言「迁移跑的是产物里那条链」全靠它。
  echo "# NEW MIGRATION CHAIN" > "$be/migrations/versions/0027_plugin_imports.py"
  echo "[alembic]" > "$be/alembic.ini"
  # 第一行是这一版新增的依赖 —— 真实事故里就是 rfc8785 没被装上。
  # 它必须与现网旧锁的第一行**不同**，否则断言分不出装的是哪一份锁。
  echo "rfc8785==0.1.4" > "$be/requirements.lock"
  echo "SQLAlchemy==2.1.4" >> "$be/requirements.lock"
  echo "[pytest]" > "$be/pytest.ini"
  # 前端产物要**像真的**：带 runtime-config.js 的引用与带哈希的资源引用。
  # 装配脚本改的就是这两处 —— 假产物里没有它们，那一步就等于没被验证。
  mkdir -p "$fe/dist/assets"
  echo "console.log(1)" > "$fe/dist/assets/index-${sha}.js"
  echo "body{}" > "$fe/dist/assets/index-${sha}.css"
  cat > "$fe/dist/index.html" <<HTML
<html><head>
<script src="/runtime-config.js"></script>
<script type="module" crossorigin src="/assets/index-${sha}.js"></script>
<link rel="stylesheet" crossorigin href="/assets/index-${sha}.css"></head>
<body>new frontend $sha</body></html>
HTML
  tar -czf "$out/cwq-backend-${sha}.tar.gz" -C "$be" .
  tar -czf "$out/cwq-frontend-${sha}.tar.gz" -C "$fe" dist
  # 发布脚本也进产物（与 build-artifacts 一致）。
  #
  # 用**仓库里真的那份**：自检那条断言比的就是「产物里的 deploy.sh」与
  # 「正在跑的 deploy.sh」的 sha256。沙箱里要是塞一份假的，这条断言就恒真，
  # 测不到任何东西 —— 那正是「恒绿测试」的形态。
  tar -czf "$out/cwq-deploy-${sha}.tar.gz" -C "$REPO_ROOT" \
    --exclude='__pycache__' --exclude='*.pyc' deploy
  {
    echo "sha=${sha}"
    echo "commit=${sha}fullsha"
    echo "ref=main"
    echo "built_at=2026-10-15T00:00:00Z"
    echo "backend=cwq-backend-${sha}.tar.gz"
    echo "frontend=cwq-frontend-${sha}.tar.gz"
    echo "deploy=cwq-deploy-${sha}.tar.gz"
  } > "$out/manifest.txt"
  ( cd "$out" && sha256sum ./cwq-backend-${sha}.tar.gz \
                        ./cwq-frontend-${sha}.tar.gz \
                        ./cwq-deploy-${sha}.tar.gz > SHA256SUMS )
}

# 换一版产物（内容与文件名都带新的 sha）。
# 用于「回退到上一版」这类断言：两次发布的 sha 必须不同，否则它们落在
# 同一个 release 目录里，「退回去了」与「压根没换过」长得一模一样。
use_sha(){
  local sha="$1"
  rm -rf "$DEPDIR/artifacts"
  mkdir -p "$DEPDIR/artifacts"
  make_artifacts "$DEPDIR/artifacts" "$sha"
}

setup_sandbox(){
  DEP_N=$((DEP_N+1))
  export DEPDIR="/tmp/cwq-dep-$DEP_N-$$"
  rm -rf "$DEPDIR"
  mkdir -p "$DEPDIR/bin"
  APP="$DEPDIR/app"
  mkdir -p "$APP/backend/.venv/bin" "$APP/backend/migrations/versions" \
           "$APP/frontend/dist" "$APP/.git"
  # 现网 venv 的「依赖状态」标记。
  #
  # **这两个文件是 §3.39 的观测点。** 依赖是在第 3 步装进**现网那个** venv 的，
  # 而回退清单里原本没有 .venv —— 于是「依赖有没有跟着回退」根本测不出来。
  # 这里给 venv 放两个带版本的内容：pip 桩会把它们换成新版的，
  # 发布失败之后它们必须**回到旧版**。
  mkdir -p "$APP/backend/.venv/lib/site-packages/oldpkg"
  echo "OLD DEPS" > "$APP/backend/.venv/lib/deps-marker"
  echo "OLD 1.0" > "$APP/backend/.venv/lib/site-packages/oldpkg/__init__.py"
  echo "OLD backend content" > "$APP/backend/app.py"
  echo "<html>old frontend</html>" > "$APP/frontend/dist/index.html"
  # 现网（上一版）留下的锁文件与迁移链，内容都与产物里的**刻意不同**。
  # 「装的是哪一份锁」「跑的是哪一条链」必须能被断言直接读出来，
  # 只断言「跑过 pip / 跑过 alembic」的话，改坏了也测不出来。
  echo "SQLAlchemy==2.1.4" > "$APP/backend/requirements.lock"
  echo "# OLD MIGRATION CHAIN" > "$APP/backend/migrations/versions/0021_youyou_chart_of_accounts.py"
  # 现网的 .env：连接串在这里。真实机器上 systemd 的 EnvironmentFile
  # 指向的也是这个文件，迁移必须打在它说的那个库上。
  echo "DATABASE_URL=postgresql+psycopg://cwq:pw@127.0.0.1:15432/cwq" > "$APP/backend/.env"
  make_artifacts "$DEPDIR/artifacts"

  # systemctl 桩：**只有 restart 能被单独打挂**（STUB_RESTART_RC）。
  # 「重启失败」是唯一能触发 trap 自动回退的注入点 —— 别的步骤失败时
  # 要么还没动现网，要么已经过了「可以回退」的阶段。
  cat > "$DEPDIR/bin/systemctl" <<'STUB'
#!/bin/sh
echo "systemctl $*" >> "$DEPDIR/calls"
case "$*" in
  *restart*) exit ${STUB_RESTART_RC:-0} ;;
esac
exit 0
STUB
  # curl 桩：**要真的模拟 nginx 干了什么**，否则前端可达性那两道闸
  # 永远测不到东西（它们量的是「线上回来的那一份」）。
  #
  #   * /api/health → 后端探活（STUB_HEALTH_RC 可以打挂）
  #   * / 与 /runtime-config.js → **从 $APP/frontend/dist 里取文件**，
  #     就像 nginx 那样（nginx 的 root 就指着那个目录）。
  #     于是「线上跑的是不是刚发出去的这一版」这件事变成可测的：
  #     dist 里是新版 → 检出版本号；换成旧的 → 闸门立刻现形。
  #   * STUB_FRONTEND_RC != 0 → 前端整个取不到（模拟 nginx 没在服务）。
  cat > "$DEPDIR/bin/curl" <<'STUB'
#!/bin/sh
echo "curl $*" >> "$DEPDIR/calls"
url=""
for a in "$@"; do
  case "$a" in
    http://*|https://*) url="$a" ;;
  esac
done
[ -n "$url" ] || exit 0
case "$url" in
  *:8000/api/health*) exit ${STUB_HEALTH_RC:-0} ;;
esac
if [ "${STUB_FRONTEND_RC:-0}" != "0" ]; then exit "$STUB_FRONTEND_RC"; fi
rest="${url#*://}"
path="/${rest#*/}"
dist="${STUB_FRONTEND_FROM:-$DEPDIR/app/frontend/dist}"
case "$path" in
  */) f="$dist/index.html" ;;
  *)  f="$dist$path" ;;
esac
[ -f "$f" ] || exit 22
cat "$f"
exit 0
STUB
  # ip 桩：**什么都查不到**。
  #
  # host_public_ip 的最后一档是「读网卡上的非回环 IPv4」—— 用真 ip 命令
  # 的话，测试机上永远查得到一个地址，于是「认不出本机出口 IP」这个分支
  # 在沙箱里**根本走不到**（§3.45 的 fail-open 就是从这个分支漏过去的）。
  cat > "$DEPDIR/bin/ip" <<'STUB'
#!/bin/sh
exit 0
STUB
  # tar 桩：能**只**打挂「切换现网代码前的那次解包」（STUB_TAR_FAIL_CODE=1）。
  #
  # 判据是 -C 的目标目录名：``.backend.incoming.*`` 才是切换点那一次，
  # 其余的解包（暂存目录、前端产物、发布工具、脚本自检）都放行。
  # 这正是「tar 故障注入」这个一直缺的测试手段。
  REAL_TAR="$(command -v tar)"
  cat > "$DEPDIR/bin/tar" <<STUB
#!/bin/sh
if [ "\${STUB_TAR_FAIL_CODE:-0}" = "1" ]; then
  dest=""
  next=0
  for a in "\$@"; do
    if [ "\$next" = "1" ]; then dest="\$a"; next=0; continue; fi
    if [ "\$a" = "-C" ]; then next=1; fi
  done
  case "\$dest" in
    */.backend.incoming.*)
      echo "tar \$*" >> "\$DEPDIR/calls"
      echo "tar: 模拟故障（切换现网代码前的那次解包失败）" >&2
      exit 1
      ;;
  esac
fi
exec "$REAL_TAR" "\$@"
STUB

  # mv 桩：**能把「把内容挪到线上那个 dist 路径上」这一步打挂**
  # （STUB_MV_FAIL_SWAP=1）。
  #
  # 判据：目标 basename 是 dist、且源**不是** dist-legacy-*。
  #   * 新代码：换软链那一步的源是 dist.swap.<pid> → 打挂 ✓（注入点）
  #   * 新代码：回退时把旧目录搬回原位，源是 dist-legacy-* → 放行 ✓
  #   * **旧代码**（两次 mv）：`mv "$FRONT_IN/dist" "$APP/frontend/dist"`
  #     正好命中 → 打挂 → 而旧代码没有 trap → dist 就此不存在 →
  #     断言「dist 仍然指得到内容」会红。**这就是「两次 mv 之间有窗口」
  #     这个回归的行为级钉子。**
  REAL_MV="$(command -v mv)"
  cat > "$DEPDIR/bin/mv" <<STUB
#!/bin/sh
if [ "\${STUB_MV_FAIL_SWAP:-0}" = "1" ]; then
  last=""
  prev=""
  for a in "\$@"; do prev="\$last"; last="\$a"; done
  case "\$last" in
    */dist)
      case "\$prev" in
        */dist-legacy-*) : ;;   # 回退时把旧目录搬回去，放行
        *)
          echo "mv \$*" >> "\$DEPDIR/calls"
          echo "mv: 模拟故障（把内容挪到线上 dist 这一步失败）" >&2
          exit 1
          ;;
      esac
      ;;
  esac
fi
exec "$REAL_MV" "\$@"
STUB
  # pip 桩：把「-r 指向的那份锁文件的第一行」记进 calls。
  # 2026-10-18 那次事故里 pip **返回成功**，装的是旧锁；只记「跑过 pip」
  # 等于把那次失败判成绿灯。
  #
  # **还要真的把现网 venv 改掉** —— 桩只记日志的话，「依赖有没有跟着回退」
  # 这件事根本测不出来（现网 venv 一个字节没变，回退无从谈起）。
  # 这里把 venv 里的依赖标记换成新版的，模拟「装依赖改写了现网 venv」。
  cat > "$APP/backend/.venv/bin/pip" <<'STUB'
#!/bin/sh
lock=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then lock=$(head -1 "$a" 2>/dev/null); next=0; continue; fi
  if [ "$a" = "-r" ]; then next=1; fi
done
echo "pip $* [lock=${lock}]" >> "$DEPDIR/calls"
if [ "${STUB_PIP_RC:-0}" = "0" ]; then
  # 模拟「装依赖把现网 venv 改掉了」。
  VENV_DIR="$DEPDIR/app/backend/.venv"
  mkdir -p "$VENV_DIR/lib/site-packages/oldpkg" 2>/dev/null
  echo "NEW DEPS" > "$VENV_DIR/lib/deps-marker" 2>/dev/null
  echo "NEW 2.0" > "$VENV_DIR/lib/site-packages/oldpkg/__init__.py" 2>/dev/null
fi
exit ${STUB_PIP_RC:-0}
STUB
  # alembic 桩：记下**从 cwd 看**到的是新链还是旧链、以及 cwd 里的 .env 是否可用。
  # cwd 才是决定依据来源的那一个（alembic.ini 的 %(here)s 与
  # prepend_sys_path = . 都相对它解析），所以记「cwd 看到什么」而不是记路径。
  # [env=ok] 是「迁移打在现网那个库上」的证据：pydantic 的 env_file 相对 cwd，
  # cwd 里没有 .env 就会退回源码默认值，把迁移打到另一个库上去。
  cat > "$APP/backend/.venv/bin/alembic" <<'STUB'
#!/bin/sh
if grep -rqs "NEW MIGRATION CHAIN" migrations 2>/dev/null; then chain=NEW; else chain=OLD; fi
if [ -e .env ] && grep -q DATABASE_URL .env 2>/dev/null; then envsrc=ok; else envsrc=MISSING; fi
echo "alembic $* [chain=${chain}] [env=${envsrc}]" >> "$DEPDIR/calls"
exit ${STUB_ALEMBIC_RC:-0}
STUB
  # db_backup.py 走 venv 里的 python，要真的吐点东西当备份。
  # **装配脚本要真的跑**（exec 到真的 python3）：这一步的价值全在
  # 「配置真的被装配对了」，桩掉它就只剩「被调用过」这种没意义的断言。
  #
  # 匹配用 **basename**：deploy.sh 传的是**绝对路径**（$INCOMING/scripts/…），
  # 按整串比对会匹配不上 —— 于是桩子只是把调用记了一笔就返回 0，
  # 脚本其实**一次都没跑**，而「调用被记录了」这个断言照样是绿的。
  # 这正是本文件开头说的那种恒绿测试。
  printf '#!/bin/sh\nif [ "$(basename "$1")" = "emit_frontend_runtime.py" ]; then echo "python $*" >> "$DEPDIR/calls"; exec python3 "$@"; fi\nif [ "$1" = "scripts/db_backup.py" ]; then\n  echo "python $*" >> "$DEPDIR/calls"\n  out=""; next=0\n  for a in "$@"; do\n    if [ "$next" = "1" ]; then out="$a"; next=0; continue; fi\n    if [ "$a" = "--out" ]; then next=1; fi\n  done\n  if [ -n "$out" ]; then\n    if [ "${STUB_BACKUP_RC:-0}" != "0" ]; then echo "backup failed (simulated)" >&2; exit "${STUB_BACKUP_RC}"; fi\n    printf "SELECT 1;\\n" > "$out.part"\n    mv "$out.part" "$out"\n  else\n    echo "SELECT 1;"\n  fi\n  exit ${STUB_BACKUP_RC:-0}\nfi\necho "python $*" >> "$DEPDIR/calls"\nif [ "$1" = "scripts/plugin_status.py" ]; then exit ${STUB_PLUGIN_STATUS_RC:-0}; fi\nif [ "$1" = "scripts/scheduler_state.py" ]; then exit ${STUB_SCHEDULER_OWNS_RC:-1}; fi\nexit 0\n' \
    > "$APP/backend/.venv/bin/python"
  # rm 桩：**透明转发**给真的 rm。
  #
  # 为什么要桩：某些本地运行时把 `rm` 换成了「可恢复删除」的转发器
  # （本机就是），而它**处理符号链接时会挂住**（实测：删软链必挂，
  # 删普通文件、删不存在的路径都正常）。
  #
  # 而 deploy.sh 自己就会造出软链并删它：`atomic_symlink_swap` 里
  # ``rm -f "${link}.swap.$$"``（deploy.sh:417）—— 前一次换软链失败时那个
  # 残留的软链就在那儿等着被删。**这不是 deploy.sh 的问题**（真实机器上的
  # `rm -f <软链>` 是一瞬间的事），但它会让整套测试在本机**卡死**。
  #
  # 沙箱里要的是「跟生产机一样的 rm」，所以这里直接转发给真的 rm。
  cat > "$DEPDIR/bin/rm" <<'STUB'
#!/bin/sh
exec /bin/rm "$@"
STUB
  chmod +x "$DEPDIR/bin"/* "$APP/backend/.venv/bin"/*
  # 调度现状探针（scripts/scheduler_state.py）要**真的存在** ——
  # deploy.sh 会先看文件在不在，文件不在就直接判「应用调度器没在管备份」。
  # 那个桩只会被 python 桩接走，返回码由 STUB_SCHEDULER_OWNS_RC 定：
  #   1（默认）= 应用调度器**没**在做离站备份 → 旧的 systemd timer 应当启用
  #   0        = 应用调度器**在**做离站备份 → 旧的 systemd timer 应当停用
  mkdir -p "$APP/backend/scripts"
  echo "# 沙箱桩：真正跑的是 python 桩，按 STUB_SCHEDULER_OWNS_RC 决定返回码。" \
    > "$APP/backend/scripts/scheduler_state.py"
  # unit 与备份的落地位置由 deploy.sh 通过 SYSTEMD_UNIT_DIR / BACKUP_DIR 传入。
  # 这里必须先建出来：真实机器上 /etc/systemd/system 与 /opt/cwq/backups
  # 本来就存在，脚本不负责建；沙箱里没人建，cp 就会落不下去。
  mkdir -p "$DEPDIR/units" "$DEPDIR/backups" "$DEPDIR/logrotate" "$DEPDIR/journald"
  : > "$DEPDIR/calls"
}

# 沙箱里跑 deploy.sh 必须放行目标机校验：这台机器的出口 IP 不会是生产机。
# 放行变量加在**每一个 runner 的调用行**上，而不是 export 到全局 ——
# 这样「测试跑在哪台机器上」这件事对 deploy.sh 是显式可见的，
# 也不会顺手把放行带进同文件里其它不经过 runner 的直接调用。
CWQ_ALLOW_ANY_HOST=1
run_script(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    LOGROTATE_DIR="$DEPDIR/logrotate" JOURNALD_DIR="$DEPDIR/journald" CWQ_ALLOW_ANY_HOST=1 CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
run_script_no_artifacts(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/empty-artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    LOGROTATE_DIR="$DEPDIR/logrotate" JOURNALD_DIR="$DEPDIR/journald" CWQ_ALLOW_ANY_HOST=1 CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
# 显式清掉 DATABASE_URL：这条只用来验证「目标库不确定时脚本会停手」。
# 不能依赖 CI 机器上恰好没有这个变量 —— 那种断言一旦环境一变就变成恒绿。
run_script_no_db_url(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    LOGROTATE_DIR="$DEPDIR/logrotate" JOURNALD_DIR="$DEPDIR/journald" CWQ_ALLOW_ANY_HOST=1 CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
    env -u DATABASE_URL LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
# 显式给 DATABASE_URL：用来验证「.env 不存在」这条路径 —— 那条路径是
# 合法的（deploy.sh 第 3 步只在 .env 与 DATABASE_URL **都**没有时才拦），
# 而它正是 TRUSTED_PROXIES 告警方向反了的触发场景。
run_script_with_db_url(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    LOGROTATE_DIR="$DEPDIR/logrotate" JOURNALD_DIR="$DEPDIR/journald" CWQ_ALLOW_ANY_HOST=1 CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
    DATABASE_URL="postgresql+psycopg://cwq:pw@127.0.0.1:15432/cwq" \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
calls(){ cat "$DEPDIR/calls" 2>/dev/null; }

# CDN 形态：静态资源基址指向 CDN、配置脚本指向源站。
run_script_cdn(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    LOGROTATE_DIR="$DEPDIR/logrotate" JOURNALD_DIR="$DEPDIR/journald" CWQ_ALLOW_ANY_HOST=1 CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
    CWQ_ASSET_BASE="https://cdn.example.com/" CWQ_ORIGIN="https://acct.example.com" \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}

# **管道里的 grep 一律不用 -q**（理由见第 16 组 code_count 的注释，
# 那里已经把同一个坑写成了规矩）：`grep -q` 一匹配就退出 → 上游的
# cat/grep 收到 SIGPIPE → `set -uo pipefail` 下整条管道返回非零 →
# 「匹配到了」反而被判成「没匹配」。
#
# 这个坑**在 macOS 上可能侥幸通过、在 Linux 上稳定复现**：上游要把
# 几十 KB 写进管道缓冲区，下游命中第一行就退出了，谁先跑完看调度。
# 2026-10-21 的 CI 就是这么红的：deploy.sh 明明装了 trap（231/234/235 行），
# 断言却说「脚本一个 trap 都没有」—— 判的不是脚本，是自己的管道。
#
# `grep -c` 会把输入读完再退出，没有这个竞态；命中与否看退出码。
calls_has(){ grep -cE -- "$1" "$DEPDIR/calls" >/dev/null 2>&1; }
# 取第一行匹配：直接读文件 + `grep -m1`，不走管道。
# （`grep ... | head -1` 里 head 先退，上游可能被 SIGPIPE 打断，
#   那条还没写出来的匹配行就丢了 —— 断言会拿到空串。）
calls_first(){ grep -m1 -F -- "$1" "$DEPDIR/calls" 2>/dev/null; }

# --------------------------------------------------------------------------
# 抠取器：把一份 shell 脚本里的**注释**（可选地连**字符串内容**一起）去掉，
# 只留下可执行的代码，并给出它的行号。
#
# **为什么不能全文 grep。**
# deploy.sh 的**注释里**就写着 `ROLLBACK_SCOPE=code`
# （§3.34 那段「回退闸开得太晚」的说明里有一句「ROLLBACK_SCOPE=code 原来在
# tar 之后才置位」）—— 全文 grep 找到的第一处是**注释**，不是代码。
# 而要断言的恰恰是「**代码**里的那一处置位在覆盖之前」。
#
# 抠取规则（对齐 bash 的真实词法）：
#   * 整行注释（行首 #）→ 丢掉；
#   * 行尾注释（# 不在引号里，且前面是空白/分隔符/行首）→ 砍掉；
#     （`a#b` 里的 # 不是注释 —— bash 的规则，抠取器也得认）
#   * 引号里的 # → 不是注释；
#   * mode=code 时**连字符串内容一起抹掉**（只留引号本身）——
#     于是「字符串里恰好写着 ROLLBACK_SCOPE=code」也不算数；
#   * mode=text 时保留字符串内容（要看「赋的值是什么」时用）。
#
# **抠取器自己也要有自测** —— 见第 21.1 组：一个连自己造的、答案明摆着的
# 样本都分不出来的抠取器，测的是「自己以为的东西」。
# --------------------------------------------------------------------------
shell_code_lines(){
  # $1 = 脚本文件，$2 = code | text
  awk -v mode="${2:-code}" '
    BEGIN { SQ = sprintf("%c", 39); DQ = sprintf("%c", 34) }
    function scan(line,   i, n, c, q, out) {
      out = ""; q = ""; i = 1; n = length(line)
      while (i <= n) {
        c = substr(line, i, 1)
        if (q == "") {
          if (c == "#") {
            if (i == 1 || substr(line, i - 1, 1) ~ /[ \t;|&()<>]/) break
            out = out c; i++; continue
          }
          if (c == SQ || c == DQ) { q = c; out = out c; i++; continue }
          out = out c; i++; continue
        } else {
          if (c == q) { q = ""; out = out c; i++; continue }
          if (c == "\\" && q == DQ) {
            out = out c; i++
            if (i <= n) { out = out substr(line, i, 1); i++ }
            continue
          }
          if (mode == "code") { out = out "" } else { out = out c }
          i++; continue
        }
      }
      return out
    }
    { print NR "\t" scan($0) }
  ' "$1"
}
# 第一条匹配的正则所在的**行号**（空 = 没找到）
code_line_no(){ shell_code_lines "$1" "${3:-code}" | grep -m1 -E -- "$2" | cut -f1 || true; }
# 第一条匹配的正则所在行的**代码**（去掉注释后的内容）
code_line_text(){ shell_code_lines "$1" "${3:-code}" | grep -m1 -E -- "$2" | cut -f2- || true; }

# 脚本必须真的跑起来了（取到了产物），否则下面全是空断言
assert_ran(){
  if calls_has "alembic upgrade head"; then return 0; fi
  no "$1 —— 脚本在沙箱里没跑起来，下面的断言全部无意义"
  return 1
}

echo "== 1. --check-only 必须真的只检查 =="
setup_sandbox
OUT=$(run_script --check-only); RC=$?
if grep -q "只做检查" <<<"$OUT"; then
  ok "--check-only 确实执行了（不是空跑，也不是跑崩了）"
else
  no "--check-only 分支没被执行 —— 后续断言无意义"
fi
if [[ "$RC" == "0" ]]; then
  ok "--check-only 以 0 正常退出（是主动结束的，不是崩的）"
else
  no "--check-only 退出码 $RC —— 它并没有在检查完就停下来"
fi
if [[ -d "$APP/.releases" ]]; then
  no "--check-only 仍然创建了备份目录 —— 它继续往下走了"
else
  ok "--check-only 没有产生任何发布副作用"
fi
for tool in systemctl curl; do
  if calls_has "$tool"; then no "--check-only 仍然调了 $tool"; else ok "--check-only 没调 $tool"; fi
done

echo "== 2. 生产机不再跑测试、不再构建前端 =="
setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
# 这两条是本次改造的核心目的：把生产机从构建机上摘下来。
if calls_has "pytest"; then no "生产机仍然跑测试（pytest）"; else ok "生产机不再跑测试"; fi
if calls_has "pnpm"; then no "生产机仍然构建前端（pnpm）"; else ok "生产机不再构建前端"; fi
if calls_has "node"; then no "生产机仍然跑前端（node）"; else ok "生产机不再跑前端"; fi
# 断言「装的锁是哪一份」，不是「有没有跑 pip」。产物里的锁第一行是
# rfc8785（这一版新增的依赖），现网旧锁第一行是 SQLAlchemy —— 两者不同，
# 断言才分得出来。
PIPLINE=$(calls_first 'pip install')
if [[ "$PIPLINE" == *"[lock=rfc8785==0.1.4]"* ]]; then
  ok "装的是产物里的运行时锁（第一行 rfc8785；不是 dev 锁，也不是现网旧锁）"
else
  no "没装、或装的不是产物里那份锁：${PIPLINE:-（根本没有 pip 调用）}"
fi

echo "== 3. 产物被取走并校验通过 =="
setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if grep -q "产物校验和通过" <<<"$OUT"; then
  ok "产物 sha256 校验通过"
else
  no "没有做产物校验（或校验没过）"
fi
if grep -q "取到产物" <<<"$OUT"; then
  ok "取到产物 ${FAKE_SHA}"
else
  no "没有取到产物"
fi

echo "== 4. 取不到产物 = 测试闸门拦住，绝不重启 =="
setup_sandbox
mkdir -p "$DEPDIR/empty-artifacts"
OUT=$(run_script_no_artifacts); RC=$?
if calls_has "systemctl restart"; then
  no "取不到产物却重启了服务 —— 测试闸门失效"
else
  ok "取不到产物没有重启"
fi
if [[ "$RC" != "0" ]]; then
  ok "取不到产物以非零退出（${RC}）"
else
  no "取不到产物却返回成功"
fi
if grep -q "没有完整的一套产物" <<<"$OUT"; then
  ok "明确报出产物缺失"
else
  no "没有明确报错"
fi
if [[ -d "$APP/.releases" ]]; then
  no "产物缺失却仍创建了备份目录"
else
  ok "产物缺失时连备份都没动"
fi

echo "== 5. 校验和不匹配必须拒绝发布 =="
setup_sandbox
echo "corrupted" >> "$DEPDIR/artifacts/cwq-backend-${FAKE_SHA}.tar.gz"
OUT=$(run_script)
if calls_has "systemctl restart"; then
  no "产物损坏却重启了服务"
else
  ok "产物损坏没有重启"
fi
if grep -q "校验和不匹配" <<<"$OUT"; then
  ok "明确报出校验和不匹配"
else
  no "没有报出校验和不匹配"
fi
if grep -q "没有改动任何东西" <<<"$OUT"; then
  ok "明确说明没有改动任何东西"
else
  no "没说清楚「什么都没改」"
fi

echo "== 6. .releases 必须 mkdir，且每次都备份 =="
setup_sandbox
run_script >/dev/null
assert_ran "首次发布" || exit 1
if [[ -d "$APP/.releases" ]]; then ok "备份目录已创建"; else no "备份目录没创建"; fi
run_script >/dev/null
N=$(find "$APP/.releases" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
if [[ "${N:-0}" -ge 2 ]]; then
  ok "连续两次发布留下 $N 份独立备份（没被覆盖）"
else
  no "两次发布只留下 ${N:-0} 份备份 —— 后一次覆盖了前一次"
fi
if [[ -n "$(find "$APP/.releases" -name app.py 2>/dev/null)" ]]; then
  ok "备份里确实有后端代码"
else
  no "备份内容为空"
fi
if [[ -d "$(find "$APP/.releases" -maxdepth 3 -type d -name artifact 2>/dev/null | head -1)" ]]; then
  ok "产物也存进备份（回滚不必重新下载）"
else
  no "产物没存进备份 —— 产物被 CI 清理后就退不回去了"
fi

echo "== 7. 前后端产物都真的切过去了 =="
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
if grep -q "NEW backend code" "$APP/backend/app/main.py" 2>/dev/null; then
  ok "后端代码换成了新产物"
else
  no "后端代码没换过去"
fi
if grep -q "new frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "前端 dist 里是新产物（不是旧的）"
else
  no "前端 dist 里不是新产物"
fi
if [[ -f "$APP/frontend/dist/index.html" ]]; then
  ok "dist 换名后仍在"
else
  no "dist 换名后丢了"
fi

echo "== 8. 备份保留 KEEP 份 =="
setup_sandbox
for i in 1 2 3 4 5; do mkdir -p "$APP/.releases/2020010$i-000000"; done
run_script >/dev/null
N=$(find "$APP/.releases" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
if [[ "${N:-99}" -le 3 ]]; then ok "旧备份已清理（剩 $N 份）"; else no "备份没清理（剩 ${N:-?} 份）"; fi

echo "== 9. 迁移由发布流程执行（P3-01）=="
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
if calls_has "alembic upgrade head"; then
  ok "发布流程执行了 alembic upgrade head"
else
  no "没有执行迁移 —— 表结构变更只能靠人工，迟早会漏"
fi

# 迁移失败必须拦住发布：库迁移了一半而代码没换，状态最糟
setup_sandbox
export STUB_ALEMBIC_RC=1
OUT=$(run_script)
unset STUB_ALEMBIC_RC
assert_ran "迁移失败的发布流程" || exit 1
if calls_has "systemctl restart"; then
  no "迁移失败却重启了服务"
else
  ok "迁移失败没有重启"
fi
if grep -q "NEW backend code" "$APP/backend/app/main.py" 2>/dev/null; then
  no "迁移失败却切换了后端代码"
else
  ok "迁移失败没有切换代码"
fi
# 「不切产物」包括前端。nginx 直接从磁盘读 dist，不经后端进程，
# 所以前端一旦换上就已经对外可见了 —— 它必须跟后端代码同一个闸门。
if grep -q "new frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  no "迁移失败却切换了前端产物（nginx 直接读磁盘，换上就已对外可见）"
else
  ok "迁移失败没有切换前端产物"
fi
if grep -q "迁移失败" <<<"$OUT"; then ok "明确报出迁移失败"; else no "没有明确报错"; fi

# 备份失败必须拦住迁移（P2-25）：备份是这个系统唯一的退路
setup_sandbox
export STUB_BACKUP_RC=1
OUT=$(run_script)
unset STUB_BACKUP_RC
if calls_has "alembic upgrade head"; then
  no "备份失败却先迁移了生产库 —— 没有退路就不该动 schema"
else
  ok "备份失败就不迁移（schema 保持在迁移前的状态）"
fi
if calls_has "systemctl restart"; then
  no "备份失败却重启了服务"
else
  ok "备份失败没有重启"
fi
if grep -q "数据库备份失败" <<<"$OUT"; then ok "明确报出备份失败"; else no "没有明确报错"; fi

# --------------------------------------------------------------------------
# 定时备份的启用/关停
#
# 这一段以前**完全没有断言** —— enable_backup_timer 挂在发布末尾，任何一次
# 行为变化都不会被测到。它的实际后果不小：这一步每次发布都会
# ``enable --now``，所以「按要求关掉了定时备份」这件事如果不写成发布脚本
# 认得的显式状态，下一次发版就会把它悄悄拉回来。
# --------------------------------------------------------------------------
echo "== 10. 定时备份：默认启用，关停标记 honored =="

setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if calls_has "systemctl enable --now cwq-backup.timer"; then
  ok "默认会启用定时备份"
else
  no "默认没有启用定时备份"
fi
if grep -q "定时备份已启用" <<<"$OUT"; then ok "明确报出已启用"; else no "没有报出定时备份状态"; fi
# 单元文件必须真的落到目标目录 —— 只 enable 不装 unit 等于没装
if [[ -f "$DEPDIR/units/cwq-backup.timer" && -f "$DEPDIR/units/cwq-backup.service" ]]; then
  ok "两个 unit 文件都装到了目标目录"
else
  no "unit 文件没落到 ${DEPDIR}/units —— enable 了一个不存在的 unit"
fi

# 关停标记在 → 一次 systemctl enable 都不能有，且要说清楚是「按要求关的」
setup_sandbox
: > "$APP/.backup-timer-off"
OUT=$(run_script)
assert_ran "带关停标记的发布" || exit 1
if calls_has "cwq-backup.timer"; then
  no "有关停标记却仍然动了 cwq-backup.timer —— 下次发版它会自己复活"
else
  ok "有关停标记时不碰 cwq-backup.timer"
fi
if grep -q "按要求保持关闭" <<<"$OUT"; then
  ok "明确报出「按要求保持关闭」（而不是含糊成启用失败）"
else
  no "没有把「按要求关闭」与「启用失败」区分开"
fi
if grep -q "⚠️  定时备份启用失败" <<<"$OUT"; then
  no "把按要求关闭报成了 ⚠️ 失败 —— 运维会去修一个没坏的东西"
else
  ok "按要求关闭时不出 ⚠️ 告警"
fi
# 关停定时**不能**连发版时的库备份一起关掉 —— 那是迁移前的唯一退路
if calls_has "alembic upgrade head"; then
  ok "关停定时备份不影响发版时的库备份与迁移"
else
  no "关停定时备份把发版备份也一起弄没了"
fi

# --------------------------------------------------------------------------
# 离站备份（阿里云 OSS）的定时器
#
# **这个 timer 以前从来没人装。** deploy/systemd/cwq-oss-backup.{service,timer}
# 在 3ae13c1（账套数据分级压缩备份到 OSS）就进了仓库，但 enable_backup_timer
# 只渲染并启用 cwq-backup.{service,timer} —— 离站备份那套单元从来没被复制到
# /etc/systemd/system，也没被 enable。于是生产上离站备份从来没排过程：只有
# 发版时和手工触发才会备份到 OSS。
#
# 为什么这不是「多一个 timer」的事：本机 dump 写在同一块盘上，机器整盘故障时
# 它和账套数据一起没了。离站备份是唯一能跨机器故障活下来的副本。少它 =
# 只有一条退路，而出事的时候才会发现是哪一条。
#
# 断言覆盖四件事：默认装上、unit 真的落盘、路径按本机布局渲染、有关停标记时
# 连 unit 都不写。
# --------------------------------------------------------------------------
echo "== 10.2 离站备份（OSS）定时器：装上、渲染、关停标记 honored =="

setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if calls_has "systemctl enable --now cwq-oss-backup.timer"; then
  ok "默认会启用离站备份定时器"
else
  no "默认没有启用离站备份定时器 —— 离站备份在生产上等于没有"
fi
if grep -q "离站备份（阿里云 OSS）已启用" <<<"$OUT"; then
  ok "明确报出离站备份已启用"
else
  no "没有报出离站备份定时器状态"
fi
if [[ -f "$DEPDIR/units/cwq-oss-backup.timer" && -f "$DEPDIR/units/cwq-oss-backup.service" ]]; then
  ok "两个 OSS unit 文件都装到了目标目录"
else
  no "OSS unit 文件没落到 ${DEPDIR}/units —— enable 了一个不存在的 unit"
fi

# 渲染：路径必须落到本机布局，不能把 /opt/cwq 灌回来
OSS_S="$DEPDIR/units/cwq-oss-backup.service"
OSS_BKDIR="$DEPDIR/backups"
if [[ -f "$OSS_S" ]]; then
  if grep -q '/opt/cwq' "$OSS_S"; then
    no "OSS unit 里还留着 /opt/cwq —— 原样拷贝会把旧布局灌回本机"
  else
    ok "OSS unit 里没有 /opt/cwq"
  fi
  for key in WorkingDirectory EnvironmentFile ExecStart; do
    ln=$(grep -m1 "^${key}=" "$OSS_S" || true)
    if [[ "$ln" == *"$APP"* ]]; then
      ok "OSS unit 的 ${key} 已渲染为本机应用根"
    else
      no "OSS unit 的 ${key} 没指向本机应用根（实际：${ln:-<无该行>}）"
    fi
  done
  rwp=$(grep -m1 '^ReadWritePaths=' "$OSS_S" || true)
  if [[ "$rwp" == *"$OSS_BKDIR"* && "$rwp" != *"$APP/backups"* ]]; then
    ok "OSS unit 的 ReadWritePaths 用的是运维给的 BACKUP_DIR"
  else
    no "OSS unit 的 ReadWritePaths 不是 BACKUP_DIR（实际：${rwp:-<无该行>}）"
  fi
  if grep -q "StandardOutput=append:$OSS_BKDIR/" "$OSS_S"; then
    ok "OSS 备份日志重定向也渲染到了 BACKUP_DIR"
  else
    no "OSS 备份日志重定向没有跟着渲染（实际：$(grep -m1 '^StandardOutput=' "$OSS_S" || echo '<无该行>')）"
  fi
else
  no "OSS unit 没装上，渲染断言全部无从谈起"
fi

# 关停标记在 → 离站备份的 timer 一个都不能动，unit 也不能写
setup_sandbox
: > "$APP/.backup-timer-off"
OUT=$(run_script)
assert_ran "带关停标记的发布" || exit 1
if calls_has "cwq-oss-backup.timer"; then
  no "有关停标记却仍然动了 cwq-oss-backup.timer —— 下次发版它会自己复活"
else
  ok "有关停标记时不碰 cwq-oss-backup.timer"
fi
if [[ -f "$DEPDIR/units/cwq-oss-backup.service" ]]; then
  no "有关停标记却仍然写了 OSS unit —— 重启后 timer 会自己复活"
else
  ok "有关停标记时 OSS unit 没被写入（渲染前就短路了）"
fi

# --------------------------------------------------------------------------
# 调度看门狗
#
# 计划任务由**外部调度服务**每分钟调一次 tick 驱动。那个服务挂了 →
# tick 不来 → 任务不被领取 → **所有定时任务静默停摆**，而应用自己发现不了
# （它只在被 tick 到时才醒着）。
#
# 看门狗是唯一能发现「应用没被叫醒」的东西，所以它必须**默认装上**。
# --------------------------------------------------------------------------
echo "== 10.5 调度看门狗：默认启用，关停标记 honored =="

setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if calls_has "systemctl enable --now cwq-scheduler-watchdog.timer"; then
  ok "默认会启用调度看门狗"
else
  no "默认没有启用调度看门狗"
fi
# 单元文件必须真的落到目标目录 —— 只 enable 不装 unit 等于没装
if [[ -f "$DEPDIR/units/cwq-scheduler-watchdog.timer" \
   && -f "$DEPDIR/units/cwq-scheduler-watchdog.service" ]]; then
  ok "看门狗的两个 unit 文件都装到了目标目录"
else
  no "unit 文件没落到 ${DEPDIR}/units —— enable 了一个不存在的 unit"
fi
# 路径必须按本机布局渲染：第二台生产机把应用根放在别处（见 ARCHITECTURE §11.7.9）
if grep -q "/opt/cwq" "$DEPDIR/units/cwq-scheduler-watchdog.service" 2>/dev/null; then
  no "unit 里还留着 /opt/cwq 的硬编码路径"
else
  ok "unit 的路径已按本机布局渲染"
fi

# 关停标记在 → 一次 systemctl enable 都不能有，且要说清楚是「按要求关的」
setup_sandbox
: > "$APP/.watchdog-timer-off"
OUT=$(run_script)
assert_ran "带关停标记的发布" || exit 1
if calls_has "cwq-scheduler-watchdog.timer"; then
  no "有关停标记却仍然动了看门狗 timer —— 下次发版它会自己复活"
else
  ok "有关停标记时不碰看门狗 timer"
fi
if grep -q "调度看门狗按要求保持关闭" <<<"$OUT"; then
  ok "明确报出「按要求保持关闭」（而不是含糊成启用失败）"
else
  no "没有把看门狗的「按要求关闭」说清楚"
fi

# --------------------------------------------------------------------------
# 10.6 用**相对路径**调用时，脚本仍要能找到自己的 systemd 目录
# --------------------------------------------------------------------------
# 这条是 2026-10-20 生产上真实踩到的坑。
#
# 脚本原来用 ``$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/systemd`` 找 unit
# 模板。那是**调用时刻**才求值的：``dirname`` 拿到相对路径（``bash ./deploy.sh``）
# 时，``cd . && pwd`` 落到的是**当时的工作目录** —— 而脚本第 3 步 cd 过
# ${APP}、第 4 步又 cd 到 ${APP}/backend，于是它找的是 ``${APP}/backend/systemd``，
# 不存在，模板没找到，unit 一个都没装。
#
# 后果比「看门狗没装上」严重：每日备份的 timer 在**全新主机**上同样装不上，
# 而且**不报错**（老主机上旧 unit 还在，enable 照样成功）—— 那台机器从上线
# 起就一直没有自动备份。
#
# 为什么沙箱之前一直是绿的：它用**绝对路径**调脚本（``bash "$SCRIPT"``），
# 那种调用方式恰好是对的。所以这条用例必须专门用相对路径调，才钉得住。
run_script_staged_relative(){
  # 照抄生产上的摆放：deploy.sh 与 systemd/ 放在暂存目录里，
  # 从那个目录用相对路径调用。
  local STAGE="$DEPDIR/stage"
  rm -rf "$STAGE"
  mkdir -p "$STAGE/systemd"
  cp "$SCRIPT" "$STAGE/deploy.sh"
  cp "$(dirname "$SCRIPT")"/systemd/* "$STAGE/systemd/"
  ( cd "$STAGE" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    LOGROTATE_DIR="$DEPDIR/logrotate" JOURNALD_DIR="$DEPDIR/journald" CWQ_ALLOW_ANY_HOST=1 CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash ./deploy.sh 2>&1 )
}

echo "== 10.6 相对路径调用时仍要找到 unit 模板 =="

setup_sandbox
OUT=$(run_script_staged_relative)
assert_ran "相对路径调用的发布" || exit 1
for u in cwq-scheduler-watchdog.timer cwq-scheduler-watchdog.service \
         cwq-backup.timer cwq-backup.service; do
  if [[ -f "$DEPDIR/units/$u" ]]; then
    ok "$u 装到了目标目录"
  else
    no "$u 没装到目标目录 —— 相对路径调用时脚本找不到自己的 systemd 目录"
  fi
done
# 装到了还得是对的内容：路径必须按本机布局渲染，不能留着 /opt/cwq
#
# **注意别写成空过的断言**：文件不存在时 grep 找不到东西会返回非零，
# 于是「文件根本没装上」会被判成「路径已正确渲染」。所以先确认文件在，
# 再谈内容 —— 缺文件是更严重的问题，不能被后一条盖过去。
for u in cwq-scheduler-watchdog.service cwq-backup.service; do
  if [[ ! -f "$DEPDIR/units/$u" ]]; then
    no "$u 根本没装上 —— 路径无从谈起（这一条不能被「没有 /opt/cwq」盖过去）"
  elif grep -q "/opt/cwq" "$DEPDIR/units/$u"; then
    no "$u 里还留着 /opt/cwq 的硬编码路径"
  else
    ok "$u 的路径已按本机布局渲染"
  fi
done

echo
echo "== 11. 回滚快照与库备份不该对本机其他账号可读 =="
# 快照目录里躺着全量的库备份、上一版源码和产物包。这台机器跑着多个站点，
# 默认的 755/644 等于把账务数据开放给其他站点的服务账号。
# 实际修法（2026-10-17）：快照目录 install -d -m 700，dump 在 umask 077 的
# 子 shell 里产出。原来是 mkdir -p + 裸重定向，权限全看调用方 umask。
mode_of(){ stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1" 2>/dev/null; }
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
SNAP=$(ls -d "$APP"/.releases/*/ 2>/dev/null | head -1)
if [[ -z "$SNAP" ]]; then
  no "找不到回滚快照目录，权限无从检查"
else
  DUMP="$SNAP/database-before-migration.sql"
  if [[ ! -f "$DUMP" ]]; then
    no "快照里没有 database-before-migration.sql"
  else
    SM=$(mode_of "$SNAP"); DM=$(mode_of "$DUMP")
    if [[ "$SM" == "700" ]]; then
      ok "回滚快照目录是 700"
    else
      no "回滚快照目录是 ${SM} —— 本机其他账号能列目录"
    fi
    if [[ "$DM" == "600" ]]; then
      ok "库备份 dump 是 600"
    else
      no "库备份 dump 是 ${DM} —— 全量账务数据对本机其他账号可读"
    fi
  fi
fi

echo
echo "== 12. 插件屏障检查与持久运行目录 =="
setup_sandbox
mkdir -p "$APP/backend/data/plugins/com.test.retained/digest"
echo "retained plugin code" > "$APP/backend/data/plugins/com.test.retained/digest/marker"
OUT=$(run_script); RC=$?
assert_ran "带插件运行目录的发布" || exit 1
if [[ "$RC" == "0" ]] && calls_has "python scripts/plugin_status.py --wait 30"; then
  ok "主服务启动后核验插件屏障才报告成功"
else
  no "没有执行插件屏障或成功发布返回非零"
fi
if [[ "$(cat "$APP/backend/data/plugins/com.test.retained/digest/marker" 2>/dev/null)" == "retained plugin code" ]]; then
  ok "发布产物没有覆盖持久插件运行目录"
else
  no "发布产物覆盖或删除了插件运行目录"
fi

setup_sandbox
export STUB_PLUGIN_STATUS_RC=1
OUT=$(run_script); RC=$?
unset STUB_PLUGIN_STATUS_RC
assert_ran "插件屏障失败的发布" || exit 1
if calls_has "python scripts/plugin_status.py --wait 30" && [[ "$RC" != "0" ]]; then
  ok "插件屏障失败时发布返回非零"
else
  no "插件屏障失败却仍然报告发布成功"
fi
if grep -q "插件 worker barrier 未通过" <<<"$OUT" && grep -q "新版本已启动" <<<"$OUT"; then
  ok "屏障失败明确说明运行现场及处置方式"
else
  no "屏障失败没有准确报告已启动的现场"
fi
if calls_has "systemctl enable --now cwq-backup.timer"; then
  no "屏障失败仍执行成功后的发布步骤"
else
  ok "屏障失败停止后续发布步骤"
fi

echo
echo "== 13. 发布顺序：依赖与迁移的依据必须是**这一版**，不是上一版 =="
# 这一组是 2026-10-18 生产事故的回归测试。
#
# 那次发布在现网目录里直接 ``pip install -r requirements.lock`` +
# ``alembic upgrade head``，两份依据都是**上一版留在现网的文件**：
# 新依赖 rfc8785 压根没装、alembic 的 head 停在 0021 而新代码要求 0027，
# 两步都显示成功，切到新代码之后服务才炸（ModuleNotFoundError +
# SchemaVersionError）。凡是一次发布同时带来新依赖和新迁移，必然触发。
setup_sandbox
OUT=$(run_script); RC=$?
assert_ran "全量发布" || exit 1

PIPLINE=$(calls_first 'pip install')
if [[ "$PIPLINE" == *"[lock=rfc8785==0.1.4]"* ]]; then
  ok "装依赖用的是产物里的锁文件，不是现网那份旧的"
else
  no "装依赖的依据不对：${PIPLINE:-（根本没有 pip 调用）}"
fi

ALEM=$(calls_first 'alembic upgrade head')
if [[ "$ALEM" == *"[chain=NEW]"* ]]; then
  ok "迁移跑的是产物里的新迁移链（head 来自这一版，不是上一版）"
else
  no "迁移仍在用现网的旧迁移链：${ALEM:-（根本没跑迁移）}"
fi
# [chain=NEW] 同时证明迁移是在**解包到暂存目录之后、切产物之前**跑的：
# 现网目录里那份迁移链是 OLD，只有暂存目录里才是 NEW。
if [[ "$ALEM" == *"[env=ok]"* ]]; then
  ok "迁移时的代码上下文里能读到现网 .env（迁移打在现网那个库上）"
else
  no "迁移时读不到现网 .env —— 会退回源码默认值，可能打错库：${ALEM}"
fi
if [[ "$RC" == "0" ]]; then
  ok "新顺序下完整发布成功（${RC}）"
else
  no "新顺序下发布失败（${RC}）：$(tail -3 <<<"$OUT")"
fi
# 暂存目录收尾：跑通之后不能留在应用根目录里
LEFTOVER=$(find "$APP" -maxdepth 1 -name '.incoming.*' 2>/dev/null | head -1)
if [[ -z "$LEFTOVER" ]]; then
  ok "临时解包目录已清理"
else
  no "临时解包目录残留：${LEFTOVER}"
fi

echo
echo "== 14. 依据不成立时就地停手：装依赖失败 / 目标库不确定 =="

# 装依赖失败就是「依赖没到位」，绝不能先切产物再重启。
setup_sandbox
export STUB_PIP_RC=1
OUT=$(run_script); RC=$?
unset STUB_PIP_RC
if [[ "$RC" != "0" ]]; then
  ok "装依赖失败以非零退出（${RC}）"
else
  no "装依赖失败却返回成功"
fi
if calls_has 'alembic upgrade head'; then
  no "依赖没装好就迁移了库 —— 迁移脚本要靠新代码跑，依赖不齐就是跑不通"
else
  ok "依赖没装好就不迁移"
fi
if calls_has "systemctl restart"; then
  no "装依赖失败却重启了服务"
else
  ok "装依赖失败没有重启"
fi
if grep -q "OLD backend content" "$APP/backend/app.py" 2>/dev/null; then
  ok "装依赖失败没有切换后端代码"
else
  no "装依赖失败却切换了后端代码"
fi
if grep -q "old frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "装依赖失败没有切换前端产物"
else
  no "装依赖失败却切换了前端产物"
fi

# 目标库不确定时必须**停**，而不是猜。
# pydantic 的 env_file 是相对 cwd 的（app/core/config.py），而迁移现在从
# 暂存目录跑 —— 没有现网 .env 时它会退回源码里写死的 localhost:15432/cwq。
# 把迁移打到另一个库上不可挽回（backend/alembic.ini 开头的注释说的就是它）。
setup_sandbox
rm -f "$APP/backend/.env"
OUT=$(run_script_no_db_url); RC=$?
if [[ "$RC" != "0" ]] && grep -q "无法确认迁移会打在哪个库上" <<<"$OUT"; then
  ok "无法确定目标库时拒绝迁移（不退回源码默认值）"
else
  no "没有 .env 也没有 DATABASE_URL 时仍然往下走了（RC=${RC}）"
fi
if calls_has 'alembic upgrade head'; then
  no "目标库不确定却仍然跑了迁移"
else
  ok "目标库不确定时 alembic upgrade head 一次都没跑"
fi

# --------------------------------------------------------------------------
# unit 按本机布局渲染
#
# 这一段对应的是一个**每次发版都会重演**的生产故障，而不是一次性的笔误：
# deploy.sh 原来把 deploy/systemd/ 原样 cp 过去，而模板里的路径写死
# /opt/cwq。于是 APP_DIR / BACKUP_DIR 明明是可配的，手工修对的路径却会在
# 下一次发版被打回旧值（2026-10-19 生产上 cwq-backup.* 就是这么被覆盖坏的，
# timer 从此每天 Failed to load environment files）。
#
# 沙箱本来就把 APP_DIR / BACKUP_DIR 指到 /tmp 下的路径，所以判据不需要额外造主机：
# 装出来的 unit 里只要还剩一个 /opt/cwq，原样拷贝就立刻现形。
#
# 断言的是**内容**而不是「文件存在」—— 第 10 节只断言了后者，
# 所以这个 bug 从来没被测出来过。
# --------------------------------------------------------------------------
echo
echo "== 15. 定时备份 unit 按本机布局渲染，不把 /opt/cwq 灌回来 =="
setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1

UNIT_S="$DEPDIR/units/cwq-backup.service"
UNIT_T="$DEPDIR/units/cwq-backup.timer"
# BACKUP_DIR 只在 run_script 的子 shell 里传给 deploy.sh，本层拿不到它；
# 这里用同一个来源重新拼一份，否则断言会去比对一个空字符串而恒真。
BKDIR="$DEPDIR/backups"

if [[ ! -f "$UNIT_S" || ! -f "$UNIT_T" ]]; then
  no "unit 没装到 ${DEPDIR}/units —— 渲染断言无从谈起"
else
  for u in "$UNIT_S" "$UNIT_T"; do
    if grep -q '/opt/cwq' "$u"; then
      no "$(basename "$u") 里还留着 /opt/cwq —— 原样拷贝会把旧布局灌回本机"
    else
      ok "$(basename "$u") 里没有 /opt/cwq"
    fi
  done

  # WorkingDirectory / EnvironmentFile / ExecStart 都应指向本机应用根。
  for key in WorkingDirectory EnvironmentFile ExecStart; do
    ln=$(grep -m1 "^${key}=" "$UNIT_S" || true)
    if [[ "$ln" == *"$APP"* ]]; then
      ok "${key} 已渲染为本机应用根"
    else
      no "${key} 没指向本机应用根（实际：${ln:-<无该行>}）"
    fi
  done

  # ReadWritePaths 必须落到 BACKUP_DIR。**不能是 $APP/backups** ——
  # 替换 /opt/cwq/backups 时若把更短的 /opt/cwq 先替换掉，结果就是后者，
  # 运维单独配的 BACKUP_DIR 会被悄悄忽略，而备份目录写在 unit 的日志重定向里，
  # 表现是 unit 加载失败而不是写错地方，更难一眼看出来。
  rwp=$(grep -m1 '^ReadWritePaths=' "$UNIT_S" || true)
  if [[ "$rwp" == *"$BKDIR"* && "$rwp" != *"$APP/backups"* ]]; then
    ok "ReadWritePaths 用的是运维给的 BACKUP_DIR"
  else
    no "ReadWritePaths 不是 BACKUP_DIR（实际：${rwp:-<无该行>}）"
  fi

  # 日志重定向同样落在备份目录上：渲染漏掉它，unit 会因为父目录不可写而加载失败。
  if grep -q "StandardOutput=append:$BKDIR/" "$UNIT_S"; then
    ok "备份日志重定向也渲染到了 BACKUP_DIR"
  else
    no "备份日志重定向没有跟着渲染（实际：$(grep -m1 '^StandardOutput=' "$UNIT_S" || echo '<无该行>')）"
  fi

  # timer 的 Documentation=file:... 指向 tuning.md，同样是写死的应用根。
  if grep -q "Documentation=file:$APP/deploy/tuning.md" "$UNIT_T"; then
    ok "timer 的 Documentation 也渲染到了本机应用根"
  else
    no "timer 的 Documentation 没渲染（实际：$(grep -m1 '^Documentation=' "$UNIT_T" || echo '<无该行>')）"
  fi

  # 关停标记必须**在渲染之前**短路：标记在时连 unit 都不该写，
  # 写过就等于「只 stop 没 disable」，重启后 timer 照样自己起来。
  setup_sandbox
  : > "$APP/.backup-timer-off"
  run_script >/dev/null
  if [[ -f "$DEPDIR/units/cwq-backup.service" ]]; then
    no "有关停标记却仍然写了 unit —— 重启后 timer 会自己复活"
  else
    ok "有关停标记时 unit 没被写入（渲染前就短路了）"
  fi
fi

echo
echo "== 16. 前端切换是**一次 rename**（原子），不是「旧的让开、新的顶上」 =="
# --------------------------------------------------------------------------
# 这一组是 P2-2 的回归测试。
#
# 原来的写法是两次 mv：
#     mv "$APP/frontend/dist" "$FRONT_OLD"     # 旧的让开
#     mv "$FRONT_IN/dist" "$APP/frontend/dist" # 新的顶上
# 两次 rename(2) 之间 dist **整个不存在** —— nginx 的 root 指的就是它，
# 那段时间里所有静态资源返回 404。而脚本当时**一个 trap 都没有**，
# 被打断（SSH 断连、systemd 超时）落在两行之间就是整站白屏，且没有
# 任何自动恢复路径。
#
# 现在改成「版本化目录 + 一次 rename 换软链」。断言分三层：
#   1. 结构：dist 是软链，指向 releases/dist-<sha>-<时间戳>；
#   2. 静态：脚本里**不再有**「把 dist 挪走」这种写法，也不再用
#      非原子的 ln -sfn；
#   3. 行为：切换失败时 trap 把上一版换回去（见第 17 组）。
# --------------------------------------------------------------------------
setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1

if [[ -L "$APP/frontend/dist" ]]; then
  ok "前端 dist 是软链（版本化布局）"
else
  no "前端 dist 不是软链 —— 用的还是「两次 mv」的老写法"
fi
DIST_LINK=$(readlink "$APP/frontend/dist" 2>/dev/null || echo "")
if [[ "$DIST_LINK" == releases/dist-* ]]; then
  ok "dist 指向 releases/ 下的版本目录（${DIST_LINK}）"
else
  no "dist 指向的不是版本目录：${DIST_LINK:-<读不到>}"
fi
if grep -q "new frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "软链指过去的那份就是新产物"
else
  no "软链指过去的内容不是新产物"
fi
# 上一版的 release 目录必须还在：回退靠它，而且旧前端引用的
# /assets/* 只有它还在盘上才打得开。
if ls -d "$APP/frontend/releases"/dist-* >/dev/null 2>&1; then
  ok "版本化目录已建立（releases/dist-*）"
else
  no "releases/ 下没有任何版本目录"
fi

# **静态断言**：这几条是「回归也抓得住」的关键。
# 行为断言只能证明「这一次切换是对的」，静态断言能证明「写法本身
# 不会退化回两次 mv」。
#
# **只看真正的命令（去掉注释行）**：注释里写着「为什么不用 ln -sfn」
# 本身不该被判成「用了 ln -sfn」。
#
# **用 `grep -c` 而不是 `grep -q`**：管道 + `set -o pipefail` 下，
# `grep -q` 一匹配就退出 → 上游的 grep 收到 SIGPIPE → 整条管道返回非零 →
# 「匹配到了」反而被判成「没匹配」。`-c` 会把输入读完，没有这个问题。
code_only(){ grep -vE '^[[:space:]]*#' "$SCRIPT"; }
code_count(){ code_only | grep -cE "$1" || true; }

if [[ "$(code_count 'mv[[:space:]]+"\$FRONT_IN/dist"[[:space:]]+"\$APP/frontend/dist"')" != "0" ]]; then
  no "脚本里仍有「把新内容 mv 进线上 dist」的写法（两次 mv = 中间有窗口）"
else
  ok "脚本里没有「把新内容 mv 进线上 dist」的写法"
fi
# **只查换软链的那个函数**：脚本里另有一处 ln -sfn（给暂存目录链 .env），
# 那一处链的不是对外服务的路径，语义完全不同，不能混为一谈。
swap_body=$(awk '/^atomic_symlink_swap\(\)/,/^}/' "$SCRIPT")
if [[ "$(grep -cE 'ln[[:space:]]+-[a-zA-Z]*f' <<<"$swap_body")" != "0" ]]; then
  no "换软链用了 ln -f —— 那是 unlink + symlink，中间链接是空的，不是原子的"
else
  ok "换软链没有用非原子的 ln -f/-n"
fi
if [[ "$(code_count 'atomic_symlink_swap')" == "0" ]]; then
  no "脚本里找不到原子换软链的实现"
else
  ok "脚本用一次 rename 完成软链切换（atomic_symlink_swap）"
fi
# **用 code_count 而不是 `code_only | grep -q`**（同一个 SIGPIPE 坑）：
# code_only 要把整份 deploy.sh（50KB+）写进管道，下游 grep -q 命中第一行
# 就退出 → 上游吃 SIGPIPE → pipefail 下整条管道非零 → 「装了 trap」被判成
# 「一个 trap 都没有」。macOS 上可能侥幸通过，Linux CI 上稳定复现。
if [[ "$(code_count '^[[:space:]]*trap[[:space:]]')" != "0" ]]; then
  ok "脚本装了 trap（被打断时能退回去）"
else
  no "脚本一个 trap 都没有 —— 被打断就停在半路"
fi

echo
echo "== 17. 切换失败 / 重启失败时，trap 把上一版换回去 =="
# --------------------------------------------------------------------------
# 「有 trap」不等于「trap 管用」。这一组真的把切换打挂（mv 桩在换软链
# 那一步失败），然后断言线上仍然是上一版。
# --------------------------------------------------------------------------
# 情形一：第一次发布（旧布局：dist 是真实目录）切换失败
setup_sandbox
OUT=$(STUB_MV_FAIL_SWAP=1 run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "切换失败以非零退出（${RC}）"
else
  no "切换失败却返回成功"
fi
if grep -q "old frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "切换失败后线上仍是上一版前端（trap 退回去了）"
else
  no "切换失败后前端不是上一版：$(cat "$APP/frontend/dist/index.html" 2>/dev/null || echo '<dist 不存在>')"
fi
if [[ -e "$APP/frontend/dist/index.html" ]]; then
  ok "切换失败后 dist 仍然指得到内容（没有留下「dist 整个不存在」）"
else
  no "切换失败后 dist 不存在 —— 整站 404"
fi
if grep -q "OLD backend content" "$APP/backend/app.py" 2>/dev/null; then
  ok "切换失败后后端代码是上一版"
else
  no "切换失败后后端代码不是上一版"
fi
if grep -q "回退" <<<"$OUT"; then
  ok "明确报出了回退动作"
else
  no "没有报出回退动作"
fi

# 情形二：已经是版本化布局，再发一版（sha 不同）且切换失败 → 退回上一版
setup_sandbox
run_script >/dev/null
assert_ran "首次发布" || exit 1
FIRST_LINK=$(readlink "$APP/frontend/dist" 2>/dev/null || echo "")
use_sha "def456abc123"
OUT=$(STUB_MV_FAIL_SWAP=1 run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "第二次发布切换失败以非零退出（${RC}）"
else
  no "第二次发布切换失败却返回成功"
fi
NOW_LINK=$(readlink "$APP/frontend/dist" 2>/dev/null || echo "")
if [[ "$NOW_LINK" == "$FIRST_LINK" ]]; then
  ok "切换失败后软链指回上一版（${NOW_LINK}）"
else
  no "切换失败后软链指向 ${NOW_LINK:-<读不到>}，不是上一版 ${FIRST_LINK}"
fi
if grep -q "new frontend abc123def456" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "切换失败后线上跑的是上一版的内容（不是这一版的）"
else
  no "切换失败后线上内容不对：$(cat "$APP/frontend/dist/index.html" 2>/dev/null || echo '<读不到>')"
fi

# 情形三：重启失败（服务起不来）→ 代码与前端都退回上一版
setup_sandbox
run_script >/dev/null
assert_ran "首次发布" || exit 1
use_sha "aaa111bbb222"
OUT=$(STUB_RESTART_RC=1 run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "重启失败以非零退出（${RC}）"
else
  no "重启失败却返回成功"
fi
if grep -q "new frontend abc123def456" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "重启失败后前端退回上一版"
else
  no "重启失败后前端不是上一版"
fi
if grep -q "OLD backend content" "$APP/backend/app.py" 2>/dev/null; then
  ok "重启失败后后端代码退回上一版"
else
  no "重启失败后后端代码不是上一版"
fi
# 库已经迁移过 → 回退报告必须**说清楚**库没有回退，以及为什么
if grep -q "没有回退" <<<"$OUT"; then
  ok "明确说明「库已迁移但没有回退」"
else
  no "没有说明库的状态（运维会以为库也退回去了）"
fi
if grep -q "不要直接重启服务" <<<"$OUT"; then
  ok "明确警告「不要直接重启服务」（上一版代码拒绝在新 schema 上启动）"
else
  no "没有警告重启的风险"
fi

# 情形四：同一份产物**再发一次**（上次发布失败后重跑）——
# 不能把当前正在服务的那份 release 删掉。
#
# **这条钉的是一个真实会踩的坑**：版本目录如果只按 sha 命名（dist-<sha>），
# 第二次发布开头的 `rm -rf` 就会把**正在服务的那一份**删掉 —— 而切换还没
# 发生，dist 软链指着一个不存在的目录 → 整站 404；这次发布要是又失败在
# 前半段，站点就一直白着。目录名带时间戳之后就不会。
setup_sandbox
run_script >/dev/null
assert_ran "首次发布" || exit 1
FIRST_LINK=$(readlink "$APP/frontend/dist" 2>/dev/null || echo "")
export STUB_ALEMBIC_RC=1
OUT=$(run_script); RC=$?
unset STUB_ALEMBIC_RC
if [[ -n "$FIRST_LINK" && ! -e "$APP/frontend/$FIRST_LINK" ]]; then
  no "重发同一份产物把正在服务的那份 release 删掉了 —— dist 软链指空，整站 404"
else
  ok "重发同一份产物没有动正在服务的那份 release"
fi
if grep -q "new frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "重发失败后线上仍然是上一版前端"
else
  no "重发失败后线上前端不见了：$(cat "$APP/frontend/dist/index.html" 2>/dev/null || echo '<dist 不存在>')"
fi

echo
echo "== 18. TRUSTED_PROXIES 的告警方向不能反 =="
# --------------------------------------------------------------------------
# 原来的检查只有两分（空 / 非空），而 grep 在**文件不存在**时退出码是 2
# → 走 else → 打印「已配置」。于是「.env 根本不存在」这个更糟的状态
# 被报成了「已处理」—— 运维以为配了、其实没配，IP 维度节流退化成一个
# 全站共用的桶。
# --------------------------------------------------------------------------
setup_sandbox
echo "TRUSTED_PROXIES=127.0.0.1" >> "$APP/backend/.env"
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if grep -q "TRUSTED_PROXIES 已配置" <<<"$OUT"; then
  ok "配了值就报「已配置」"
else
  no "配了值却没报「已配置」"
fi

setup_sandbox
echo "TRUSTED_PROXIES=" >> "$APP/backend/.env"
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if grep -q "TRUSTED_PROXIES 是空的" <<<"$OUT"; then
  ok "配成空值就报「是空的」"
else
  no "空值没被识别出来"
fi

setup_sandbox
grep -v "DATABASE_URL" "$APP/backend/.env" > "$APP/backend/.env.tmp" || true
mv "$APP/backend/.env.tmp" "$APP/backend/.env"
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if grep -q "没有.*TRUSTED_PROXIES" <<<"$OUT"; then
  ok ".env 里没有这一项时报「没有 TRUSTED_PROXIES」"
else
  no ".env 里没有这一项时没有告警"
fi

# **关键的一条**：.env 不存在时必须说「无法确认」，**不能**说「已配置」。
setup_sandbox
rm -f "$APP/backend/.env"
OUT=$(run_script_with_db_url)
assert_ran "全量发布" || exit 1
if grep -q "无法确认" <<<"$OUT"; then
  ok ".env 不存在时报「无法确认」"
else
  no ".env 不存在时没有说「无法确认」"
fi
if grep -q "TRUSTED_PROXIES 已配置" <<<"$OUT"; then
  no ".env 不存在却报「已配置」—— 把更糟的状态报成了已处理"
else
  ok ".env 不存在没有误报「已配置」"
fi

echo
echo "== 19. 断言管道里不许出现 grep -q / grep|head（SIGPIPE 会把「匹配到了」判成「没匹配」）==="
# --------------------------------------------------------------------------
# 这条**不测 deploy.sh，测这份脚本自己**。
#
# 2026-10-21 的 CI 就是这么红的：deploy.sh 明明装了 trap（231/234/235 行），
# 断言却说「脚本一个 trap 都没有」—— 判的不是脚本，是这条断言自己的管道：
# `code_only | grep -q` 里，下游命中第一行就退出，上游 grep 写 50KB 时
# 收到 SIGPIPE，`set -uo pipefail` 下整条管道返回非零 → 「匹配到了」
# 被判成「没匹配」。macOS 上可能侥幸通过，Linux CI 上稳定复现。
#
# **守卫只查它自己之前的部分**：这条检查自己就带着 `| grep` 的形状，
# 查全文件会自己命中自己。
# --------------------------------------------------------------------------
SELF=${BASH_SOURCE[0]}
GUARD_LINE=$(grep -m1 -n "== 19. 断言管道里不许出现" "$SELF" | cut -d: -f1)
# **只看代码行**：注释里提到 `| grep -q` 是为了说明这个坑，不该自己被算进去
# （跟 deploy.sh 那边 code_only 剥注释是同一个道理）。
HEAD_PART=$(awk -v lim="$GUARD_LINE" 'NR<lim' "$SELF" | grep -vE '^[[:space:]]*#')
# 管道里出现 `grep -q`（-q 命中即退 → 上游吃 SIGPIPE）
Q_PIPE=$(grep -cE '\|[[:space:]]*grep[[:space:]]+(-[a-zA-Z]+)*[[:space:]]*-?q' <<<"$HEAD_PART" || true)
if [[ "$Q_PIPE" == "0" ]]; then
  ok "断言管道里没有 grep -q（不会有「匹配到了」被判成「没匹配」）"
else
  no "断言管道里仍有 grep -q（第 ${Q_PIPE} 处）—— Linux 上会稳定误判"
fi
# `grep ... | head -1`：head 读一行就退，上游可能被 SIGPIPE 打断，
# 那条还没写出来的匹配行就丢了（断言拿到空串）。
G_HEAD=$(grep -cE 'grep[^|]*\|[[:space:]]*head' <<<"$HEAD_PART" || true)
if [[ "$G_HEAD" == "0" ]]; then
  ok "断言里没有「grep | head」（不会被 head 提前退出打断）"
else
  no "断言里仍有「grep | head」（第 ${G_HEAD} 处）—— 可能丢掉匹配行"
fi

echo
echo "== 20. 动静分离：运行时配置在发布时装配 =="
# --------------------------------------------------------------------------
# 这一步的意义：**一份产物装到不同环境**。CI 每个 commit 只构建一份产物，
# 而「API 在哪」「静态资源在哪」每套部署都不同 —— 所以它们必须在**发布时**
# 写进产物，而不是烤进构建产物里。
#
# 断言的是**产物里的实际内容**（dist/runtime-config.js、dist/index.html），
# 不是「脚本被调用过」。后者在装配逻辑写错时照样是绿的。
# --------------------------------------------------------------------------

# 默认（同源部署）：配置是同源的，资源地址不动
setup_sandbox
OUT=$(run_script)
assert_ran "同源全量发布" || exit 1
if calls_has "emit_frontend_runtime.py"; then
  ok "发布时装配了运行时配置（用的是这一版产物里的脚本）"
else
  no "没有调用运行时配置装配脚本"
fi
if [[ -f "$APP/frontend/dist/runtime-config.js" ]]; then
  ok "产物里有 runtime-config.js"
else
  no "产物里没有 runtime-config.js —— 前端读不到配置"
fi
if grep -cE '"apiBase":"/api"' "$APP/frontend/dist/runtime-config.js" >/dev/null 2>&1; then
  ok "同源部署的 apiBase 是 /api（与从前完全一致）"
else
  no "同源部署的 apiBase 不对：$(cat "$APP/frontend/dist/runtime-config.js" 2>/dev/null)"
fi
if grep -cE 'src="/assets/' "$APP/frontend/dist/index.html" >/dev/null 2>&1; then
  ok "同源部署的资源地址仍是 /assets/（没有被改成别的源）"
else
  no "同源部署的资源地址被改了 —— 不该动的地方动了"
fi

# CDN 形态：资源指向 CDN、配置脚本指向源站
setup_sandbox
OUT=$(run_script_cdn)
assert_ran "CDN 形态全量发布" || exit 1
if grep -cE 'src="https://cdn\.example\.com/assets/' "$APP/frontend/dist/index.html" >/dev/null 2>&1; then
  ok "index.html 的资源地址指向 CDN"
else
  no "index.html 的资源地址没有指向 CDN：$(grep -oE 'src="[^"]*"' "$APP/frontend/dist/index.html" 2>/dev/null | tr '\n' ' ')"
fi
if grep -cE 'href="https://cdn\.example\.com/assets/' "$APP/frontend/dist/index.html" >/dev/null 2>&1; then
  ok "CSS 的地址也指向 CDN（不是只有 JS）"
else
  no "CSS 仍指向源站 —— 静态分离只做了一半"
fi
if grep -cE '"assetBase":"https://cdn\.example\.com/"' "$APP/frontend/dist/runtime-config.js" >/dev/null 2>&1; then
  ok "运行时配置里的 assetBase 指向 CDN"
else
  no "运行时配置的 assetBase 不对：$(cat "$APP/frontend/dist/runtime-config.js" 2>/dev/null)"
fi
if grep -cE 'src="https://acct\.example\.com/runtime-config\.js"' "$APP/frontend/dist/index.html" >/dev/null 2>&1; then
  ok "配置脚本指向源站（配置的唯一真源在源站，不跟着 CDN 缓存走）"
else
  no "配置脚本没有指向源站：$(grep -oE 'src="[^"]*runtime-config[^"]*"' "$APP/frontend/dist/index.html" 2>/dev/null)"
fi
if grep -cE '/assets/index-' "$APP/frontend/dist/index.html" >/dev/null 2>&1; then
  ok "资源文件名仍带内容哈希（CDN 才能长缓存）"
else
  no "资源地址里看不到内容哈希"
fi

# ---------------------------------------------------------------------------
# 目标机守卫：只在 43.138.178.174 上部署
# ---------------------------------------------------------------------------
# 这组断言验的是**发布被拦住**这件事本身，不是「拦的时候日志长什么样」。
#
# 为什么要有它：本项目历史上部署过三台机，前两台已清理。清理只解决
# 「旧机上没有东西了」，解决不了「有人又往旧机上发一次版」。
# 一旦发版成功，两台机就各自推进一份库、各自领一遍调度任务 ——
# 那是最难查的一类故障。所以这道闸必须**有测试盯着**，
# 否则哪天有人为了「先跑起来」把它注释掉，没人会发现。
echo
echo "== 目标机守卫 =="

# 守卫的判据是「本机出口 IP」。沙箱里 curl 是桩（只记录调用、不返回真 IP），
# 所以用 CWQ_HOST_IP 注入 —— 守卫专门支持这个变量，就是为了让
# 「识别成某台机时的行为」可以被确定地测出来。
#
# 这几组用例验的是**发布被拦住**这件事本身，不是日志长什么样。
# 为什么必须有它：本项目历史上部署过三台机，前两台已清理。清理只解决
# 「旧机上没有东西了」，解决不了「有人又往旧机上发一次版」。
# 一旦发版成功，两台机就各自推进一份库、各自领一遍调度任务 ——
# 那是最难查的一类故障。所以这道闸必须**有测试盯着**，
# 否则哪天有人为了「先跑起来」把它注释掉，没人会发现。

# 在老机上发布 → 必须拒绝
setup_sandbox
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_HOST_IP=8.155.171.57 \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if [[ "${RC}" != "0" ]] && grep -qE "已下线的老生产机" <<<"$OUT"; then
  ok "在已下线的旧机（8.155.171.57）上发布被拒绝（退出码 ${RC}）"
else
  no "在旧机上发布没有被拒绝 —— 守卫形同虚设（退出码 ${RC}）"
fi

# 更早那台（/opt/cwq 布局）同样必须被拒绝
setup_sandbox
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_HOST_IP=110.42.44.53 \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if [[ "${RC}" != "0" ]] && grep -qE "已下线的老生产机" <<<"$OUT"; then
  ok "在已下线的旧机（110.42.44.53）上发布被拒绝（退出码 ${RC}）"
else
  no "在旧机上发布没有被拒绝 —— 守卫形同虚设（退出码 ${RC}）"
fi

# 被拒绝的发布不能有任何副作用
setup_sandbox
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_HOST_IP=8.155.171.57 \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if calls_has "alembic upgrade head"; then
  no "被拒绝的发布仍然往下走了（改了库）"
else
  ok "被拒绝的发布没有产生任何副作用（没碰库）"
fi

# **授权不能推翻禁令**：把生产机的授权标记拷到老机上，也必须被拒。
# 这是「先查下线名单、后看授权标记」这条顺序要保住的理由。
setup_sandbox
printf '43.138.178.174\n' > "$DEPDIR/target-marker"
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_HOST_IP=8.155.171.57 CWQ_DEPLOY_TARGET_FILE="$DEPDIR/target-marker" \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if [[ "${RC}" != "0" ]] && grep -qE "已下线的老生产机" <<<"$OUT"; then
  ok "授权标记被拷到老机上仍然被拒（授权推翻不了禁令）"
else
  no "拷过去的授权标记放行了旧机 —— 下线名单被绕过了（退出码 ${RC}）"
fi

# 标记文件与本机不符时也必须拒 —— 那是「以为在 A 机、实际在 B 机」。
setup_sandbox
printf '203.0.113.99\n' > "$DEPDIR/target-marker"
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_HOST_IP=10.1.0.15 CWQ_DEPLOY_TARGET_FILE="$DEPDIR/target-marker" \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if [[ "${RC}" != "0" ]] && grep -qE "目标机授权标记与本机不符" <<<"$OUT"; then
  ok "标记文件与本机不符时发布被拒绝"
else
  no "标记文件写着别的 IP 仍然放行了 —— 这正是「以为在 A 机、实际在 B 机」（退出码 ${RC}）"
fi

# 生产机（标记与本机一致）→ 放行
setup_sandbox
printf '43.138.178.174\n' > "$DEPDIR/target-marker"
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_HOST_IP=43.138.178.174 CWQ_DEPLOY_TARGET_FILE="$DEPDIR/target-marker" \
        CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if grep -qE "目标机授权标记：43\.138\.178\.174" <<<"$OUT"; then
  ok "生产机上发布被放行"
else
  no "生产机被误拦了 —— 这道闸会挡住正常发布（退出码 ${RC}）"
fi

# 显式放行：沙箱演练要能跑通。
setup_sandbox
OUT=$(run_script --check-only); RC=$?
if [[ "${RC}" == "0" ]]; then
  ok "CWQ_ALLOW_ANY_HOST=1 时沙箱演练照常通过"
else
  no "沙箱演练被守卫挡住了 —— 测试与演练没法做（退出码 ${RC}）"
fi

# --------------------------------------------------------------------------
# 10.8 日志轮转规则：**自动装**，且路径按本机布局渲染
# --------------------------------------------------------------------------
#
# 为什么这是断言而不是「顺手做一下」：
#   unit 里是 ``StandardOutput=append:…/api.log`` —— append 模式**不轮转**，
#   没有任何东西管它的大小，日志只增不减。以前这一步是手工的（tuning.md 11.3），
#   于是它有两个坏处：靠人记得（换机就漏），以及规则里写死路径（写错就
#   盯着一个不存在的路径，**看着装了、其实没转**）。
# --------------------------------------------------------------------------
echo "== 10.8 日志轮转规则：自动装、路径渲染 =="

setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if [[ -f "$DEPDIR/logrotate/cwq-api" && -f "$DEPDIR/logrotate/cwq-backup" ]]; then
  ok "两条 logrotate 规则都装上了"
else
  no "logrotate 规则没装上（实际：$(ls "$DEPDIR/logrotate" 2>/dev/null | tr '\n' ' ')）"
fi
if grep -q '/opt/cwq' "$DEPDIR/logrotate/cwq-api" 2>/dev/null; then
  no "logrotate 规则里还留着 /opt/cwq —— 盯的是一个不存在的路径"
else
  ok "logrotate 规则按本机布局渲染（没有 /opt/cwq）"
fi
if grep -q "$APP/backend/logs/api.log" "$DEPDIR/logrotate/cwq-api" 2>/dev/null; then
  ok "API 日志路径渲染到了本机应用根"
else
  no "API 日志路径没渲染到本机应用根"
fi
if grep -q "$DEPDIR/backups" "$DEPDIR/logrotate/cwq-backup" 2>/dev/null; then
  ok "备份日志路径渲染到了本机备份目录"
else
  no "备份日志路径没渲染到本机备份目录 —— oss-backup.log / backup.log 无人管"
fi
if grep -q "日志轮转规则已装" <<<"$OUT"; then
  ok "发布日志里明确报出日志轮转已装"
else
  no "发布日志没有报出日志轮转的状态"
fi

# --------------------------------------------------------------------------
# 10.8.1 journald 的上限：**显式写死**，不靠默认值
# --------------------------------------------------------------------------
#
# systemd 的默认值是「磁盘的 10%」（上限 4G）。这台机磁盘 49 G ⇒ 约 4.9 G，
# 被 4 G 封顶 —— 够大到把备份产物的地盘挤掉，而那 4 G 里绝大部分是几个月前
# 没人再看的启动日志。
# --------------------------------------------------------------------------
echo "== 10.8.1 journald 上限：自动装 =="

setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if [[ -f "$DEPDIR/journald/cwq.conf" ]]; then
  ok "journald 上限配置装上了"
else
  no "journald 上限没装上 —— journal 会涨到磁盘的 10%"
fi
if grep -q "SystemMaxUse=" "$DEPDIR/journald/cwq.conf" 2>/dev/null; then
  ok "journald 有明确的大小上限"
else
  no "journald 配置里没有 SystemMaxUse —— 靠默认值就是靠磁盘大小"
fi

# --------------------------------------------------------------------------
# 10.9 应用调度器接管离站备份 ⇒ **停掉旧的 systemd timer**
# --------------------------------------------------------------------------
#
# 两条路径干的是同一件事（ARCHITECTURE §11.13 的硬约束）。同时启用 = 每天
# 备份两次 = 同一份数据传两遍 + 备份记录每天多 10 行。
#
# 判据是「最近有没有真的成功跑过」（scripts/scheduler_state.py），
# 不是「触发密钥配了没有」—— 密钥配了而外部 cron 没部署的情况下停掉旧
# timer 就等于离站备份彻底没了。判错的方向是安全的：最多多备一次。
# --------------------------------------------------------------------------
echo "== 10.9 应用调度器接管离站备份时，旧 timer 被停用 =="

setup_sandbox
OUT=$(STUB_SCHEDULER_OWNS_RC=0 run_script)
assert_ran "全量发布" || exit 1
if calls_has "systemctl disable --now cwq-oss-backup.timer"; then
  ok "应用调度器在管离站备份时，旧 timer 被停用"
else
  no "应用调度器已经在做离站备份，旧 timer 仍然开着 —— 每天备份两次"
fi
if calls_has "systemctl enable --now cwq-oss-backup.timer"; then
  no "旧 timer 在被停用的同时又被启用了 —— 那等于什么都没做"
else
  ok "旧 timer 没有被同时启用"
fi
if grep -q "离站备份由应用内调度器接管" <<<"$OUT"; then
  ok "发布日志里说清了为什么停用"
else
  no "发布日志没有说明为什么停用旧 timer"
fi

# 反过来：应用调度器**没**在管（probe 返回非 0）⇒ 旧 timer 照旧启用。
# 判错的方向必须是「多备一次」而不是「一次都不备」。
setup_sandbox
OUT=$(STUB_SCHEDULER_OWNS_RC=1 run_script)
assert_ran "全量发布" || exit 1
if calls_has "systemctl enable --now cwq-oss-backup.timer"; then
  ok "应用调度器没在管时，旧 timer 仍然启用（不会漏备）"
else
  no "应用调度器没在管时旧 timer 也没启用 —— 离站备份彻底没了"
fi
if calls_has "systemctl disable --now cwq-oss-backup.timer"; then
  no "旧 timer 被停用了，但应用调度器明明没在管 —— 这一停就断了一条退路"
else
  ok "旧 timer 没有被误停"
fi

echo
echo "== 10.10 发布脚本同版自检 + \$APP/deploy 的旧副本不再遮蔽新规则 =="
# --------------------------------------------------------------------------
# 真实事故（2026-10-25，生产机）：$APP/deploy 是 2026-10-24 的旧副本，
# 它的 logrotate/ 里只有 cwq-api、**没有 cwq-backup**。而查找逻辑原先是
# 「先 $APP/deploy、没有才看脚本自己旁边那份」—— 于是新加的备份日志轮转
# 规则被旧目录**遮蔽**：发布照样打印「日志轮转规则已装」，其实只装了旧的
# 那一份。**看着做完了，其实没做。**
#
# 两条防线：
#   1. 规则查找改成「先脚本自己旁边那份」—— 那才是本次发布真正在跑的那一版；
#   2. 每次发布把产物里的 deploy/ 铺回 $APP/deploy，让它不再长期是旧副本。
#
# 还有第三道：发布脚本必须与产物同版。否则「CI 的沙箱演练通过」这句话
# 对这次发布就不成立 —— 脚本少做的事，发布界面照样显示成功。
# --------------------------------------------------------------------------

# (a) 跑的是一份被人改过的脚本 ⇒ 中止，且现网一个字节都没动。
setup_sandbox
cp "$SCRIPT" "$DEPDIR/stale-deploy.sh"
echo "# 一份被人动过的旧脚本" >> "$DEPDIR/stale-deploy.sh"
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
  SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
  LOGROTATE_DIR="$DEPDIR/logrotate" JOURNALD_DIR="$DEPDIR/journald" CWQ_ALLOW_ANY_HOST=1 CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
  bash "$DEPDIR/stale-deploy.sh" 2>&1 )
if grep -q "与产物里那一份不是同一个文件" <<<"$OUT"; then
  ok "发布脚本与产物不同版时被拦下"
else
  no "改了发布脚本照样发布 —— CI 验过的行为对这次发布就不成立了"
fi
if grep -q "old frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "拦下之后现网前端没被动过"
else
  no "拦下了却已经把新前端换上去"
fi

# (b) $APP/deploy 是旧副本时，新规则仍然装得上（旧目录不得遮蔽）。
setup_sandbox
mkdir -p "$APP/deploy/logrotate"
echo "# OLD RULE：旧副本里只有 cwq-api" > "$APP/deploy/logrotate/cwq-api"
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if [[ -f "$DEPDIR/logrotate/cwq-backup" ]]; then
  ok "备份日志的轮转规则装上了 —— \$APP/deploy 的旧目录没能遮蔽它"
else
  no "备份日志的轮转规则没装上 —— \$APP/deploy 的旧目录把它遮蔽了"
fi

# (c) 同样的发布之后，$APP/deploy 本身被刷新成与产物同版。
if [[ -f "$APP/deploy/logrotate/cwq-backup" ]]; then
  ok "\$APP/deploy 被刷新成与产物同版"
else
  no "\$APP/deploy 还是旧副本 —— 下次手工跑它就是旧行为"
fi
if cmp -s "$APP/deploy/deploy.sh" "$SCRIPT" 2>/dev/null; then
  ok "\$APP/deploy/deploy.sh 与产物里的同一份"
else
  no "\$APP/deploy/deploy.sh 不是产物里那一份"
fi

# ===========================================================================
# 2026-10-25 审计的回归用例（§3.34 - §3.45）
# ===========================================================================
#
# 这一组每一条都对着审计里**已经复现过**的一条缺陷。
# 写法与前面各组一致：真的把 deploy.sh 跑一遍，然后断言现网的状态。
#
# 沙箱自己的两个前提（它们不是「放水」，是沙箱与生产机的差异）：
#   * CWQ_ALLOW_ANY_HOST=1 —— 目标机守卫（出口 IP 不是生产机）；
#   * CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 —— 应用根不是 /opt/cwq。
# 两条都有**专门的一组用例**证明它们在不放行时确实会拦住发布。

ESA_MTLS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/esa-mtls.sh"
ESA_REALIP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/esa-realip-update.sh"

echo
echo "== 21. §3.34 解包失败：现网毫发无损，且回退确实发生 =="
# --------------------------------------------------------------------------
# **这一条以前完全没有覆盖**：整个测试文件里没有任何 tar 故障注入
# （第 5 组那个「corrupted」产物是在 sha256 校验阶段被拦下的，压根没走到解包）。
#
# 原来的写法是**就地覆盖**现网目录，而 ROLLBACK_SCOPE=code 在 tar **之后**
# 才置位 —— 于是 tar 一失败，on_exit 看到 ROLLBACK_SCOPE=none，
# **一行回退都不做**，现网目录停在「新旧混合、部分文件被截断」的状态。
#
# 现在的写法：先解到暂存目录，再覆盖；回退闸在覆盖**之前**就打开。
# --------------------------------------------------------------------------
setup_sandbox
OUT=$(STUB_TAR_FAIL_CODE=1 run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "解包失败以非零退出（${RC}）"
else
  no "解包失败却返回成功"
fi
if grep -q "OLD backend content" "$APP/backend/app.py" 2>/dev/null; then
  ok "解包失败后现网后端代码**一个字节都没动**（不是新旧混合）"
else
  no "解包失败后现网代码变了：$(cat "$APP/backend/app.py" 2>/dev/null || echo '<不存在>')"
fi
if [[ ! -e "$APP/backend/app/main.py" ]]; then
  ok "新代码没有落进现网目录"
else
  no "新代码已经落进现网目录 —— 解包失败时不该动现网"
fi
if grep -q "回退" <<<"$OUT"; then
  ok "回退确实被触发了（不再是「一行回退都不做」）"
else
  no "解包失败没有触发任何回退 —— 闸门开得太晚"
fi
if grep -q "old frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "前端也没被切（失败发生在切前端之前）"
else
  no "前端被切了 —— 失败点比预期靠后"
fi

echo
echo "== 21.1 §3.34 回退闸必须在「覆盖到现网代码目录」**之前**打开 =="
# --------------------------------------------------------------------------
# 原来这一组只有一条断言：`grep -q "回退" <<<"$OUT"`。
# 它命中的是**依赖回退**那行日志（「↩ 回退运行时依赖：…」）——
# 与「代码回退闸是否在覆盖之前打开」毫无关系。
# 复核方实测：把 `ROLLBACK_SCOPE=code` 从覆盖**之前**挪到覆盖**之后**
# （**注释全留**）→ 5/5 全绿。
#
# 现在钉两件事：
#   (a) **位置**：可执行代码里 `ROLLBACK_SCOPE=code` 的行号必须**小于**
#       「覆盖到现网代码目录」那次调用的行号。
#       用抠取器比 —— 注释与字符串都不算数（deploy.sh 的注释里就写着
#       「ROLLBACK_SCOPE=code」，全文 grep 会命中那一处）。
#   (b) **行为**：注入「覆盖失败」后，现网代码必须回到上一版。
#       闸门开在覆盖之前，覆盖失败时 trap 才拿得到闸门去回退代码；
#       开在覆盖之后 ⇒ 闸门还没开 ⇒ 一行回退都不做 ⇒ 现网停在半新半旧。
# --------------------------------------------------------------------------

# ---- (a) 位置断言 --------------------------------------------------------
# **抠取器先过「已知答案」的自测** —— 一个连自己造的、答案明摆着的样本都
# 分不出来的抠取器，测的是「自己以为的东西」。
mk_fake_gate_script(){
  # $1 = before|after，$2 = 输出文件
  {
    echo '#!/usr/bin/env bash'
    echo '# 这行注释里也写着 ROLLBACK_SCOPE=code —— 抠取器不许把它当代码。'
    echo 'overlay_backend_code(){'
    echo '  cp -a "$1" "$2" || return 1'
    echo '}'
    if [[ "$1" == "before" ]]; then
      echo 'ROLLBACK_SCOPE=code'
      echo 'if ! overlay_backend_code "$CODE_STAGE" "$APP/backend"; then'
      echo '  die "x"'
      echo 'fi'
    else
      echo 'if ! overlay_backend_code "$CODE_STAGE" "$APP/backend"; then'
      echo '  die "x"'
      echo 'fi'
      echo 'ROLLBACK_SCOPE=code'
    fi
  } > "$2"
}
FAKE_BEFORE="$DEPDIR/fake-gate-before.sh"
FAKE_AFTER="$DEPDIR/fake-gate-after.sh"
mk_fake_gate_script before "$FAKE_BEFORE"
mk_fake_gate_script after  "$FAKE_AFTER"
GB="$(code_line_no "$FAKE_BEFORE" 'ROLLBACK_SCOPE[[:space:]]*=[[:space:]]*code')"
OB="$(code_line_no "$FAKE_BEFORE" 'overlay_backend_code[[:space:]]+"')"
GA="$(code_line_no "$FAKE_AFTER"  'ROLLBACK_SCOPE[[:space:]]*=[[:space:]]*code')"
OA="$(code_line_no "$FAKE_AFTER"  'overlay_backend_code[[:space:]]+"')"
if [[ -n "$GB" ]] && [[ -n "$OB" ]] && [[ "$GB" -lt "$OB" ]]; then
  ok "抠取器自测：闸门在覆盖之前的样本，被判成「之前」（闸门第 ${GB} 行 / 覆盖第 ${OB} 行）"
else
  no "抠取器自测失败：闸门在覆盖之前却没被判出来（闸门=${GB:-<没找到>} 覆盖=${OB:-<没找到>}）"
fi
if [[ -n "$GA" ]] && [[ -n "$OA" ]] && [[ "$GA" -gt "$OA" ]]; then
  ok "抠取器自测：闸门在覆盖之后的样本，被判成「之后」（闸门第 ${GA} 行 / 覆盖第 ${OA} 行）"
else
  no "抠取器自测失败：闸门在覆盖之后却没被判出来（闸门=${GA:-<没找到>} 覆盖=${OA:-<没找到>}）"
fi
# 抠取器**不许**把注释里那一处当成代码：样本里注释在第 2 行，
# 真正的置位在第 6 行（before 样本）与第 9 行（after 样本）。
if [[ "$GB" == "6" ]] && [[ "$GA" == "9" ]]; then
  ok "抠取器自测：注释里那一处没有被当成代码（判成第 6/9 行，不是第 2 行）"
else
  no "抠取器把注释里的 ROLLBACK_SCOPE=code 当成了代码（判成 ${GB}/${GA}，应为 6/9）"
fi

# 现在量真的 deploy.sh。
GATE_N="$(code_line_no "$SCRIPT" 'ROLLBACK_SCOPE[[:space:]]*=[[:space:]]*code')"
OVL_N="$(code_line_no "$SCRIPT" 'overlay_backend_code[[:space:]]+"')"
if [[ -n "$GATE_N" ]] && [[ -n "$OVL_N" ]] && [[ "$GATE_N" -lt "$OVL_N" ]]; then
  ok "回退闸（ROLLBACK_SCOPE=code，第 ${GATE_N} 行）在覆盖到现网代码目录（第 ${OVL_N} 行）**之前**打开"
else
  no "回退闸不在覆盖之前（闸门=${GATE_N:-<没找到>} 覆盖=${OVL_N:-<没找到>}）—— 闸门开得太晚"
fi

# ---- (b) 行为断言：注入「覆盖失败」⇒ 现网代码必须回到上一版 --------------
setup_sandbox
# cp 桩：**只**在「把暂存目录覆盖到现网 backend」那一步失败。
#
# 判据是**源**在暂存目录（.backend.incoming.*）而**目的**是 backend/scripts/ ——
# 按 glob 顺序 app/ 与 migrations/ 在它**之前**已经铺开，
# 于是失败现场真的是「半新半旧」—— 那正是这条缺陷的后果。
# 回退时的那次 cp（源在 .releases 快照里）不受影响。
REAL_CP="$(command -v cp)"
cat > "$DEPDIR/bin/cp" <<STUB
#!/usr/bin/env bash
# 源与目的要**跳过选项**再取：cp -a SRC DST 里 \$1 是选项。
# （不跳过的话 src 拿到的是 "-a"，判据永远不成立 —— 注入静默失效，
#  用例会「全绿」而其实什么都没测。）
#
# **这段 heredoc 是无引号的**（里面的变量会被展开），所以里面**不能出现反引号** ——
# 反引号会被当成命令替换去执行。
src=""; dest=""
for a in "\$@"; do
  case "\$a" in -*) continue ;; esac
  [ -z "\$src" ] && src="\$a"
  dest="\$a"
done
echo "cp \$*" >> "\$DEPDIR/cp_calls"
if [ "\${STUB_CP_FAIL_OVERLAY:-0}" = "1" ]; then
  case "\$src" in
    */.backend.incoming.*)
      if [ "\$dest" = "\$DEPDIR/app/backend/scripts/" ]; then
        echo "cp FAILED" >> "\$DEPDIR/cp_failures"
        echo "cp: 模拟故障（覆盖到现网代码目录中途失败）" >&2
        exit 1
      fi
      ;;
  esac
fi
exec "$REAL_CP" "\$@"
STUB
chmod +x "$DEPDIR/bin/cp"
: > "$DEPDIR/cp_calls"
: > "$DEPDIR/cp_failures"
OUT=$(STUB_CP_FAIL_OVERLAY=1 run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "覆盖失败时发布失败（${RC}）"
else
  no "覆盖失败却返回成功"
fi
# **注入生效的守卫（两条都要）**：
#   * 覆盖那一步**真的失败了**（不是「压根没触发」）；
#   * 而失败**之前**新代码已经铺到现网（现场真的是半新半旧）。
if [[ -s "$DEPDIR/cp_failures" ]]; then
  ok "注入确实生效（覆盖到现网代码目录那一步真的失败了）"
else
  no "注入没生效：覆盖那一步压根没失败 —— 下面的断言会全部空转"
fi
if grep -qE '\.backend\.incoming\.[^ ]*migrations/\. .*backend/migrations/' "$DEPDIR/cp_calls" 2>/dev/null; then
  ok "新代码在失败之前已经铺到现网（现场是半新半旧）"
else
  no "失败之前新代码没铺到现网 —— 现场不是半新半旧，这条断言测不到东西"
fi
if [[ -f "$APP/backend/migrations/versions/0021_youyou_chart_of_accounts.py" ]] \
   && [[ ! -e "$APP/backend/migrations/versions/0027_plugin_imports.py" ]]; then
  ok "覆盖失败后现网代码回到上一版（新迁移链没留在现网）"
else
  no "覆盖失败后现网代码**没有**回到上一版（半新半旧留在了现网）"
fi
if grep -q "OLD backend content" "$APP/backend/app.py" 2>/dev/null; then
  ok "上一版的代码文件仍在原位"
else
  no "上一版的代码文件不见了"
fi

echo
echo "== 22. §3.35 nginx -t 的输出不许落进固定 /tmp 路径 =="
# --------------------------------------------------------------------------
# shell 的 `>` 跟随符号链接并截断目标，而这两个脚本是以 root 跑的。
# 固定路径 /tmp/nginx-t-*.log ⇒ 本地任意低权账号先 `ln -sf <敏感文件> <那个路径>`，
# 运维随后以 root 跑脚本 → **root 把敏感文件截断成 nginx 的自检输出**。
# --------------------------------------------------------------------------
for f in "$ESA_MTLS" "$ESA_REALIP"; do
  bn="$(basename "$f")"
  # **只看代码行**：注释里写着「原来写的是 >/tmp/nginx-t-mtls.log」是为了
  # 说明这个坑，不该自己被算进去（跟 deploy.sh 那边 code_only 剥注释同理）。
  tmpw=$(grep -vE '^[[:space:]]*#' "$f" | grep -cE '>[[:space:]]*/tmp/' || true)
  if [[ "$tmpw" == "0" ]]; then
    ok "$bn 没有把 nginx 的输出写进固定的 /tmp 路径"
  else
    no "$bn 仍然把 nginx 的输出写进固定的 /tmp 路径（${tmpw} 处）"
  fi
  mk=$(grep -vE '^[[:space:]]*#' "$f" | grep -c 'mktemp' || true)
  if [[ "$mk" != "0" ]]; then
    ok "$bn 用了 mktemp 造临时文件"
  else
    no "$bn 没有用 mktemp —— 临时文件名可被预测"
  fi
  tr=$(grep -vE '^[[:space:]]*#' "$f" | grep -cE '^[[:space:]]*trap[[:space:]]' || true)
  rmf=$(grep -vE '^[[:space:]]*#' "$f" | grep -c 'rm -f' || true)
  if [[ "$tr" != "0" ]] && [[ "$rmf" != "0" ]]; then
    ok "$bn 退出时会清掉临时文件（trap + rm -f）"
  else
    no "$bn 没有在退出时清掉临时文件（trap=${tr} rm -f=${rmf}）"
  fi
done

# 行为级：跑一次 status，临时文件必须**用完就没了**（不留残骸）。
setup_sandbox
cat > "$DEPDIR/bin/mktemp" <<'STUB'
#!/bin/sh
p=$(/usr/bin/mktemp "$@")
echo "$p" >> "$DEPDIR/tmpfiles"
printf '%s' "$p"
STUB
chmod +x "$DEPDIR/bin/mktemp"
: > "$DEPDIR/tmpfiles"
( cd "$DEPDIR" && PATH="$DEPDIR/bin:$PATH" bash "$ESA_MTLS" status >/dev/null 2>&1 )
LEFT=0
while IFS= read -r p; do
  [ -n "$p" ] || continue
  if [ -e "$p" ]; then LEFT=$((LEFT+1)); fi
done < "$DEPDIR/tmpfiles"
if [[ "$LEFT" -eq 0 ]]; then
  ok "esa-mtls.sh 跑完后没有留下任何临时文件"
else
  no "esa-mtls.sh 留下了 ${LEFT} 个临时文件"
fi
if grep -cE '/tmp/nginx-t-mtls\.log' "$DEPDIR/tmpfiles" >/dev/null 2>&1; then
  no "临时文件又落回固定路径 /tmp/nginx-t-mtls.log"
else
  ok "临时文件是随机名，不是固定路径"
fi

echo
echo "== 22.1 §3.35 符号链接攻击：两个脚本都要挡住 =="
# --------------------------------------------------------------------------
# 原来这一组的行为级用例**只跑 esa-mtls.sh**，而且跑的是 `status` ——
# 那条路径**根本不写 nginx 的输出**（status 只读配置）。
# 对 esa-realip-update.sh 则是**零**行为级覆盖。
#
# 而那条字面量 grep（`>[[:space:]]*/tmp/`）只认「`>` **紧邻**路径」的写法：
# 把路径放进变量（`NGINX_T_LOG="/tmp/xxx.log"`）后 `>` 与 `/tmp` 不再相邻
# ⇒ 断言被绕过；而 esa-realip 另有 `TMP="$(mktemp)"` ⇒「用了 mktemp」那条也照样绿。
#
# 现在改成**真的打一次符号链接攻击**：
#   * 受害文件预先放好已知内容，另存一份原件用来逐字节比对；
#   * 把脚本里（**注释里也包括** —— 历史上正是这些注释描述的那个固定路径）
#     出现过的每一个 /tmp 下的字面路径，都做成指向受害文件的符号链接；
#   * 跑脚本，**走到真的写 nginx 输出的那一步**；
#   * 受害文件必须**逐字节未变**。
#
# 攻击的完整形态是：「本地低权账号先 `ln -sf <敏感文件> <那个路径>`，
# 运维随后以 root 跑脚本 → root 把敏感文件截断成 nginx 的自检输出」。
# 这台机是共用主机，这样的账号是存在的。
# --------------------------------------------------------------------------
setup_sandbox
mkdir -p "$DEPDIR/nginx/conf.d"
cat > "$DEPDIR/nginx/conf.d/acct.xins.live.conf" <<CONF
server {
    listen 443 ssl;
    server_name acct.xins.live;
    ssl_verify_client     optional;
    access_log  $DEPDIR/nginx/access.log cwq_mtls;
    root /opt/cwq/frontend/dist;
}
CONF
: > "$DEPDIR/fake-ca.pem"        # ESA 根证书：切强制校验前它必须存在
printf '1.2.3.0/24\n' > "$DEPDIR/ips-ok.txt"
# nginx 桩：**必须真的往 stdout 写东西** ——
# 受害文件被截断的证据就是「它变成了 nginx 的自检输出」。
cat > "$DEPDIR/bin/nginx" <<'STUB'
#!/bin/sh
if [ "$1" = "-T" ]; then
  echo "# configuration file /dev/null:"
  exit 0
fi
if [ "$1" = "-t" ]; then
  echo "nginx: the test configuration is successful (ATTACKER PAYLOAD)"
  exit 0
fi
exit 0
STUB
chmod +x "$DEPDIR/bin/nginx"
# rm 桩在 setup_sandbox 里就装好了（那边有为什么的说明）——
# 本组的负控要造软链、让脚本自己删它，所以**必须**绕开本机那个
# 「可恢复删除」转发器（它删软链会挂住）。

run_esa_mtls(){
  # $1 = 脚本路径。跑 strict —— 那条路径**真的**会把 nginx 的输出写进文件。
  # 配置每次都重造：第一次跑完它已经被改成 on 了，第二次会「无需改动」而
  # **根本走不到 nginx -t** —— 那样攻击就不会发生，负控会假绿。
  cat > "$DEPDIR/nginx/conf.d/acct.xins.live.conf" <<CONF
server {
    listen 443 ssl;
    server_name acct.xins.live;
    ssl_verify_client     optional;
    access_log  $DEPDIR/nginx/access.log cwq_mtls;
    root /opt/cwq/frontend/dist;
}
CONF
  ( cd "$DEPDIR" && PATH="$DEPDIR/bin:$PATH" \
    CWQ_MTLS_CONF="$DEPDIR/nginx/conf.d/acct.xins.live.conf" \
    CWQ_MTLS_CA="$DEPDIR/fake-ca.pem" \
    bash "$1" strict >/dev/null 2>&1 )
}
run_esa_realip(){
  # $1 = 脚本路径
  ( cd "$DEPDIR" && PATH="$DEPDIR/bin:$PATH" \
    CWQ_REALIP_TARGET="$DEPDIR/realip.conf" \
    CWQ_MTLS_CONF="$DEPDIR/nginx/conf.d/acct.xins.live.conf" \
    bash "$1" "$DEPDIR/ips-ok.txt" >/dev/null 2>&1 )
}

for spec in "esa-mtls.sh|$ESA_MTLS|run_esa_mtls" "esa-realip-update.sh|$ESA_REALIP|run_esa_realip"; do
  bn="${spec%%|*}"; rest="${spec#*|}"; f="${rest%%|*}"; runner="${rest##*|}"
  VICT="$DEPDIR/victim-$bn"
  printf 'SECRET-KEY-MATERIAL-DO-NOT-TRUNCATE\n' > "$VICT"
  cp "$VICT" "$VICT.orig"
  # 攻击者：脚本里出现过的每一个 /tmp 字面路径，都做成指向受害文件的符号链接。
  LITLIST=""
  while IFS= read -r l; do
    [ -n "$l" ] || continue
    ln -sf "$VICT" "$l" 2>/dev/null || true
    LITLIST="$LITLIST $l"
  done < <(grep -oE '/tmp/[A-Za-z0-9._/-]+' "$f" | sort -u)
  if [[ -z "${LITLIST// /}" ]]; then
    no "$bn 里找不到任何 /tmp 字面路径 —— 攻击无处可打，这条用例是空转"
  else
    ok "$bn 的攻击面已就位（符号链接：${LITLIST# }）"
  fi
  # ---- 真脚本：受害文件必须逐字节未变 --------------------------------
  "$runner" "$f"
  if cmp -s "$VICT" "$VICT.orig"; then
    ok "$bn 挡住了符号链接攻击（受害文件逐字节未变）"
  else
    no "$bn 被符号链接攻击咬到了：受害文件被改成了——$(head -1 "$VICT" 2>/dev/null)"
  fi
  # ---- 负控：把路径改回固定值，同一套攻击必须咬人 ----------------------
  # 这一步证明的不是「好脚本安全」，而是「**这套攻击真的能咬人**」——
  # 否则上面那些全绿只是因为攻击根本没生效（恒绿测试）。
  FIXED="$(grep -oE '/tmp/[A-Za-z0-9._/-]+' "$f" | sort -u | head -1)"
  if [[ -z "$FIXED" ]]; then
    no "$bn 没有可用的固定路径可用来造负控"
  else
    MUT="$DEPDIR/mutant-$bn.sh"
    # **只改赋值那一行，注释全留** —— 这正是「最阴」的变异形态。
    sed "s|^NGINX_T_LOG=.*|NGINX_T_LOG=\"$FIXED\"|" "$f" > "$MUT"
    printf 'SECRET-KEY-MATERIAL-DO-NOT-TRUNCATE\n' > "$VICT"
    while IFS= read -r l; do
      [ -n "$l" ] || continue
      ln -sf "$VICT" "$l" 2>/dev/null || true
    done < <(grep -oE '/tmp/[A-Za-z0-9._/-]+' "$f" | sort -u)
    "$runner" "$MUT"
    if cmp -s "$VICT" "$VICT.orig"; then
      no "$bn 的负控没咬人：把路径改回固定值（${FIXED}）后受害文件竟然没变 —— 这条用例是空转"
    else
      ok "$bn 的负控咬到了人（路径改回固定值 ${FIXED} 后受害文件被截断）—— 攻击确实有效"
    fi
  fi
  # 清掉攻击者留下的符号链接（只删链接，不删受害文件）。
  # **用沙箱里的 rm 桩** —— 本机运行时那个「可恢复删除」转发器处理软链会挂住。
  for l in $LITLIST; do
    [ -L "$l" ] && "$DEPDIR/bin/rm" -f "$l"
  done
done

# ---- 结构判据：NGINX_T_LOG 的赋值必须来自 mktemp ----------------------
# 原来的字面量 grep（`>[[:space:]]*/tmp/`）只认「`>` 紧邻路径」，
# 路径一旦放进变量就被绕过。改成**解析赋值本身**：
# 值不是 mktemp 造出来的 ⇒ 路径可预测 ⇒ 不合格。
for spec in "esa-mtls.sh|$ESA_MTLS" "esa-realip-update.sh|$ESA_REALIP"; do
  bn="${spec%%|*}"; f="${spec#*|}"
  assign="$(code_line_text "$f" 'NGINX_T_LOG[[:space:]]*=' 'text')"
  if [[ -z "$assign" ]]; then
    no "$bn 里找不到 NGINX_T_LOG 的赋值"
  elif [[ "$assign" == *mktemp* ]]; then
    ok "$bn 的 NGINX_T_LOG 由 mktemp 造出来（路径不可预测）：${assign}"
  else
    no "$bn 的 NGINX_T_LOG 不是 mktemp 造的（路径可预测）：${assign}"
  fi
done

echo
echo "== 23. §3.36 两个备份 unit 都要有 TimeoutStartSec =="
# --------------------------------------------------------------------------
# `Type=oneshot` 的服务**整个执行时长**都算在 TimeoutStartSec 里，
# 而 systemd 的 DefaultTimeoutStartSec 默认只有 90 秒 ——
# 库长到 pg_dump 要超过 90 秒就被 SIGTERM 掐断，
# db_backup.py 走异常路径把临时文件 unlink 掉 → **目录里一个备份都没有**。
#
# 对照：同批的 cwq-deploy.service 早就给了 1800。
# --------------------------------------------------------------------------
for u in cwq-backup.service cwq-oss-backup.service; do
  f="$REPO_ROOT/deploy/systemd/$u"
  if [[ ! -f "$f" ]]; then
    no "$u 不在仓库里 —— 断言无从谈起"
    continue
  fi
  v="$(grep -m1 '^TimeoutStartSec=' "$f" | cut -d= -f2 | tr -dc '0-9')"
  if [[ -n "$v" ]] && [[ "$v" -ge 600 ]]; then
    ok "$u 的 TimeoutStartSec=${v}s（≥600s，不会被 90s 默认值掐断）"
  else
    no "$u 的 TimeoutStartSec 缺失或过小（实际：${v:-<没有这一行>}）"
  fi
done
# 装到机器上的那份也要有（渲染不该把它弄丢）。
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
for u in cwq-backup.service cwq-oss-backup.service; do
  if grep -qE '^TimeoutStartSec=[0-9]{3,}' "$DEPDIR/units/$u" 2>/dev/null; then
    ok "装到机器上的 $u 也带着 TimeoutStartSec"
  else
    no "装到机器上的 $u 没有 TimeoutStartSec"
  fi
done

# 体检还要验**产物**，不只是 timer 开没开。
setup_sandbox
: > "$DEPDIR/backups/db-2026-01-01.sql"     # 0 字节 = 被掐断后留下的空壳
OUT=$(run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "最近一次备份产物是空的时，体检把发布判为失败（${RC}）"
else
  no "备份产物是 0 字节却返回成功 —— 那不是备份，是个空壳"
fi
if grep -q "备份产物是\*\*空的\*\*" <<<"$OUT"; then
  ok "明确报出「备份产物是空的」"
else
  no "没有说清楚是备份产物的问题"
fi
setup_sandbox
printf 'SELECT 1;\n' > "$DEPDIR/backups/db-2026-01-01.sql"
OUT=$(run_script); RC=$?
if [[ "$RC" == "0" ]] && grep -q "非空" <<<"$OUT"; then
  ok "备份产物存在且非空时，体检放行"
else
  no "非空的备份产物反而没放行（${RC}）"
fi

echo
echo "== 24. §3.37 迁移前的 dump 必须走 db_backup.py --out =="
# --------------------------------------------------------------------------
# `> "$DUMP"` 一把梭：shell 重定向**先把文件建出来**，pg_dump 随后才跑。
# 中途失败或被打断 → 目录里留下一个 0 字节/半截的文件，**而文件名看着完全正常**。
# assert_restorable 只查「非空 + 头部特征行」，半截 dump 的头是完整的 → 通过。
#
# 所以判据是：**备份失败之后不能留下任何 dump 文件**。
# --------------------------------------------------------------------------
if grep -vE '^[[:space:]]*#' "$SCRIPT" | grep -cE '>[[:space:]]*"\$DUMP"' >/dev/null 2>&1; then
  no "发布脚本仍然用 shell 重定向写 dump（> \"\$DUMP\"）"
else
  ok "发布脚本不再用 shell 重定向写 dump"
fi
setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
if calls_has "db_backup.py --out"; then
  ok "备份走的是 db_backup.py --out（临时文件 + 非空校验 + 原子改名）"
else
  no "备份没有走 db_backup.py --out"
fi
setup_sandbox
export STUB_BACKUP_RC=1
OUT=$(run_script)
unset STUB_BACKUP_RC
LEFT=""
while IFS= read -r f; do LEFT="$f"; break; done \
  < <(find "$APP/.releases" -name 'database-before-migration.sql' 2>/dev/null)
if [[ -z "$LEFT" ]]; then
  ok "备份失败后**没有留下任何 dump 文件**（不会有半截文件冒充退路）"
else
  no "备份失败却留下了 ${LEFT} —— 那是一个看着正常的假退路"
fi
# 成功的那次：dump 必须在，而且非空。
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
DUMPF=""
while IFS= read -r f; do DUMPF="$f"; break; done \
  < <(find "$APP/.releases" -name 'database-before-migration.sql' 2>/dev/null)
if [[ -n "$DUMPF" ]] && [[ -s "$DUMPF" ]]; then
  ok "备份成功时 dump 存在且非空"
else
  no "备份成功后没有拿到非空的 dump：${DUMPF:-<找不到>}"
fi

echo
echo "== 25. §3.38 打印出来的「回退库」流程必须真的能执行 =="
# --------------------------------------------------------------------------
# 原来打印的是一行 `db_backup.py --restore --file <dump>` ——
# 而 dump 不带 --clean，往一个已经有数据的库里灌会撞 CREATE TABLE already exists，
# 配合 --single-transaction + ON_ERROR_STOP=1 → **整份恢复回滚，一张表都没灌进去**。
# 照抄的运维只会得到一次「恢复失败」。
#
# 所以现在打印的是**完整的两步**，并且明说会丢写入。
# --------------------------------------------------------------------------
setup_sandbox
export STUB_ALEMBIC_RC=1
OUT=$(run_script)
unset STUB_ALEMBIC_RC
# **断言必须限定在「打印出来的那段流程」里。**
#
# 原来三条都是**全文 grep 关键词** ——
#   * 「db_schema_reset.py」被**工具安装日志**命中
#     （「回退辅助脚本已就位（…/app/deploy/scripts/db_schema_reset.py）」）；
#   * 「不可逆」被**别处**命中（on_exit 里「库**已经迁到新版本**……会丢掉
#     备份之后的写入」那句，以及迁移失败时「第 1 步不可逆」那句）。
# 于是把恢复流程里「步骤 1：清空 schema」那一行**整个删掉**（注释全留），
# 三条断言照样全绿 —— 复核方实测 4/4 全绿。
#
# 现在的判据是**步骤标记 + 同现**，而不是关键词有没有出现过：
#   * 「步骤 1：……清空……schema」这一行必须**自己**写着「不可逆」；
#   * 清空 schema 的命令必须落在「步骤 1」与「步骤 2」**之间**；
#   * 恢复 dump 的命令必须落在「步骤 2」**之后**。
# 于是删掉「步骤 1」那一行 ⇒ 下面三条全红。
#
# 注意：流程里还有一行「（步骤 2 的退出码 4 = 数据回来了但 schema 没对齐）」——
# 那是**既有事实**，不是「步骤 1」，所以判据不能靠「哪一行提到 schema」来定。
STEP1="$(grep -m1 -nE '步骤 1：.*清空.*schema' <<<"$OUT" || true)"
STEP1_N="${STEP1%%:*}"
STEP1_TXT="${STEP1#*:}"
if [[ -n "$STEP1_N" ]]; then
  ok "回退流程里有「步骤 1：清空 schema」这一步"
else
  no "回退流程里没有「步骤 1：清空 schema」这一步 —— 照抄执行会撞 CREATE TABLE already exists"
fi
if [[ "$STEP1_TXT" == *"不可逆"* ]]; then
  ok "「不可逆」长在「步骤 1」那一行上（警告长在它该在的地方，不是在别处）"
else
  no "「步骤 1」那一行没有「不可逆」：${STEP1_TXT:-<找不到步骤 1>}"
fi
# 从「步骤 1」往下取 8 行 = 打印出来的那段流程（它是连续的一小段）。
if [[ -n "$STEP1_N" ]]; then
  PROC="$(sed -n "${STEP1_N},$((STEP1_N + 7))p" <<<"$OUT")"
else
  PROC=""
fi
P_RESET="$(grep -m1 -n 'db_schema_reset.py' <<<"$PROC" || true)"
P_STEP2="$(grep -m1 -nE '步骤 2：' <<<"$PROC" || true)"
P_RESTORE="$(grep -m1 -n -- '--restore --file' <<<"$PROC" || true)"
RESET_N="${P_RESET%%:*}"; STEP2_N="${P_STEP2%%:*}"; RESTORE_N="${P_RESTORE%%:*}"
if [[ -n "$RESET_N" ]] && [[ -n "$STEP2_N" ]] && [[ "$RESET_N" -lt "$STEP2_N" ]]; then
  ok "清空 schema 的命令落在「步骤 1」与「步骤 2」之间"
else
  no "清空 schema 的命令不在「步骤 1」与「步骤 2」之间（步骤1=${STEP1_N:-<无>} 清空=${RESET_N:-<无>} 步骤2=${STEP2_N:-<无>}）"
fi
if [[ -n "$RESTORE_N" ]] && [[ -n "$STEP2_N" ]] && [[ "$RESTORE_N" -gt "$STEP2_N" ]]; then
  ok "恢复 dump 的命令落在「步骤 2」之后"
else
  no "恢复 dump 的命令不在「步骤 2」之后（步骤2=${STEP2_N:-<无>} 恢复=${RESTORE_N:-<无>}）"
fi
# 那个被引用的辅助脚本必须真的存在（否则打印出来的是一条假出路）。
if [[ -f "$APP/deploy/scripts/db_schema_reset.py" ]]; then
  ok "回退流程引用的辅助脚本已装到 \$APP/deploy/scripts/"
else
  no "回退流程引用的辅助脚本不存在 —— 打印出来的是一条假出路"
fi

echo
echo "== 26. §3.39 依赖必须跟着代码一起回退 =="
# --------------------------------------------------------------------------
# 依赖是在第 3 步装进**现网那个** venv 的，而回退清单里原本没有 .venv ——
# 于是失败回退只还原代码，**依赖留在新版** → 现网变成「旧代码 + 新依赖」。
# 那个组合表面完全正常（进程还跑着内存里的旧代码），
# 但**下一次 systemctl restart** 就是旧代码配新依赖。
#
# 观测点：venv 里的依赖标记。pip 桩会把它换成新版，回退之后必须回到旧版。
# --------------------------------------------------------------------------
setup_sandbox
export STUB_ALEMBIC_RC=1
OUT=$(run_script)
unset STUB_ALEMBIC_RC
if [[ "$(cat "$APP/backend/.venv/lib/deps-marker" 2>/dev/null)" == "OLD DEPS" ]]; then
  ok "发布失败后 venv 的依赖状态回到上一版"
else
  no "依赖没有跟着回退（现网变成「旧代码 + 新依赖」）：$(cat "$APP/backend/.venv/lib/deps-marker" 2>/dev/null || echo '<读不到>')"
fi
if grep -q "OLD 1.0" "$APP/backend/.venv/lib/site-packages/oldpkg/__init__.py" 2>/dev/null; then
  ok "被升级的那个包也换回了上一版的版本"
else
  no "被升级的包还是新版 —— 依赖集没回到上一版"
fi
# 反过来：发布**成功**时不能把依赖退回去。
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
if [[ "$(cat "$APP/backend/.venv/lib/deps-marker" 2>/dev/null)" == "NEW DEPS" ]]; then
  ok "发布成功时依赖保持新版（没有被误回退）"
else
  no "发布成功却把依赖退回去了"
fi

echo
echo "== 26.1 §3.39(b) 发布**已生效**之后的失败：依赖**不许**回退 =="
# --------------------------------------------------------------------------
# §3.39 的修法（依赖跟着代码一起回退）在**迁移失败**那条路径上是对的：
# 那时现网代码还没换（还是旧版），依赖换回旧版 → 代码与依赖同版本 ✓。
#
# 但它把判据写成了「这次失败了没」—— 而**发布已经生效之后**的失败
# （前端可达性体检不过、TRUSTED_PROXIES 为空）也满足这个判据。
# 那两条路上 ROLLBACK_SCOPE 已经是 none（代码**不回退**，保持新版），
# 于是依赖被换回旧版 → 现网变成**新代码 + 旧依赖**：
#   * 进程还跑着内存里的旧代码（表面完全正常）；
#   * 但**下一次 systemctl restart** 就是新代码配旧依赖 ——
#     那正是 §3.39 的原始失败场景，被 §3.39 自己的修法在另一条路径上引回来。
#
# 判据必须是不变量「**代码与依赖同版本**」，不是「失败了没」。
# 观测点与 §3.39 一样：venv 里的依赖标记 + 现网代码的内容。
# --------------------------------------------------------------------------

# (a) 前端可达性体检失败（§3.41 的 die，落在服务重启**之后**）
setup_sandbox
OUT=$(STUB_FRONTEND_RC=22 run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "前端体检失败时发布失败（${RC}）"
else
  no "前端体检失败却返回成功"
fi
DEPMARK="$(cat "$APP/backend/.venv/lib/deps-marker" 2>/dev/null || echo '<读不到>')"
if [[ "$DEPMARK" == "NEW DEPS" ]]; then
  ok "发布已生效后失败：运行时依赖**没有**被回退（保持新版，与新版代码同版本）"
else
  no "发布已生效后失败：依赖被回退了（现网变成「新代码 + 旧依赖」）：${DEPMARK}"
fi
if grep -q "NEW backend code" "$APP/backend/app/main.py" 2>/dev/null; then
  ok "发布已生效后失败：现网代码仍是新版（没有回退）"
else
  no "发布已生效后失败：现网代码不是新版了"
fi

# (b) TRUSTED_PROXIES 为空（§3.44 的 die，同样在发布生效之后）
setup_sandbox
echo "TRUSTED_PROXIES=" >> "$APP/backend/.env"
OUT=$(run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "TRUSTED_PROXIES 为空时发布被阻断（${RC}）"
else
  no "TRUSTED_PROXIES 为空却返回成功"
fi
DEPMARK="$(cat "$APP/backend/.venv/lib/deps-marker" 2>/dev/null || echo '<读不到>')"
if [[ "$DEPMARK" == "NEW DEPS" ]]; then
  ok "发布已生效后失败（TRUSTED_PROXIES）：运行时依赖**没有**被回退"
else
  no "发布已生效后失败（TRUSTED_PROXIES）：依赖被回退了（新代码 + 旧依赖）：${DEPMARK}"
fi
if grep -q "NEW backend code" "$APP/backend/app/main.py" 2>/dev/null; then
  ok "发布已生效后失败（TRUSTED_PROXIES）：现网代码仍是新版"
else
  no "发布已生效后失败（TRUSTED_PROXIES）：现网代码不是新版了"
fi

# (c) 对称面：迁移失败时**仍然**要回退依赖（§3.39 原行为不能被弄坏）
setup_sandbox
export STUB_ALEMBIC_RC=1
OUT=$(run_script)
unset STUB_ALEMBIC_RC
if [[ "$(cat "$APP/backend/.venv/lib/deps-marker" 2>/dev/null)" == "OLD DEPS" ]]; then
  ok "迁移失败：依赖回到上一版（§3.39 原行为没被弄坏）"
else
  no "迁移失败：依赖没有回退：$(cat "$APP/backend/.venv/lib/deps-marker" 2>/dev/null || echo '<读不到>')"
fi
if grep -q "NEW backend code" "$APP/backend/app/main.py" 2>/dev/null; then
  no "迁移失败：现网代码变成了新版（不该动）"
else
  ok "迁移失败：现网代码仍是旧版（与旧依赖同版本）"
fi

echo
echo "== 27. §3.40 残留目录清理：删老的、留新的 =="
# --------------------------------------------------------------------------
# 原来的判据是 `find -mtime -7` —— 那是「7 天以内修改过」，find **会打印它**，
# 于是 `[ -n "$(find ...)" ]` 对**新**目录成立 → **删掉的是刚失败的那一次**，
# 留下的是老的 —— 与那段注释说的**正好相反**。
#
# 判据必须是「输出 = 够老了 = 该删」。
# --------------------------------------------------------------------------
setup_sandbox
mkdir -p "$APP/.incoming.OLD" "$APP/.artifact.OLD" "$APP/.incoming.NEW"
# touch -t 是 POSIX 的（macOS / Linux 都支持），-d 不是。
touch -t 202001010000 "$APP/.incoming.OLD" "$APP/.artifact.OLD"
run_script >/dev/null
assert_ran "全量发布" || exit 1
if [[ ! -d "$APP/.incoming.OLD" ]]; then
  ok "超过 7 天的残留被清掉了"
else
  no "超过 7 天的残留还在 —— 正是这段代码要解决的那个「只增不减」"
fi
if [[ ! -d "$APP/.artifact.OLD" ]]; then
  ok "超过 7 天的产物残留也被清掉了"
else
  no "超过 7 天的产物残留还在"
fi
if [[ -d "$APP/.incoming.NEW" ]]; then
  ok "刚失败的那一次被留着了（现场还在有效期内）"
else
  no "刚失败的那一次被删掉了 —— 出事时「翻现场」的东西没了"
fi

echo
echo "== 28. §3.41 前端没在服务时，发布不许报成功 =="
# --------------------------------------------------------------------------
# nginx 读的是**磁盘上的 dist**，不经过后端进程 ——
# 后端健康与「前端在不在服务」互不相干，而原来只打后端。
#
# 两种翻车形态都要挡住：
#   (a) 前端整个取不到（nginx 没在服务 / root 指向别处）；
#   (b) 前端能取到，但**不是刚发出去的这一版**（root 仍指向旧目录）。
# --------------------------------------------------------------------------
setup_sandbox
OUT=$(STUB_FRONTEND_RC=22 run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "前端不可达时发布失败（${RC}）"
else
  no "前端不可达却返回成功"
fi
if grep -q "✅ 发布完成" <<<"$OUT"; then
  no "前端不可达却打印了「✅ 发布完成」"
else
  ok "前端不可达时没有打印「✅ 发布完成」"
fi
if grep -q "前端" <<<"$OUT"; then
  ok "明确报出了前端这一项的问题"
else
  no "没有说清楚是前端没在服务"
fi

# (b) nginx 继续服务上一版 → 版本对不上 → 必须失败。
setup_sandbox
run_script >/dev/null
assert_ran "首次发布" || exit 1
OLD_LINK="$(readlink "$APP/frontend/dist" 2>/dev/null || echo "")"
use_sha "ccc222ddd333"
export STUB_FRONTEND_FROM="$APP/frontend/$OLD_LINK"
OUT=$(run_script); RC=$?
unset STUB_FRONTEND_FROM
if [[ "$RC" != "0" ]]; then
  ok "线上跑的是上一版前端时，发布失败"
else
  no "线上跑的是上一版前端却返回成功"
fi
if grep -q "✅ 发布完成" <<<"$OUT"; then
  no "线上跑的是上一版前端却打印了「✅ 发布完成」"
else
  ok "线上跑的是上一版前端时没有打印「✅ 发布完成」"
fi

echo
echo "== 29. §3.43 0.0.0.0/0 与 ::/0 必须被拒 =="
# --------------------------------------------------------------------------
# 脚本头自己把「不能写 0.0.0.0/0」列为安全边界，而那个正则照单全收。
# 装上它之后，任何能直连源站的人都能伪造自己的 IP ——
# IP 维度的节流与审计全部失效，正是这个脚本存在的目的被反转。
# --------------------------------------------------------------------------
setup_sandbox
# nginx 桩：这个脚本的最后一步是 `nginx -t`，沙箱里没有 nginx。
cat > "$DEPDIR/bin/nginx" <<'STUB'
#!/bin/sh
echo "nginx $*" >> "$DEPDIR/calls"
exit 0
STUB
chmod +x "$DEPDIR/bin/nginx"
# 目标路径放到临时目录：**绝不在测试机上碰真的 /etc/nginx**。
printf '0.0.0.0/0\n' > "$DEPDIR/ips-bad-v4.txt"
OUT=$( cd "$DEPDIR" && PATH="$DEPDIR/bin:$PATH" \
        CWQ_REALIP_TARGET="$DEPDIR/realip.conf" \
        bash "$ESA_REALIP" "$DEPDIR/ips-bad-v4.txt" 2>&1 ); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "0.0.0.0/0 被拒绝（退出码 ${RC}）"
else
  no "0.0.0.0/0 被照单全收 —— 装上之后任何人都能伪造自己的 IP"
fi
if grep -q "拒绝" <<<"$OUT"; then
  ok "明确说了为什么拒绝"
else
  no "没有说清楚为什么拒绝"
fi
if [[ -e "$DEPDIR/realip.conf" ]]; then
  no "拒绝了却仍然写了目标文件 —— 必须是「不改任何东西」"
else
  ok "被拒时一个字节都没写"
fi
printf '::/0\n' > "$DEPDIR/ips-bad-v6.txt"
OUT=$( cd "$DEPDIR" && PATH="$DEPDIR/bin:$PATH" \
        CWQ_REALIP_TARGET="$DEPDIR/realip.conf" \
        bash "$ESA_REALIP" "$DEPDIR/ips-bad-v6.txt" 2>&1 ); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "::/0 被拒绝（退出码 ${RC}）"
else
  no "::/0 被照单全收"
fi
# 正常的网段必须还能用（闸门不能把正当用法一起挡掉）。
printf '1.2.3.0/24\n2409:8c54::/32\n' > "$DEPDIR/ips-ok.txt"
OUT=$( cd "$DEPDIR" && PATH="$DEPDIR/bin:$PATH" \
        CWQ_REALIP_TARGET="$DEPDIR/realip.conf" \
        bash "$ESA_REALIP" "$DEPDIR/ips-ok.txt" 2>&1 ); RC=$?
if [[ "$RC" == "0" ]]; then
  ok "正常的网段（/24、/32）被接受并装上了"
else
  no "正常的网段被挡住了（${RC}）：$(tail -3 <<<"$OUT")"
fi
if grep -q "set_real_ip_from 1.2.3.0/24" "$DEPDIR/realip.conf" 2>/dev/null; then
  ok "装出来的配置里确实有那条网段"
else
  no "装出来的配置里没有那条网段：$(cat "$DEPDIR/realip.conf" 2>/dev/null | head -5)"
fi

echo
echo "== 30. §3.44 TRUSTED_PROXIES 为空必须阻断 =="
# --------------------------------------------------------------------------
# 空值不是「没配」，是「**明确配成了空**」（.env.example 的默认值就是空的）。
# 后果：全站共用一个节流桶 → 任何匿名用户 30 次错误登录就把全公司锁 15 分钟；
# 每一条登录审计记录的来源 IP 都退化成 127.0.0.1。
# --------------------------------------------------------------------------
setup_sandbox
echo "TRUSTED_PROXIES=" >> "$APP/backend/.env"
OUT=$(run_script); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "TRUSTED_PROXIES 为空时发布被阻断（${RC}）"
else
  no "TRUSTED_PROXIES 为空却返回成功"
fi
if grep -q "TRUSTED_PROXIES 是空的" <<<"$OUT"; then
  ok "明确报出「TRUSTED_PROXIES 是空的」"
else
  no "没有报出空值这一档"
fi
if grep -q "✅ 发布完成" <<<"$OUT"; then
  no "不合格的部署却打印了「✅ 发布完成」"
else
  ok "不合格的部署没有打印「✅ 发布完成」"
fi
# 豁免：确实要接受这个风险时有一条明确的出路。
setup_sandbox
echo "TRUSTED_PROXIES=" >> "$APP/backend/.env"
export CWQ_DEPLOY_ALLOW_EMPTY_TRUSTED_PROXIES=1
OUT=$(run_script); RC=$?
unset CWQ_DEPLOY_ALLOW_EMPTY_TRUSTED_PROXIES
if [[ "$RC" == "0" ]]; then
  ok "CWQ_DEPLOY_ALLOW_EMPTY_TRUSTED_PROXIES=1 时放行"
else
  no "豁免变量没有放行（${RC}）"
fi
if grep -q "✅ 发布完成" <<<"$OUT"; then
  ok "豁免之后发布正常完成"
else
  no "豁免之后发布没有完成"
fi

echo
echo "== 31. §3.45 认不出本机出口 IP 时必须拒绝（fail closed） =="
# --------------------------------------------------------------------------
# 原来的比较是 `[[ -n "$this_ip" && "$authorized" != "$this_ip" ]]` ——
# this_ip 为空时整个比较被短路跳过，于是「标记文件说了算」，**任何机器都放行**。
#
# 沙箱里 `ip` 是桩、curl 是桩 ⇒ host_public_ip 拿不到东西 ⇒ this_ip 为空。
# --------------------------------------------------------------------------
setup_sandbox
printf '43.138.178.174\n' > "$DEPDIR/target-marker"
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_HOST_IP= CWQ_DEPLOY_TARGET_FILE="$DEPDIR/target-marker" \
        CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if [[ "$RC" != "0" ]]; then
  ok "认不出本机出口 IP 时发布被拒绝（退出码 ${RC}）"
else
  no "认不出本机身份却放行了 —— 这道闸 fail-open"
fi
if grep -q "认不出本机出口 IP" <<<"$OUT"; then
  ok "明确报出「认不出本机出口 IP」"
else
  no "没有说清楚为什么拒绝"
fi
if grep -q "目标机授权标记：" <<<"$OUT"; then
  no "认不出本机身份却仍然打印了「目标机授权标记 ✅」"
else
  ok "没有把「读不到身份」报成「标记文件说了算」"
fi
# 沙箱豁免仍然放行（否则本地演练没法做）。
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_HOST_IP= CWQ_DEPLOY_TARGET_FILE="$DEPDIR/target-marker" \
        CWQ_ALLOW_ANY_HOST=1 CWQ_DEPLOY_ALLOW_NONSTANDARD_APP_DIR=1 \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if [[ "$RC" == "0" ]]; then
  ok "CWQ_ALLOW_ANY_HOST=1 时沙箱演练照常通过"
else
  no "沙箱豁免被挡住了（${RC}）"
fi

echo
echo "== 32. §3.41(b) 应用根不是 /opt/cwq 时必须拒绝 =="
# --------------------------------------------------------------------------
# nginx 的 root 写死在 /opt/cwq/frontend/dist，而它**不在** render_unit 的
# 渲染范围内 —— 换根目录后 systemd unit 与 dist 软链都切了，nginx 没切，
# 线上跑的是上一版前端，而发布照样打印「✅ 发布完成」。
# --------------------------------------------------------------------------
setup_sandbox
OUT=$( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
        SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
        CWQ_ALLOW_ANY_HOST=1 \
        LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" --check-only 2>&1 ); RC=$?
if [[ "$RC" != "0" ]] && grep -q "不是 /opt/cwq" <<<"$OUT"; then
  ok "应用根不是 /opt/cwq 时发布被拒绝"
else
  no "应用根不是 /opt/cwq 却放行了（nginx 的 root 还写死在旧路径上）"
fi
if calls_has "alembic upgrade head"; then
  no "被拒绝的发布仍然改了库"
else
  ok "被拒绝的发布没有产生任何副作用"
fi

echo
echo "== 33. §3.42 生效的 vhost 与日志路径必须探测出来 =="
# --------------------------------------------------------------------------
# 脚本原来把两个路径**写死**了，而两个都可能不是真的：
#   CONF=/etc/nginx/conf.d/acct.xins.live.conf  （宝塔形态不是这个）
#   LOG=/var/log/nginx/acct.xins.live.log       （模板里写的是 /www/wwwlogs/…）
#
# 后果是这个脚本**永远拿不到它存在的唯一理由**（切 strict 之前要的那份证据）：
# status 找不到 ssl_verify_client → 退出 1；日志路径不存在 → 永远「日志还没有」。
# --------------------------------------------------------------------------
setup_sandbox
mkdir -p "$DEPDIR/nginx/conf.d" "$DEPDIR/nginx/wwwlogs"
cat > "$DEPDIR/nginx/conf.d/acct.xins.live.conf" <<CONF
server {
    listen 443 ssl;
    server_name acct.xins.live;
    ssl_verify_client     optional;
    access_log  $DEPDIR/nginx/wwwlogs/acct.xins.live.log cwq_mtls;
    root /opt/cwq/frontend/dist;
}
CONF
# nginx 桩：-T 打出「它真正加载的那一份配置」（每一份前面带 configuration file 标记）。
cat > "$DEPDIR/bin/nginx" <<STUB
#!/bin/sh
if [ "\$1" = "-T" ]; then
cat <<'CONF'
# configuration file $DEPDIR/nginx/conf.d/acct.xins.live.conf:
server {
    ssl_verify_client     optional;
    access_log  $DEPDIR/nginx/wwwlogs/acct.xins.live.log cwq_mtls;
}
CONF
exit 0
fi
if [ "\$1" = "-t" ]; then echo "nginx: configuration file test is successful"; exit 0; fi
exit 0
STUB
chmod +x "$DEPDIR/bin/nginx"
OUT=$( cd "$DEPDIR" && PATH="$DEPDIR/bin:$PATH" bash "$ESA_MTLS" status 2>&1 )
if grep -q "conf.d/acct.xins.live.conf" <<<"$OUT"; then
  ok "从 nginx -T 里认出了生效的 vhost 路径"
else
  no "没认出生效的 vhost 路径：$(head -4 <<<"$OUT")"
fi
if grep -q "wwwlogs/acct.xins.live.log" <<<"$OUT"; then
  ok "日志路径跟着生效配置的 access_log 走（不再写死）"
else
  no "日志路径不是从生效配置里取的：$(head -8 <<<"$OUT")"
fi
if grep -q "ssl_verify_client: optional" <<<"$OUT"; then
  ok "读出了 ssl_verify_client 的当前值"
else
  no "读不出 ssl_verify_client 的当前值"
fi
# strict 必须**真的改掉**那一条指令。
#
# 原来那条 sed 用的是 BRE 的 ``\+`` —— 那是 GNU 的扩展，BSD sed 上匹配不到，
# 于是配置**根本没改**却照样打印「✅ 已切到 on」。
# 这条断言就是钉住那个「看着成功、其实什么都没做」的状态。
: > "$DEPDIR/fake-ca.pem"     # ESA 根证书：切强制校验前它必须存在
OUT=$( cd "$DEPDIR" && PATH="$DEPDIR/bin:$PATH" \
        CWQ_MTLS_CONF="$DEPDIR/nginx/conf.d/acct.xins.live.conf" \
        CWQ_MTLS_CA="$DEPDIR/fake-ca.pem" \
        bash "$ESA_MTLS" strict 2>&1 )
if grep -qE "^[[:space:]]*ssl_verify_client[[:space:]]+on;" "$DEPDIR/nginx/conf.d/acct.xins.live.conf"; then
  ok "strict 真的把 ssl_verify_client 改成了 on"
else
  no "strict 报成功却没改到指令：$(grep -n ssl_verify_client "$DEPDIR/nginx/conf.d/acct.xins.live.conf")"
fi
# 找不到生效配置时，报的必须是**可操作**的提示。
OUT=$( cd "$DEPDIR" && PATH="/usr/bin:/bin" bash "$ESA_MTLS" status 2>&1 )
if grep -q "没找到" <<<"$OUT"; then
  ok "找不到生效的 vhost 时明确报「没找到」"
else
  no "找不到生效的 vhost 时没有说清楚"
fi
if grep -q "nginx -T" <<<"$OUT"; then
  ok "给了「怎么找」的命令（nginx -T | grep ssl_verify_client）"
else
  no "没有告诉运维怎么找生效的配置"
fi

echo
echo "通过 $PASS / 失败 $FAIL"
[[ "$FAIL" == "0" ]]
