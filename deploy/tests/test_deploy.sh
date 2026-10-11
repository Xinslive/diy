#!/usr/bin/env bash
# 部署脚本沙箱演练（审计 P3-03）。
#
# deploy.sh 没有测试框架，但「它到底能不能跑通」恰恰是这次要钉的东西 ——
# 审计说这份脚本当前状态下无法成功完成一次发布，修完就该证明它能。
#
# 做法：用假的 git / pip / pytest / pnpm / systemctl / curl 顶替真命令，
# 在临时目录里**真的把 deploy.sh 跑一遍**，然后断言它做了什么。
#
# 最重要的一条是 assert_ran：脚本必须真的跑起来了，后面所有断言才有意义。
# 这个守卫是被「恒绿测试」坑出来的 —— 旧版 deploy.sh 把 APP 写死成
# /opt/cwq（不支持覆盖），在沙箱里第一行 ``cd "$APP"`` 就失败退出，
# 于是「没重启服务」「没跑测试」这些断言全都成立，一片绿。
# 但那不是脚本修好了，是脚本**根本没执行**。
#
# 运行：bash deploy/tests/test_deploy.sh
set -uo pipefail

SCRIPT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy.sh
PASS=0; FAIL=0
ok(){ echo "  OK   $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL $1"; FAIL=$((FAIL+1)); }

DEP_N=0
setup_sandbox(){
  DEP_N=$((DEP_N+1))
  export DEPDIR="/tmp/cwq-dep-$DEP_N-$$"
  mkdir -p "$DEPDIR/bin"
  APP="$DEPDIR/app"
  mkdir -p "$APP/backend/.venv/bin" "$APP/frontend/dist" "$APP/.git"
  echo "backend content" > "$APP/backend/app.py"
  echo "old frontend" > "$APP/frontend/dist/index.html"
  touch "$APP/frontend/pnpm-lock.yaml"

  # 假的 git：symbolic-ref 回答默认分支，其余调用只记账
  cat > "$DEPDIR/bin/git" <<'EOF'
#!/bin/sh
echo "git $*" >> "$DEPDIR/calls"
if [ "$1" = "symbolic-ref" ]; then echo "origin/master"; exit 0; fi
if [ "$1" = "rev-parse" ]; then echo "master"; exit 0; fi
if [ "$1" = "show-ref" ]; then exit 0; fi
exit 0
EOF
  cat > "$DEPDIR/bin/pnpm" <<'EOF'
#!/bin/sh
echo "pnpm $*" >> "$DEPDIR/calls"
if [ "$1" = "run" ]; then D=$(pwd)/dist; mkdir -p "$D"; echo "new frontend" > "$D/index.html"; fi
exit 0
EOF
  for c in systemctl curl pip; do
    printf '#!/bin/sh\necho "%s $*" >> "$DEPDIR/calls"\nexit 0\n' "$c" > "$DEPDIR/bin/$c"
  done
  # 后端 venv 里的 python 才是真正被调用的那个
  printf '#!/bin/sh\necho "pytest $*" >> "$DEPDIR/calls"\nexit ${STUB_PYTEST_RC:-0}\n' \
    > "$APP/backend/.venv/bin/python"
  printf '#!/bin/sh\necho "pip $*" >> "$DEPDIR/calls"\nexit 0\n' > "$APP/backend/.venv/bin/pip"
  chmod +x "$DEPDIR/bin"/* "$APP/backend/.venv/bin"/*
  : > "$DEPDIR/calls"
}

run_script(){
  ( cd "$APP" && PATH="$DEPDIR/bin:$PATH" APP_DIR="$APP" \
    LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 bash "$SCRIPT" "$@" 2>&1 )
}
calls(){ cat "$DEPDIR/calls" 2>/dev/null; }

# 脚本必须真的跑起来了（走到了 git fetch），否则下面全是空断言
assert_ran(){
  if calls | grep -q "git fetch"; then return 0; fi
  no "$1 —— 脚本在沙箱里没跑起来，下面的断言全部无意义"
  return 1
}

echo "== 1. --check-only 必须真的只检查 =="
setup_sandbox
OUT=$(run_script --check-only); RC=$?
# 注意：--check-only 本来就不该 git fetch（那正是「不发布」的一部分），
# 所以这里的活着性判据是「有没有输出检查结果」，不是「有没有 fetch」。
if grep -q "只做检查" <<<"$OUT"; then
  ok "--check-only 确实执行了（不是空跑，也不是跑崩了）"
else
  no "--check-only 分支没被执行 —— 后续断言无意义"
fi
# 「只检查」必须**正常退出**。旧脚本没有 exit 0，会一路往下走；
# 它最终非零退出不是因为检查失败，而是因为后面的某一步崩了 ——
# 所以光断言「没重启」会被这个崩溃掩盖过去（空断言）。
if [[ "$RC" == "0" ]]; then
  ok "--check-only 以 0 正常退出（是主动结束的，不是崩的）"
else
  no "--check-only 退出码 $RC —— 它并没有在检查完就停下来，而是继续往下走了"
fi
# 同理，直接查副作用：只检查不该动文件系统
if [[ -d "$APP/.releases" ]]; then
  no "--check-only 仍然创建了备份目录 —— 它继续往下走了"
else
  ok "--check-only 没有产生任何发布副作用"
fi
for pair in "pytest:跑测试" "systemctl:重启服务" "pnpm:构建前端" "pip:装依赖"; do
  tool="${pair%%:*}"; what="${pair##*:}"
  if calls | grep -q "$tool"; then no "--check-only 仍然$what"; else ok "--check-only 没$what"; fi
done

echo "== 2. 分支名不写死 =="
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
if calls | grep -q "git checkout main"; then
  no "仍然写死 checkout main"
else
  ok "没有写死 main"
fi

echo "== 3. .releases 必须 mkdir，且每次都备份 =="
setup_sandbox
run_script >/dev/null
assert_ran "首次发布" || exit 1
if [[ -d "$APP/.releases" ]]; then ok "备份目录已创建"; else no "备份目录没创建"; fi
# 连续两次发布（不 sleep，逼出同一秒内两次发布）
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

echo "== 4. 包管理器与锁文件一致 =="
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
if calls | grep -q "npm ci"; then
  no "仍然用 npm ci（仓库没有 package-lock.json，必然失败）"
else
  ok "没用 npm ci"
fi
if calls | grep -q "pnpm install --frozen-lockfile"; then
  ok "用了 pnpm install --frozen-lockfile"
else
  no "没用 pnpm --frozen-lockfile"
fi

echo "== 5. dist 换名而不是自删自移 =="
setup_sandbox
run_script >/dev/null
assert_ran "全量发布" || exit 1
if [[ -f "$APP/frontend/dist/index.html" ]]; then
  ok "dist 换名后仍在"
else
  no "dist 换名后丢了"
fi
if grep -q "new frontend" "$APP/frontend/dist/index.html" 2>/dev/null; then
  ok "dist 里是新构建的产物（不是旧的）"
else
  no "dist 里不是新产物，换名逻辑可能反了"
fi

echo "== 6. 测试不过就不重启 =="
setup_sandbox
export STUB_PYTEST_RC=1
OUT=$(run_script)
unset STUB_PYTEST_RC
assert_ran "测试失败的发布流程" || exit 1
if calls | grep -q "systemctl restart"; then
  no "测试失败却重启了服务"
else
  ok "测试失败没有重启"
fi
if grep -q "测试未通过" <<<"$OUT"; then ok "明确报出测试未通过"; else no "没有明确报错"; fi

echo "== 7. 备份保留 KEEP 份 =="
setup_sandbox
for i in 1 2 3 4 5; do mkdir -p "$APP/.releases/2020010$i-000000"; done
run_script >/dev/null
N=$(find "$APP/.releases" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
if [[ "${N:-99}" -le 3 ]]; then ok "旧备份已清理（剩 $N 份）"; else no "备份没清理（剩 ${N:-?} 份）"; fi

echo
echo "通过 $PASS / 失败 $FAIL"
[[ "$FAIL" == "0" ]]
