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
# 钉住四件事：
#   1. 楔住时 ./dev.sh **必须返回非零**，且**不许**打印「好了」；
#   2. 它必须把楔住的那个进程组真的清掉 —— 否则下一次 ./dev.sh 照样起不来；
#   3. 预检不过时**不许**把 uvicorn 拉起来（闸门必须在进程起来之前）；
#   4. stop 必须 TERM → 等待 → KILL 升级，而不是 kill 一次就放弃。
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

cleanup() {
  for p in "${OWNED_PIDS[@]:-}"; do kill -KILL "$p" 2>/dev/null; done
  [ -n "$SB" ] && rm -rf "$SB"
}
trap cleanup EXIT

# --------------------------------------------------------------------------
# 造沙箱：dev.sh 靠 `cd "$(dirname "$0")"` 定 ROOT，所以复制一份进去它就
# 只会看见沙箱里的东西，不会碰到真的仓库。
# --------------------------------------------------------------------------
make_sandbox() {
  SB=$(mktemp -d "${TMPDIR:-/tmp}/cwq-devsh.XXXXXX")
  OWNED_PIDS=()
  # .run 必须先建：用例要在跑 dev.sh **之前**就往里写 pid 文件（楔住的
  # 进程是本用例起的，不是 dev.sh 起的），而 dev.sh 自己 mkdir 得太晚。
  mkdir -p "$SB/backend/.venv/bin" "$SB/frontend" "$SB/fakebin" "$SB/tools" "$SB/.run"
  cp "$REPO/dev.sh" "$SB/dev.sh"
  chmod +x "$SB/dev.sh"

  # 预检的替身。行为由 FAKE_PREFLIGHT_RC / FAKE_PREFLIGHT_MSG 控制 ——
  # 真实预检要连库，沙箱里不该连，判据（退出码与文案）才是被测的东西。
  cat > "$SB/backend/.venv/bin/python" <<'EOF'
#!/usr/bin/env bash
echo "${FAKE_PREFLIGHT_MSG:-  ✓ schema 就绪（数据库 revision 0025_backup_run）}"
exit "${FAKE_PREFLIGHT_RC:-0}"
EOF
  chmod +x "$SB/backend/.venv/bin/python"

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
echo "---- dev.sh 沙箱演练：$PASS 通过，$FAIL 失败 ----"
[ "$FAIL" -eq 0 ]
