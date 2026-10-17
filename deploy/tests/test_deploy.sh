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
  echo "print('pass')" > "$be/migrations/env.py"
  echo "[alembic]" > "$be/alembic.ini"
  echo "SQLAlchemy==2.1.4" > "$be/requirements.lock"
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
  mkdir -p "$APP/backend/.venv/bin" "$APP/frontend/dist" "$APP/.git"
  echo "OLD backend content" > "$APP/backend/app.py"
  echo "<html>old frontend</html>" > "$APP/frontend/dist/index.html"
  echo "SQLAlchemy==2.1.4" > "$APP/backend/requirements.lock"
  make_artifacts "$DEPDIR/artifacts"

  for c in systemctl curl; do
    printf '#!/bin/sh\necho "%s $*" >> "$DEPDIR/calls"\nexit 0\n' "$c" > "$DEPDIR/bin/$c"
  done
  printf '#!/bin/sh\necho "pip $*" >> "$DEPDIR/calls"\nexit 0\n' > "$APP/backend/.venv/bin/pip"
  printf '#!/bin/sh\necho "alembic $*" >> "$DEPDIR/calls"\nexit ${STUB_ALEMBIC_RC:-0}\n' \
    > "$APP/backend/.venv/bin/alembic"
  # db_backup.py 走 venv 里的 python，要真的吐点东西当备份
  printf '#!/bin/sh\nif [ "$1" = "scripts/db_backup.py" ]; then echo "SELECT 1;"; exit ${STUB_BACKUP_RC:-0}; fi\necho "python $*" >> "$DEPDIR/calls"\nexit 0\n' \
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
for pair in "pytest:跑测试" "pnpm:构建前端" "node:跑前端"; do
  tool="${pair%%:*}"; what="${pair##*:}"
  if calls | grep -q "$tool"; then no "生产机仍然${what}（${tool}）"; else ok "生产机不再${what}"; fi
done
if calls | grep -q "pip install -q -r requirements.lock"; then
  ok "装的是运行时锁 requirements.lock（不是 dev 锁）"
else
  no "没有装 requirements.lock"
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
echo "通过 $PASS / 失败 $FAIL"
[[ "$FAIL" == "0" ]]
