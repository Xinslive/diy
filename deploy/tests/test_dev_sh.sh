#!/usr/bin/env bash
# dev.sh 沙箱演练（2026-10-17 那次「后端又没有响应」的复现与修复验证）。
#
# **这个测试要复现的不是「脚本跑不通」，而是那个具体的进程形态。** 所以它不
# 造假 lsof、不造假 curl：真的起一个**真的 listen 着 8000、但永远不
# accept** 的进程，再真的拿 curl 去连。内核会完成三次握手（客户端看到
# ESTABLISHED），然后请求就永远挂着 —— 与那天现场逐条对得上。
#
# 那个进程还**故意吞掉 SIGTERM**，学 uvicorn 的 reloader：它的 signal handler
# 只 should_exit.set()，而那个 Event 只在 pause()/should_restart() 里被看，
# 父进程此刻阻塞在 os_waitpid，于是事件永远轮不到被看一眼。实测连发两次
# SIGTERM 纹丝不动，kill -9 之后端口才释放。
#
# 钉住七件事：
#   1. 楔住时 ./dev.sh **必须返回非零**，且**不许**打印「好了」；
#   2. 它必须把楔住的那个进程组真的清掉 —— 否则下一次 ./dev.sh 照样起不来；
#   3. 预检不过时**不许**把 uvicorn 拉起来（闸门必须在进程起来之前）；
#   4. stop 必须 TERM → 等待 → KILL 升级，而不是 kill 一次就放弃。
#
# 第 5–7 条是 2026-10-18 接入受信任插件后加的。它们防的是**反向回归**：
# 为了让本地能调试插件，把默认的热重载路径一起改掉 —— 那样默认体验被牺牲，
# 而且没人会发现（因为插件默认关着，平时根本看不出来）。所以插件的判据是
# 「关着时一个字节都不能变」，不是「开着时能跑」。
#   5. 插件关着时仍然走 uvicorn --reload，且不碰 supervisor、不问屏障；
#   6. 插件开着时改走 scripts/start_server.py，显式带 --workers，且不含 --reload；
#   7. 插件路径下屏障真的被调用过，且结果被报出来。
#
# 运行：bash deploy/tests/test_dev_sh.sh
set -uo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
PASS=0; FAIL=0
ok(){ echo "  OK   $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL $1"; FAIL=$((FAIL+1)); }
chk(){ if [ "$1" = "0" ]; then ok "$2"; else no "$2"; fi; }

SB=""; declare -a OWNED_PIDS=()

# 探测预算压到最小：这些用例等的都是「失败」，默认值是给冷启动的宽裕上限。
export CWQ_PROBE_TIMEOUT=1 CWQ_HEALTH_TIMEOUT=5 CWQ_READY_TIMEOUT=3 \
       CWQ_VITE_TIMEOUT=3 CWQ_STOP_GRACE=6

kill_owned() {
  local p
  for p in "${OWNED_PIDS[@]:-}"; do
    [ -n "$p" ] && kill -KILL "$p" 2>/dev/null
  done
  OWNED_PIDS=()
}

cleanup() {
  kill_owned
  [ -n "$SB" ] && rm -rf "$SB"
}
trap cleanup EXIT

# --------------------------------------------------------------------------
# 造沙箱：dev.sh 靠 `cd "$(dirname "$0")"` 定 ROOT，所以复制一份进去它就
# 只会看见沙箱里的东西，不会碰到真的仓库。
# --------------------------------------------------------------------------
make_sandbox() {
  # 上一组用例起的东西**必须在这里清掉**，不能等脚本退出。
  #
  # 这些用例全都靠 8000/5173 的真实应答来判形，而 OWNED_PIDS 一旦在造新
  # 沙箱时被清零，上一组那些还占着端口的进程就没人管了，会一直活到脚本
  # 结束 —— 于是后面这组测的根本不是它以为的东西，而且会**假绿**：
  # 「后端起不来」被上一组的残留服务顶成「后端起来了」。
  kill_owned
  [ -n "$SB" ] && rm -rf "$SB"
  # 端口没空出来就不许开下一组：抢不到 bind 的 start_serve 会静默死掉。
  for _ in $(seq 1 50); do
    lsof -nP -tiTCP:8000 -tiTCP:5173 -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.1
  done

  SB=$(mktemp -d "${TMPDIR:-/tmp}/cwq-devsh.XXXXXX")
  OWNED_PIDS=()
  # .run 必须先建：用例要在跑 dev.sh **之前**就往里写 pid 文件（楔住的
  # 进程是本用例起的，不是 dev.sh 起的），而 dev.sh 自己 mkdir 得太晚。
  mkdir -p "$SB/backend/.venv/bin" "$SB/frontend" "$SB/fakebin" "$SB/tools" "$SB/.run"
  cp "$REPO/dev.sh" "$SB/dev.sh"
  chmod +x "$SB/dev.sh"

  # 假 python 按 argv 区分三件事：
  #   * 探插件开关（-c ... plugin_enabled ...）→ 答 FAKE_PLUGIN_ENABLED，默认 0；
  #   * 跑 preflight → 按替身变量答；
  #   * 其余一切调用（含被 start_server.py 拉起的那次）→ 记下 argv 后立刻退出。
  #
  # **记 argv 是「这次选了哪条启动命令」的唯一可观察证据。** 沙箱里没有真的
  # uvicorn，插件路径那个假进程一退出后端就死，所以后面两条用例断言的是
  # 命令与文案，**不是 rc** —— 断言 rc 就是在断言「起不来」。
  export PY_CALLS_LOG="$SB/spawn-calls.log"
  : > "$PY_CALLS_LOG"
  cat > "$SB/backend/.venv/bin/python" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${PY_CALLS_LOG:-/dev/null}"
case "$*" in
  *plugin_enabled*) printf '%s' "${FAKE_PLUGIN_ENABLED:-0}"; exit 0 ;;
esac
echo "${FAKE_PREFLIGHT_MSG:-  ✓ schema 就绪（数据库 revision 0025_backup_run）}"
exit "${FAKE_PREFLIGHT_RC:-0}"
EOF
  chmod +x "$SB/backend/.venv/bin/python"

  # 假 uvicorn：默认路径直接拉起的是它，**根本不经过 python**，所以只给
  # 假 python 记 argv 的话，「默认是否还在热重载」这条就是永远测不到的。
  cat > "$SB/backend/.venv/bin/uvicorn" <<'EOF'
#!/usr/bin/env bash
printf 'uvicorn %s\n' "$*" >> "${PY_CALLS_LOG:-/dev/null}"
exit 0
EOF
  chmod +x "$SB/backend/.venv/bin/uvicorn"

  # 假的 docker / npm。docker 只被问「容器在不在」，npm 被 spawn 之后要
  # 一直活着（否则前端「进程自己退了」，会让用例断言的东西跑偏）。
  cat > "$SB/fakebin/docker" <<'EOF'
#!/usr/bin/env bash
[ "$1" = "ps" ] && echo "cwq-pg" || exit 0
EOF
  cat > "$SB/fakebin/npm" <<'EOF'
#!/usr/bin/env bash
exec sleep 3600
EOF
  chmod +x "$SB/fakebin/docker" "$SB/fakebin/npm"
  export PATH="$SB/fakebin:$PATH"

  # 楔住的进程：真的 bind + listen，**永远不 accept**，并且吞掉 SIGTERM。
  cat > "$SB/tools/wedge.py" <<'EOF'
import os, signal, socket, sys, time
port = int(sys.argv[1])
os.setsid()                      # 自己就是进程组长（pgid == pid）
signal.signal(signal.SIGTERM, signal.SIG_IGN)   # 学 uvicorn reloader：吞 TERM
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port)); s.listen(128)
# 握手由内核完成并进 backlog；我们一个 accept 都不调 —— 就是那天那个形态。
while True:
    time.sleep(3600)
EOF

  # 正常应答的 HTTP 服务，给「一切正常」那条用例用。
  cat > "$SB/tools/serve.py" <<'EOF'
import http.server, sys
port = int(sys.argv[1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"200"; self.send_response(200)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
EOF
}

start_wedge() {  # start_wedge <端口> <pid文件>
  python3 "$SB/tools/wedge.py" "$1" & local p=$!
  OWNED_PIDS+=("$p")
  echo "$p" > "$2"
  # 等它真的 listen 上，否则探测会看到「端口还没开」而误判成另一种形态
  for _ in $(seq 1 40); do
    lsof -nP -tiTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  return 1
}

start_serve() {  # start_serve <端口> <pid文件>
  python3 "$SB/tools/serve.py" "$1" & local p=$!
  OWNED_PIDS+=("$p"); echo "$p" > "$2"
  for _ in $(seq 1 40); do
    curl -s -m 1 -o /dev/null "http://127.0.0.1:$1/" && return 0
    sleep 0.1
  done
  return 1
}

alive_pid() { kill -0 "$1" 2>/dev/null; }

# ==========================================================================
echo "== 1. 端口在监听但没人应答（2026-10-17 的现场）=="
# ==========================================================================
make_sandbox
if start_wedge 8000 "$SB/.run/backend.pid"; then
  # pid 要**在跑 dev.sh 之前**取：dev.sh 清理时会 ``rm -f`` 掉 pid 文件，
  # 事后再 cat 只能拿到空。而 ``kill -0 ""`` 会打到调用者自己的进程组、
  # ``kill -0 0`` 更是永远成功 —— 用它判活会得到「进程还活着」的假结论，
  # 于是这条断言在**任何**实现下都不可能变红（非空洞，但也不测任何东西）。
  WEDGE_PID=$(cat "$SB/.run/backend.pid")
  out=$("$SB/dev.sh" 2>&1); rc=$?
  echo "$out" | sed 's/^/    | /'
  [ "$rc" -ne 0 ]; chk $? "起不来时返回非零（实际 rc=${rc}）"
  ! printf '%s' "$out" | grep -q '好了：'
  chk $? "没有打印「好了」—— 假活被当成启动成功是最该防的形态"
  printf '%s' "$out" | grep -q '没人应答\|没人 accept\|有人应答'
  chk $? "说清楚了是「监听但没人应答」而不是笼统的失败"
  sleep 0.5; ! alive_pid "$WEDGE_PID"
  chk $? "楔住的进程被清掉了（否则下一次 ./dev.sh 照样起不来）"
  lsof -nP -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1
  [ $? -ne 0 ]; chk $? "端口 8000 真的空了（残留会变成下一次启动的 Errno 48）"
else
  no "楔住进程没起来 —— 本用例全部结论无效"
fi

# ==========================================================================
echo "== 2. 预检不过时，不许把 uvicorn 拉起来 =="
# ==========================================================================
make_sandbox
out=$(FAKE_PREFLIGHT_RC=3 \
     FAKE_PREFLIGHT_MSG="✗ 数据库停在 0003，代码要求 0025 —— 库比代码旧" \
     "$SB/dev.sh" 2>&1); rc=$?
echo "$out" | sed 's/^/    | /'
[ "$rc" -ne 0 ]; chk $? "预检不过时返回非零（实际 rc=${rc}）"
! printf '%s' "$out" | grep -q '好了：'
chk $? "没有打印「好了」"
printf '%s' "$out" | grep -q '数据库停在 0003'
chk $? "预检的原始报错透出来了（不用去翻日志）"
# 闸门前移的可观察证据：uvicorn 一旦被拉起，它的日志文件必然被创建。
# 旧版 dev.sh 没有任何预检，所以这条在旧版上必然是红的。
[ ! -f "$SB/.run/api.log" ]; chk $? "uvicorn 没被拉起（.run/api.log 不存在）"

# ==========================================================================
echo "== 3. stop 必须 TERM → 等待 → KILL 升级 =="
# ==========================================================================
make_sandbox
if start_wedge 8000 "$SB/.run/backend.pid"; then
  p=$(cat "$SB/.run/backend.pid")   # 同上：dev.sh 会删掉 pid 文件
  out=$("$SB/dev.sh" stop 2>&1); rc=$?
  echo "$out" | sed 's/^/    | /'
  [ "$rc" -eq 0 ]; chk $? "stop 返回 0"
  ! alive_pid "$p"; chk $? "吞 SIGTERM 的进程被 KILL 掉了"
  lsof -nP -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1
  [ $? -ne 0 ]; chk $? "端口 8000 真的空了"
  printf '%s' "$out" | grep -q 'SIGTERM 被吞'
  chk $? "说清楚是 SIGTERM 被吞了（否则没人知道为什么 stop 这么慢）"
else
  no "楔住进程没起来 —— 本用例全部结论无效"
fi

# ==========================================================================
echo "== 4. 一切正常时照常报「好了」=="
# ==========================================================================
make_sandbox
if start_serve 8000 "$SB/.run/backend.pid" && start_serve 5173 "$SB/.run/frontend.pid"; then
  out=$("$SB/dev.sh" 2>&1); rc=$?
  echo "$out" | sed 's/^/    | /'
  [ "$rc" -eq 0 ]; chk $? "正常时返回 0"
  printf '%s' "$out" | grep -q '好了：http://127.0.0.1:5173'
  chk $? "正常时打印访问地址"
  # 这条防「修得太狠」：探测逻辑被改成永远失败，那就只是把上一条修绿了。
  printf '%s' "$out" | grep -q '就绪'
  chk $? "就绪探针真的被问了（不是被跳过）"
else
  no "应答服务没起来 —— 本用例全部结论无效"
fi

echo
echo "== 5. 默认路径仍然是 uvicorn --reload（插件是 opt-in，不能反客为主）=="
# ==========================================================================
# 这里**故意不**预起应答服务：预起了 dev.sh 会认为 backend 已在跑、直接跳过
# 启动分支，那样就什么命令都没被发起，等于什么都没测。
make_sandbox
out=$("$SB/dev.sh" 2>&1); rc=$?
echo "$out" | sed 's/^/    | /'
calls=$(cat "$PY_CALLS_LOG" 2>/dev/null)
[ "$rc" -ne 0 ]; chk $? "沙箱里没有真的 uvicorn，起不来是对的（实际 rc=${rc}）"
printf '%s' "$calls" | grep -q -- '--reload'
chk $? "默认仍走热重载（改后端代码立刻生效这件事不能被插件改掉）"
printf '%s' "$calls" | grep -q 'app.main:app'
chk $? "默认仍直接拉起 uvicorn app.main:app"
! printf '%s' "$calls" | grep -q 'start_server.py'
chk $? "插件关着时不碰 supervisor 入口"
! printf '%s' "$calls" | grep -q 'plugin_status.py'
chk $? "插件关着时不问屏障（没有插件就没有代次要等）"

# ==========================================================================
echo "== 6. 本地开了插件：改走 supervisor 入口，并交出热重载 =="
# ==========================================================================
make_sandbox
out=$(FAKE_PLUGIN_ENABLED=1 "$SB/dev.sh" 2>&1); rc=$?
echo "$out" | sed 's/^/    | /'
calls=$(cat "$PY_CALLS_LOG" 2>/dev/null)
printf '%s' "$calls" | grep -q 'scripts/start_server.py'
chk $? "探测到插件开启后改走 supervisor（否则 worker 没有 generation，一律 503）"
printf '%s' "$calls" | grep -q -- '--workers 2'
chk $? "显式传了 worker 数（CWQ_PLUGIN_EXPECTED_WORKERS 要等于实际 worker 数）"
! printf '%s' "$calls" | grep -q -- '--reload'
chk $? "插件路径不含 --reload（重载会换 worker，cohort 永远凑不齐）"
printf '%s' "$out" | grep -q '放弃热重载'
chk $? "明说放弃了热重载（否则开发者改完代码还在等它生效）"
! printf '%s' "$out" | grep -q '好了：'
chk $? "没真起来就不打印「好了」"

# ==========================================================================
echo "== 7. 插件路径下屏障真的被问了（不是写在那儿没人调）=="
# ==========================================================================
# 这一条要 RC=0 才会走到屏障，所以这里预起应答服务，让就绪探测通过。
# 代价是启动分支被跳过（见上一条的说明）—— 两条各测一半，合起来才完整。
make_sandbox
if start_serve 8000 "$SB/.run/backend.pid" && start_serve 5173 "$SB/.run/frontend.pid"; then
  out=$(FAKE_PLUGIN_ENABLED=1 "$SB/dev.sh" 2>&1); rc=$?
  echo "$out" | sed 's/^/    | /'
  calls=$(cat "$PY_CALLS_LOG" 2>/dev/null)
  [ "$rc" -eq 0 ]; chk $? "核心服务正常时仍返回 0（实际 rc=${rc}）"
  printf '%s' "$calls" | grep -q 'plugin_status.py'
  chk $? "探测通过后真的调了 scripts/plugin_status.py"
  printf '%s' "$out" | grep -q '插件 worker 屏障已通过'
  chk $? "屏障结果被报出来了（主服务活着不等于插件可用）"
else
  no "应答服务没起来 —— 本用例全部结论无效"
fi

echo
echo "---- dev.sh 沙箱演练：$PASS 通过，$FAIL 失败 ----"
[ "$FAIL" -eq 0 ]
