#!/usr/bin/env bash
# 一键起开发环境：数据库 + 后端 8000 + 前端 5173
#
#   ./dev.sh          启动
#   ./dev.sh stop     停掉
#   ./dev.sh status   看状态（真的发请求，不靠 pid 文件猜）
#   ./dev.sh doctor   诊断并修复「端口在监听但没人应答」
#
# ---------------------------------------------------------------------------
# 为什么这个脚本现在这么啰嗦（2026-10-17 那次「后端又没有响应」的教训）
# ---------------------------------------------------------------------------
# 那天症状是：端口在监听、curl 不通、浏览器一直转圈 —— 和「服务器卡死」在
# 外面没有任何可观察的差别。真实链路是：
#
#   1. --reload 的 **reloader 父进程**自己 bind 住 :8000 的 LISTEN 套接字，
#      再把它 pickle 给 worker 子进程（uvicorn/_subprocess.py）。
#   2. worker 的 startup() 里 schema 闸门拦人 → 子进程抛异常退出。
#   3. 父进程还活着、还握着那个套接字，并且阻塞在 os_waitpid 等那个已经死掉
#      的子进程 —— 于是**永远没有人调 accept()**。
#
# 三个后果都要在这个脚本里处理，缺一个就会回到老形态：
#
#   * 闸门要长在**进程起来之前**（scripts/preflight.py），不能长在 uvicorn 的
#     startup 里。否则每拦一次人就产出一次「端口在监听但没人应答」。
#   * SIGTERM **杀不掉**它。reloader 的 signal handler 只做
#     should_exit.set()，而那个 Event 只在 pause()/should_restart() 里被看；
#     父进程此刻阻塞在 os_waitpid，事件永远轮不到被看一眼 —— 实测连发两次
#     SIGTERM 纹丝不动，kill -9 之后端口才释放。所以 stop 必须是
#     **TERM → 等 → 按进程组 KILL**，而不是 kill 一次就放弃。
#   * 起来之后必须**真的探到应答**才敢说「好了」。旧版 wait_http 失败只打一行
#     ✗，不清理、不返回非零，接着照常打印「好了：…」 —— 于是「假活」被当成
#     启动成功。那是这套系统最该防的形态，而不是一个可以忽略的小 bug。
#
# spawn() 用的是 os.setsid()，所以孙进程自己就是**进程组长**（pgid == pid），
# `kill -- -<pid>` 因此能一次干掉 reloader + worker 及其全部后代。
#
# ---------------------------------------------------------------------------
# 后端有两条启动路径（2026-10-18 接入受信任插件时加的第二条）
# ---------------------------------------------------------------------------
#   * 默认：`uvicorn --reload --reload-dir app`。热重载是这套开发体验的核心，
#     上面那些坑全是围绕它总结出来的。
#   * 本地开了插件：改走 `scripts/start_server.py`（supervisor）。原因不是
#     偏好，是**没有第三条路**：supervisor 每次启动生成一个随机 generation
#     交给全部 worker，插件的全 worker 屏障就建立在这个代号上。缺代号的
#     worker 直接 fail-closed（INVALID_WORKER_CONFIGURATION → 503）；而
#     --reload 每次代码重载都会换掉 worker 进程，补上代号也凑不齐 cohort，
#     结果是「插件永远不 READY」。生产侧（systemd、Docker）走的就是这一条，
#     本地不跟生产同构就等于本地调试的不是要上线的那套。
#
# 所以这一条路径**必然放弃热重载**：要改后端代码就 ./dev.sh stop && ./dev.sh。
#
# 两条路径是互斥分支，默认那条一个字节都没改。判据也不是自己解析 .env，而是
# 以 backend/ 为 cwd 问应用自己的 settings（见下面的 PLUGIN_ENABLED_LOCAL）：
# 解析两份配置迟早会在某次改动后静默分裂，然后开发者看到一片 503 却查不到
# 任何配置 —— 那比一开始就用 supervisor 难查得多。
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
RUN="$ROOT/.run"
mkdir -p "$RUN"

BACKEND_PORT=8000
FRONTEND_PORT=5173
HEALTH_URL="http://127.0.0.1:$BACKEND_PORT/api/health"
READY_URL="http://127.0.0.1:$BACKEND_PORT/api/ready"
VITE_URL="http://127.0.0.1:$FRONTEND_PORT/"
PY="$ROOT/backend/.venv/bin/python"
PREFLIGHT="$ROOT/backend/scripts/preflight.py"

# 探测的等待预算。做成环境变量只有一个理由：**CI 与沙箱演练要跑得快**。
# 这些探测里最慢的一环是 curl 自己的超时（PROBE_TIMEOUT），一次 wait 要重复
# 几十次，所以默认值直接决定一次失败启动要等多久。默认值按「冷启动也得来得及」
# 取：venv import + schema 版本判定 + 首轮迁移检查，45 秒是宽裕的下限。
# 调它们**不会**改变「起没起来」的判据，只改判据多久出结论。
PROBE_TIMEOUT="${CWQ_PROBE_TIMEOUT:-2}"
HEALTH_TIMEOUT="${CWQ_HEALTH_TIMEOUT:-45}"
READY_TIMEOUT="${CWQ_READY_TIMEOUT:-10}"
VITE_TIMEOUT="${CWQ_VITE_TIMEOUT:-30}"
# stop 时给 TERM 的宽限，0.25 秒一格。5 秒对「正常退出」足够，
# 对「被吞掉 TERM」是刚好够确认它真的没退（然后升级到 KILL）。
STOP_GRACE="${CWQ_STOP_GRACE:-20}"

# spawn <pid文件> <日志> <工作目录> <命令...>
#
# 两次 fork 之后，真正跑起来的是「孙进程」，中间进程和父进程都会立刻退出。
# 所以 pid 必须由孙进程自己写进 pid 文件 —— 让父进程打印 pid 记下来的会是那个
# 马上就没了的中间进程，下次 alive 判断永远是 false，不停地重复启动。
spawn() {
  local pidf=$1 log=$2 dir=$3; shift 3
  python3 - "$pidf" "$log" "$dir" "$@" <<'PYFORK'
import os, sys
pidf, log, cwd, argv = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
if not argv:
    sys.exit("没有命令要跑")
if os.fork():
    os._exit(0)          # 父进程：只负责让路
os.setsid()              # 新会话，脱离原进程组
if os.fork():
    os._exit(0)          # 中间进程退出，孙进程交给 init 收养
os.chdir(cwd)
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1); os.dup2(fd, 2)
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)
with open(pidf, "w") as f:            # 记孙进程自己的 pid，这才是要杀的那个
    f.write(str(os.getpid()))
os.execvp(argv[0], argv)
PYFORK
}

alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
port_pids() { lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null; }
pid_list() { port_pids "$1" | tr '\n' ' ' | tr -s ' '; }

# http_code <url> [超时秒]
#
# 任何 HTTP 状态码都算「应答了」：401 /api/books 正是没带 token 时的正确响应，
# 说明链路是通的。
http_code() {
  local code
  # 千万别写成 `|| echo 000`。curl 连不上时**自己**就会通过 -w 往 stdout 打一个
  # 000，再叠加一次就成了 "000000"，于是 != "000" 成立，被误判成「已就绪」。
  code=$(curl -s -m "${2:-$PROBE_TIMEOUT}" -o /dev/null -w '%{http_code}' "$1" 2>/dev/null)
  printf '%s' "${code:-000}"
}

log_tail() {  # log_tail <日志名> [行数]
  local f="$RUN/$1.log" n=${2:-12}
  [ -f "$f" ] || { echo "        （.run/$1.log 还不存在）"; return 0; }
  tail -n "$n" "$f" | sed 's/^/        /'
}

# 把日志里真正的报错捞出来。让人自己翻几百行日志去猜是哪一行炸了，
# 是排障失败的主要原因 —— 而这里恰恰有一行现成的答案。
log_hint() {  # log_hint <日志名>
  local f="$RUN/$1.log"
  [ -f "$f" ] || return 0
  grep -m 4 -E 'SchemaVersionError|Traceback|Error:|ERROR:' "$f" 2>/dev/null \
    | sed 's/^/        /'
}

# kill_group <pgid> <信号>
#
# 只在**确认是别人的进程组**时才发信号。`kill -- -0` 会打到当前进程组、
# `kill -- -1` 会打到几乎所有进程 —— 这两个都不是「清理残留」，
# 是把开发机整个搞挂。守卫在这里，不能靠调用方自觉。
kill_group() {
  local pgid=$1 sig=$2
  case "${pgid:-}" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$pgid" -gt 1 ] || return 1
  [ "$pgid" != "$$" ] || return 1
  kill -"$sig" -- "-$pgid" 2>/dev/null
}

start_one() {  # start_one <名字> <工作目录> <日志名> <命令...>
  local name=$1 dir=$2 log=$3; shift 3
  if alive "$name"; then
    echo "  · $name 已在跑（pid $(cat "$RUN/$name.pid")）"; return 0
  fi
  rm -f "$RUN/$name.pid"
  spawn "$RUN/$name.pid" "$RUN/$log.log" "$dir" "$@"
  # pid 文件是孙进程自己写的，与 spawn 返回之间有一小段竞态。
  # 不等就 cat，拿到的是空 —— 然后脚本会打印一个没有信息量的 "pid ?"。
  for _ in $(seq 1 20); do
    [ -s "$RUN/$name.pid" ] && break
    sleep 0.05
  done
  echo "  · $name 已拉起（pid $(cat "$RUN/$name.pid" 2>/dev/null || echo ?)，日志 .run/${log}.log）"
}

# stop_one <名字> <端口>
#
# TERM → 等 → **按进程组 KILL** → 确认端口真的空了。
#
# 为什么不能 kill 一次就放弃（2026-10-17 实测）：
#   * pid 文件里那个 pid 是 setsid 之后的孙进程，它自己就是进程组长。只 kill
#     组长的话，reloader 派生的 worker 会被 init 收养，继续占着端口。
#   * uvicorn --reload 的父进程**吞 SIGTERM**（见文件头）。TERM 之后不升级到
#     KILL，端口就一直被占着，下一次启动就是
#     `ERROR: [Errno 48] Address already in use`，而新的 worker 又 bind 失败退出
#     —— 于是「重启也起不来」。
#   * 端口上可能还有 pid 文件管不到的残留（中间进程崩了、上一轮手动起的），
#     所以最后还要按端口兜一次。
#   * 说「停了」必须以**端口真的空掉**为准，而不是以 kill 返回 0 为准。
stop_one() {
  local name=$1 port=$2 p pgid=""
  if alive "$name"; then
    p=$(cat "$RUN/$name.pid")
    pgid=$(ps -o pgid= -p "$p" 2>/dev/null | tr -d ' ')
    if kill_group "${pgid:-}" TERM; then
      echo "  · $name 进程组 $pgid 已收到 TERM（pid ${p}）"
    else
      kill -TERM "$p" 2>/dev/null && echo "  · $name (pid $p) 已收到 TERM"
    fi
    local i
    for ((i = 0; i < STOP_GRACE; i++)); do alive "$name" || break; sleep 0.25; done
    if alive "$name"; then
      # TERM 被吞了 —— 就是 --reload 那个形态。上 KILL，而且要带上整个组。
      if kill_group "${pgid:-}" KILL; then
        echo "  · $name 5 秒内没退（SIGTERM 被吞），已 KILL 整个进程组 $pgid"
      else
        kill -KILL "$p" 2>/dev/null && echo "  · $name 已 KILL"
      fi
      sleep 0.5
    fi
  fi
  for p in $(port_pids "$port"); do
    kill -KILL "$p" 2>/dev/null \
      && echo "  · $name 端口 $port 上的残留 (pid $p) 也 KILL 了"
  done
  rm -f "$RUN/$name.pid"
  local left
  left=$(port_pids "$port" | tr '\n' ' ')
  if [ -n "$left" ]; then
    echo "  ✗ $name 的端口 $port 仍被 [$left] 占着，没能清掉。" >&2
    echo "    这些 pid 很可能不是这个脚本拉起来的（手起的、或别人在跑）。" >&2
    echo "    确认无用之后手动：kill -9 $left" >&2
    return 1
  fi
  return 0
}

# wait_live <名字> <url> <超时秒>：等到**有应答**为止
#
# 计时用墙钟而不是「循环 60 次」：旧写法是 60 × (curl -m 2 + sleep 0.5)，
# 最坏要 150 秒，却对外宣称「20 秒内没应答」—— 报错文案和实际行为对不上，
# 排障的人会先怀疑自己的判断。
wait_live() {
  local name=$1 url=$2 timeout=$3 deadline code
  deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    code=$(http_code "$url" "$PROBE_TIMEOUT")
    if [ "$code" != "000" ]; then
      echo "  ✓ $name 应答（HTTP ${code}）"; return 0
    fi
    sleep 0.5
  done
  echo "  ✗ $name ${timeout} 秒内没应答" >&2
  return 1
}

# wait_backend：先要「有应答」，再要「就绪」。两件事不能混。
#
# /api/health 是**存活**探针，故意不碰数据库；schema 版本与数据库要问
# /api/ready。只探 health 的话，一个 schema 不对但进程活着的后端会被报成
# 「起来了」—— 而那正是它下一次 reload 就会崩掉的形态。
wait_backend() {
  local code
  wait_live backend "$HEALTH_URL" "$HEALTH_TIMEOUT" || return 1
  code=$(http_code "$READY_URL" "$READY_TIMEOUT")
  case "$code" in
    200) echo "  ✓ backend 就绪（数据库可达且 schema 版本对得上）"; return 0 ;;
    503)
      echo "  ✗ backend 活着但**未就绪**（HTTP 503）：数据库不可达，或 schema 版本不符。" >&2
      return 2 ;;
    *)   echo "  ✗ backend /api/ready 返回 HTTP $code" >&2; return 1 ;;
  esac
}

# explain_backend_failure：失败时说清楚是哪一种。
#
# 这三种从外面看一模一样（curl 不通），处置动作却完全不同：不区分的话，
# 唯一的办法就是把日志从头翻到尾 —— 而 2026-10-17 那次就是这么耗掉的。
explain_backend_failure() {
  local port=$BACKEND_PORT
  echo >&2
  if [ -n "$(port_pids "$port")" ]; then
    echo "  · 端口 $port 上有进程在 LISTEN（pid $(pid_list "$port")），但没人应答。" >&2
    echo "    这是 --reload 的已知形态：reloader 父进程还活着、还握着套接字，" >&2
    echo "    而它的 worker 已经退出，于是没有人调 accept()。SIGTERM 也杀不掉" >&2
    echo "    它（父进程卡在 waitpid，收不到那个让它退出的 Event）。" >&2
  elif alive backend; then
    echo "  · 进程还在（pid $(cat "$RUN/backend.pid")），但端口 $port 没监听 —— 半拉起来。" >&2
  else
    echo "  · 端口 $port 上没有 LISTEN 进程 —— 后端进程自己退了（启动就崩了）。" >&2
  fi
  echo "  · 日志里的真正报错（.run/api.log）：" >&2
  log_hint api
  echo "  · 日志末尾： .run/api.log" >&2
  log_tail api
}

# report_status <名字> <端口> <url>
#
# 状态必须**真的发一个请求**，不能只看 pid 文件在不在。
#
# 那天就是这么被误导的：pid 文件里的 reloader 父进程活得好好的，
# status 报「backend 运行中」，而实际上一个请求都进不来。
# 「看起来在工作但其实没生效」—— 这是这套系统反复在防的那个形态，
# 而它最常见的来源就是用一个不反映真实行为的判据。
report_status() {
  local name=$1 port=$2 url=$3 code
  if [ -n "$(port_pids "$port")" ]; then
    code=$(http_code "$url" "$PROBE_TIMEOUT")
    if [ "$code" != "000" ]; then
      echo "  $name  活着并应答（HTTP ${code}，端口 $port 上 pid $(pid_list "$port")）"
    else
      echo "  $name  **端口 $port 在监听但没人应答**（pid $(pid_list "$port")）" >&2
      echo "        就是 --reload 卡死的形态：reloader 还活着，worker 没了，没人 accept。" >&2
      echo "        修：./dev.sh stop（TERM→KILL 整个进程组），再 ./dev.sh" >&2
    fi
  elif alive "$name"; then
    echo "  $name  进程在（pid $(cat "$RUN/$name.pid")）但端口 $port 没监听 —— 半拉起来" >&2
  else
    echo "  $name  没跑"
  fi
}

case "${1:-start}" in
stop)
  rc=0
  stop_one backend "$BACKEND_PORT" || rc=1
  stop_one frontend "$FRONTEND_PORT" || rc=1
  [ "$rc" -eq 0 ] && echo "  · 都停了"
  exit "$rc" ;;
status)
  report_status backend "$BACKEND_PORT" "$HEALTH_URL"
  report_status frontend "$FRONTEND_PORT" "$VITE_URL"
  exit 0 ;;
doctor)
  # 「端口在监听但没人应答」的定点诊断与修复。分开做成子命令，是因为它
  # 要在**不动数据库、不重启任何东西**的前提下先给出结论。
  echo "诊断 $BACKEND_PORT ..."
  if [ -z "$(port_pids "$BACKEND_PORT")" ] && ! alive backend; then
    echo "  · 后端没跑，也没有残留监听 —— 直接 ./dev.sh 就行"
    exit 0
  fi
  if [ "$(http_code "$HEALTH_URL" "$PROBE_TIMEOUT")" != "000" ]; then
    echo "  · 后端应答正常，不需要修"
    exit 0
  fi
  echo "  ✗ 端口在监听但没人应答 —— 这是 --reload 卡死的形态"
  log_hint api
  echo "  · 正在按进程组清掉（TERM → 5 秒 → KILL）..."
  stop_one backend "$BACKEND_PORT"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "  · 已清干净。现在 ./dev.sh 起新的。"
  else
    echo "  · 没清干净，见上面的 pid。" >&2
  fi
  exit "$rc" ;;
esac

echo "1) 数据库"
if docker ps --format '{{.Names}}' | grep -qx cwq-pg; then
  echo "  · cwq-pg 已在跑"
else
  docker start cwq-pg >/dev/null && echo "  · cwq-pg 已启动"
fi

echo "2) 后端 :$BACKEND_PORT"
# --reload 只盯 app/：改后端代码立刻生效，不用每次手工重启。
# 不加的话 uvicorn 会把改过的文件一直缓存着 —— 页面上看着像 bug 修了，
# 其实跑的还是十分钟前的旧代码（踩过：结转守卫改了，报错文案还是旧的）。
#
# APP_ENV=dev 是必需的：生产配置校验默认按 prod 处理（fail-closed），
# 本机没有 .env 时会带着默认密钥被拒绝启动。显式声明开发环境即可跳过。
# 用 export 而不是「VAR=x 命令」的前缀写法：前缀写法在 bash 里作用于
# **函数**时，变量会在函数返回后仍然留着，容易在后面几行意外生效。
export APP_ENV="${APP_ENV:-dev}"

# 插件开关的**唯一事实来源是应用自己的配置**（含 backend/.env）。
#
# 不在这里重新解析一遍 .env：解析两份就会在某次改动后静默分裂 —— 这个脚本
# 以为插件关着、进程却开着，于是开发者看到的是一片 503，却查不到任何配置。
#
# 必须以 backend/ 为 cwd 跑：pydantic-settings 从**当前目录**读 .env，在
# 仓库根执行会漏读 backend/.env，正好制造上面那种分裂。
#
# 探测不到就当关着 —— 默认路径必须和今天一模一样。
PLUGIN_ENABLED_LOCAL=0
if [ -x "$PY" ]; then
  probe=$(cd "$ROOT/backend" && "$PY" -c \
    "from app.core.config import settings; print('1' if settings.plugin_enabled else '0')" 2>/dev/null)
  [ "$probe" = "1" ] && PLUGIN_ENABLED_LOCAL=1
fi

STARTED_BACKEND=0
if alive backend; then
  echo "  · backend 已在跑（pid $(cat "$RUN/backend.pid")）"
else
  # 闸门前移：预检不过就**不起进程**。
  #
  # 这是 2026-10-17 那次故障的核心修复。放在 uvicorn 的 startup 里拦，
  # 每拦一次人就得到一个「端口在监听、没人应答、又杀不掉」的进程；
  # 放在进程起来之前拦，得到的是一条看得懂的报错和一个干净的退出码。
  if [ ! -x "$PY" ]; then
    echo "  ✗ 找不到 $PY —— 先建虚拟环境并装依赖" >&2
    echo "    cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
  fi
  if ! "$PY" "$PREFLIGHT"; then
    echo >&2
    echo "  后端**没有起来**，也不应该起来：schema/数据库预检没过（原因在上面）。" >&2
    echo "  照上面给的命令处理完，再跑 ./dev.sh。" >&2
    exit 1
  fi
  if [ "$PLUGIN_ENABLED_LOCAL" -eq 1 ]; then
    # 插件开启时**放弃热重载**，改走 supervisor 入口（scripts/start_server.py）。
    #
    # 为什么不能两条都要：supervisor 每次启动生成一个随机 generation 交给
    # 全部 worker，而 --reload 的每次代码重载都会换掉 worker 进程。没有
    # generation 的 worker 直接 fail-closed（workers.py 的
    # INVALID_WORKER_CONFIGURATION → 503）；就算补上，重载产生的**半截
    # cohort** 也永远凑不齐 expected worker 数，结果就是「插件永远不 READY」。
    # 这与首版不热加载的设计是同一件事：重启才换代号。
    #
    # 要改后端代码就 ./dev.sh stop && ./dev.sh。
    echo "  · 检测到 PLUGIN_ENABLED：走 supervisor 入口（放弃热重载），worker=${CWQ_API_WORKERS:-2}"
    start_one backend "$ROOT/backend" api .venv/bin/python scripts/start_server.py \
      --host 127.0.0.1 --port "$BACKEND_PORT" --workers "${CWQ_API_WORKERS:-2}"
  else
    start_one backend "$ROOT/backend" api .venv/bin/uvicorn app.main:app \
      --host 127.0.0.1 --port "$BACKEND_PORT" --reload --reload-dir app
  fi
  STARTED_BACKEND=1
fi

echo "3) 前端 :$FRONTEND_PORT"
STARTED_FRONTEND=0
if alive frontend; then
  echo "  · frontend 已在跑（pid $(cat "$RUN/frontend.pid")）"
else
  start_one frontend "$ROOT/frontend" vite npm run dev -- \
    --host 127.0.0.1 --port "$FRONTEND_PORT"
  STARTED_FRONTEND=1
fi

echo "4) 就绪探测"
RC=0
if ! wait_backend; then
  explain_backend_failure
  RC=1
fi
if [ "$RC" -eq 0 ] && ! wait_live frontend "$VITE_URL" "$VITE_TIMEOUT"; then
  echo "  · 前端没起来。看 .run/vite.log：" >&2
  log_hint vite
  log_tail vite
  RC=1
fi

# 插件屏障：主服务活着**不代表**插件可用。任一 worker 没起来、租约失效或
# 代次不同，所有插件请求都一致 503 —— 和 deploy.sh 发布后的那道闸门同构。
#
# 为什么在开发环境也返回非零：这个脚本存在的全部理由就是不许把「没真起来」
# 报成「好了」（见文件头 2026-10-17）。本地显式开了插件却拿不到屏障，是同一
# 类假活：核心账务能用，但插件页一进去就是 503，与其让人自己撞上，不如这里
# 说清楚。plugin_status.py 在插件关闭或注册表为空时返回 0，不会假失败。
if [ "$RC" -eq 0 ] && [ "$PLUGIN_ENABLED_LOCAL" -eq 1 ]; then
  if (cd "$ROOT/backend" && "$PY" scripts/plugin_status.py --wait "${CWQ_PLUGIN_BARRIER_TIMEOUT:-30}"); then
    echo "  · 插件 worker 屏障已通过"
  else
    echo "  ✗ 插件 worker 屏障未通过 —— 插件请求会一律 503，见 .run/api.log" >&2
    echo "    核心记账不受影响。查 PLUGIN_ROOT / PLUGIN_TRUSTED_KEYS，" >&2
    echo "    或 PLUGIN_ENABLED=false 后重跑。" >&2
    RC=1
  fi
fi

if [ "$RC" -ne 0 ]; then
  # 清理分两种，区别在于那个进程是不是**坏的**：
  #
  #  * 自己这次拉起来的 → 清掉，不留半拉起来的东西。
  #  * 之前就在那儿、但正楔着（端口在监听却没人应答）→ **照样清掉**，哪怕不是
  #    本次起的。留着它的代价是确定的：8000 被占着，下一次 ./dev.sh 只会得到
  #    `[Errno 48] Address already in use`，新的 worker 又 bind 失败退出 ——
  #    于是「重启也起不来」。而这个状态本身无可争议：没人应答的后端没有任何
  #    保留价值。至于「之前就在那儿、只是启动失败」的健康进程，不动它 ——
  #    把别人正在用的东西杀掉，比启动失败更糟。
  if [ -n "$(port_pids "$BACKEND_PORT")" ] && \
     [ "$(http_code "$HEALTH_URL" "$PROBE_TIMEOUT")" = "000" ]; then
    echo "  · 正在清理楔住的后端（端口在监听但没人应答）：" >&2
    stop_one backend "$BACKEND_PORT"
  elif [ "$STARTED_BACKEND" -eq 1 ]; then
    stop_one backend "$BACKEND_PORT"
  fi
  [ "$STARTED_FRONTEND" -eq 1 ] && stop_one frontend "$FRONTEND_PORT"
  echo >&2
  echo "启动失败：这里**不会**打印访问地址。" >&2
  echo "一个没起来的后端配上「好了」三个字，比任何一条错误信息都更容易" >&2
  echo "让人在错误的路上耗下去 —— 2026-10-17 那次就是这么查了很久。上面是原因。" >&2
  exit 1
fi

echo
echo "好了：http://127.0.0.1:$FRONTEND_PORT   （停：./dev.sh stop）"
