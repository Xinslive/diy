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
  mkdir -p "$out"
  local be="$DEPDIR/stage-backend" fe="$DEPDIR/stage-frontend"
  mkdir -p "$be/app" "$be/scripts" "$be/migrations/versions" "$fe/dist"
  echo "NEW backend code" > "$be/app/main.py"
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
  echo "<html>new frontend</html>" > "$fe/dist/index.html"
  tar -czf "$out/cwq-backend-${FAKE_SHA}.tar.gz" -C "$be" .
  tar -czf "$out/cwq-frontend-${FAKE_SHA}.tar.gz" -C "$fe" dist
  {
    echo "sha=${FAKE_SHA}"
    echo "commit=${FAKE_SHA}fullsha"
    echo "ref=main"
    echo "built_at=2026-10-15T00:00:00Z"
    echo "backend=cwq-backend-${FAKE_SHA}.tar.gz"
    echo "frontend=cwq-frontend-${FAKE_SHA}.tar.gz"
  } > "$out/manifest.txt"
  ( cd "$out" && sha256sum ./cwq-backend-${FAKE_SHA}.tar.gz \
                        ./cwq-frontend-${FAKE_SHA}.tar.gz > SHA256SUMS )
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

  for c in systemctl curl; do
    printf '#!/bin/sh\necho "%s $*" >> "$DEPDIR/calls"\nexit 0\n' "$c" > "$DEPDIR/bin/$c"
  done
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
calls(){ cat "$DEPDIR/calls" 2>/dev/null; }

# 脚本必须真的跑起来了（取到了产物），否则下面全是空断言
assert_ran(){
  if calls | grep -q "alembic upgrade head"; then return 0; fi
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
  if calls | grep -q "$tool"; then no "--check-only 仍然调了 $tool"; else ok "--check-only 没调 $tool"; fi
done

echo "== 2. 生产机不再跑测试、不再构建前端 =="
setup_sandbox
OUT=$(run_script)
assert_ran "全量发布" || exit 1
# 这两条是本次改造的核心目的：把生产机从构建机上摘下来。
if calls | grep -q "pytest"; then no "生产机仍然跑测试（pytest）"; else ok "生产机不再跑测试"; fi
if calls | grep -q "pnpm"; then no "生产机仍然构建前端（pnpm）"; else ok "生产机不再构建前端"; fi
if calls | grep -q "node"; then no "生产机仍然跑前端（node）"; else ok "生产机不再跑前端"; fi
# 断言「装的锁是哪一份」，不是「有没有跑 pip」。产物里的锁第一行是
# rfc8785（这一版新增的依赖），现网旧锁第一行是 SQLAlchemy —— 两者不同，
# 断言才分得出来。
PIPLINE=$(calls | grep -F 'pip install' | head -1)
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
if calls | grep -q "systemctl restart"; then
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
if calls | grep -q "systemctl restart"; then
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
if calls | grep -q "alembic upgrade head"; then
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
if calls | grep -q "systemctl restart"; then
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
if calls | grep -q "alembic upgrade head"; then
  no "备份失败却先迁移了生产库 —— 没有退路就不该动 schema"
else
  ok "备份失败就不迁移（schema 保持在迁移前的状态）"
fi
if calls | grep -q "systemctl restart"; then
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
if calls | grep -q "systemctl enable --now cwq-backup.timer"; then
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
if calls | grep -q "cwq-backup.timer"; then
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
if calls | grep -q "alembic upgrade head"; then
  ok "关停定时备份不影响发版时的库备份与迁移"
else
  no "关停定时备份把发版备份也一起弄没了"
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
if calls | grep -q "systemctl enable --now cwq-scheduler-watchdog.timer"; then
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
if calls | grep -q "cwq-scheduler-watchdog.timer"; then
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
# $APP、第 4 步又 cd 到 $APP/backend，于是它找的是 ``$APP/backend/systemd``，
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
if [[ "$RC" == "0" ]] && calls | grep -q "python scripts/plugin_status.py --wait 30"; then
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
if calls | grep -q "python scripts/plugin_status.py --wait 30" && [[ "$RC" != "0" ]]; then
  ok "插件屏障失败时发布返回非零"
else
  no "插件屏障失败却仍然报告发布成功"
fi
if grep -q "插件 worker barrier 未通过" <<<"$OUT" && grep -q "新版本已启动" <<<"$OUT"; then
  ok "屏障失败明确说明运行现场及处置方式"
else
  no "屏障失败没有准确报告已启动的现场"
fi
if calls | grep -q "systemctl enable --now cwq-backup.timer"; then
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

PIPLINE=$(calls | grep -F 'pip install' | head -1)
if [[ "$PIPLINE" == *"[lock=rfc8785==0.1.4]"* ]]; then
  ok "装依赖用的是产物里的锁文件，不是现网那份旧的"
else
  no "装依赖的依据不对：${PIPLINE:-（根本没有 pip 调用）}"
fi

ALEM=$(calls | grep -F 'alembic upgrade head' | head -1)
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
if calls | grep -qF 'alembic upgrade head'; then
  no "依赖没装好就迁移了库 —— 迁移脚本要靠新代码跑，依赖不齐就是跑不通"
else
  ok "依赖没装好就不迁移"
fi
if calls | grep -q "systemctl restart"; then
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
if calls | grep -qF 'alembic upgrade head'; then
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
echo "通过 $PASS / 失败 $FAIL"
[[ "$FAIL" == "0" ]]
