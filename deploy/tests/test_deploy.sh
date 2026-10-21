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
  echo "print('pass')" > "$be/migrations/env.py"
  # 新迁移链的标记。断言「迁移跑的是产物里那条链」全靠它。
  echo "# NEW MIGRATION CHAIN" > "$be/migrations/versions/0027_plugin_imports.py"
  echo "[alembic]" > "$be/alembic.ini"
  # 第一行是这一版新增的依赖 —— 真实事故里就是 rfc8785 没被装上。
  # 它必须与现网旧锁的第一行**不同**，否则断言分不出装的是哪一份锁。
  echo "rfc8785==0.1.4" > "$be/requirements.lock"
  echo "SQLAlchemy==2.1.4" >> "$be/requirements.lock"
  echo "[pytest]" > "$be/pytest.ini"
  echo "<html>new frontend $sha</html>" > "$fe/dist/index.html"
  tar -czf "$out/cwq-backend-${sha}.tar.gz" -C "$be" .
  tar -czf "$out/cwq-frontend-${sha}.tar.gz" -C "$fe" dist
  {
    echo "sha=${sha}"
    echo "commit=${sha}fullsha"
    echo "ref=main"
    echo "built_at=2026-10-15T00:00:00Z"
    echo "backend=cwq-backend-${sha}.tar.gz"
    echo "frontend=cwq-frontend-${sha}.tar.gz"
  } > "$out/manifest.txt"
  ( cd "$out" && sha256sum ./cwq-backend-${sha}.tar.gz \
                        ./cwq-frontend-${sha}.tar.gz > SHA256SUMS )
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
  printf '#!/bin/sh\necho "curl $*" >> "$DEPDIR/calls"\nexit 0\n' > "$DEPDIR/bin/curl"

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
  cat > "$APP/backend/.venv/bin/pip" <<'STUB'
#!/bin/sh
lock=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then lock=$(head -1 "$a" 2>/dev/null); next=0; continue; fi
  if [ "$a" = "-r" ]; then next=1; fi
done
echo "pip $* [lock=${lock}]" >> "$DEPDIR/calls"
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
  # db_backup.py 走 venv 里的 python，要真的吐点东西当备份
  printf '#!/bin/sh\nif [ "$1" = "scripts/db_backup.py" ]; then echo "SELECT 1;"; exit ${STUB_BACKUP_RC:-0}; fi\necho "python $*" >> "$DEPDIR/calls"\nif [ "$1" = "scripts/plugin_status.py" ]; then exit ${STUB_PLUGIN_STATUS_RC:-0}; fi\nexit 0\n' \
    > "$APP/backend/.venv/bin/python"
  chmod +x "$DEPDIR/bin"/* "$APP/backend/.venv/bin"/*
  # unit 与备份的落地位置由 deploy.sh 通过 SYSTEMD_UNIT_DIR / BACKUP_DIR 传入。
  # 这里必须先建出来：真实机器上 /etc/systemd/system 与 /opt/cwq/backups
  # 本来就存在，脚本不负责建；沙箱里没人建，cp 就会落不下去。
  mkdir -p "$DEPDIR/units" "$DEPDIR/backups"
  : > "$DEPDIR/calls"
}

run_script(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
run_script_no_artifacts(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/empty-artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
# 显式清掉 DATABASE_URL：这条只用来验证「目标库不确定时脚本会停手」。
# 不能依赖 CI 机器上恰好没有这个变量 —— 那种断言一旦环境一变就变成恒绿。
run_script_no_db_url(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    env -u DATABASE_URL LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
# 显式给 DATABASE_URL：用来验证「.env 不存在」这条路径 —— 那条路径是
# 合法的（deploy.sh 第 3 步只在 .env 与 DATABASE_URL **都**没有时才拦），
# 而它正是 TRUSTED_PROXIES 告警方向反了的触发场景。
run_script_with_db_url(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" ARTIFACT_DIR="$DEPDIR/artifacts" \
    SYSTEMD_UNIT_DIR="$DEPDIR/units" BACKUP_DIR="$DEPDIR/backups" \
    DATABASE_URL="postgresql+psycopg://cwq:pw@127.0.0.1:15432/cwq" \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
calls(){ cat "$DEPDIR/calls" 2>/dev/null; }

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
echo "通过 $PASS / 失败 $FAIL"
[[ "$FAIL" == "0" ]]
