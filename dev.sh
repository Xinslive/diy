#!/usr/bin/env bash
# 一键起开发环境：数据库 + 后端 8000 + 前端 5173
#
# 反复撞到「127.0.0.1 拒绝建立连接」，根因是后端/前端都挂在当时那个终端的
# 进程组上，终端一关、进程组一被回收，两个服务就一起没了。
# macOS 没有 setsid（Linux 才有），所以这里用 Python 的 os.setsid() 做标准
# 的双 fork 脱离：第一次 fork 让父进程拿到 pid 就退出，第二次 fork 之后子进程
# 已经被 init 收养，再 setsid 脱离进程组 —— 这样关终端、收进程组都杀不到它。
#
#   ./dev.sh          启动
#   ./dev.sh stop     停掉
#   ./dev.sh status   看状态
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
RUN="$ROOT/.run"
mkdir -p "$RUN"

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

# wait_http <url> <名字> <日志名>：任意 HTTP 状态码都算「应答了」。
# 401 /api/books 正是没带 token 时的正确响应，说明链路是通的。
wait_http() {
  for _ in $(seq 1 60); do
    # 注意别写成 `|| echo 000`：curl 连不上时自己就会往 stdout 打一个 000，
    # 再叠加一次就成了 "000000"，反而 != 000，被误判成「已就绪」。
    code=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null) || code=000
    if [ "$code" != "000" ]; then echo "  ✓ $2 应答（HTTP ${code}）"; return 0; fi
    sleep 0.5
  done
  echo "  ✗ $2 20 秒内没应答，看 .run/$3.log" >&2
  return 1
}

start_one() {  # start_one <名字> <工作目录> <日志名> <命令...>
  local name=$1 dir=$2 log=$3; shift 3
  if alive "$name"; then
    echo "  · $name 已在跑（pid $(cat "$RUN/$name.pid")）"; return 0
  fi
  spawn "$RUN/$name.pid" "$RUN/$log.log" "$dir" "$@"
  echo "  · $name 已拉起（pid $(cat "$RUN/$name.pid" 2>/dev/null || echo ?)，日志 .run/${log}.log）"
}

stop_one() {  # 先按 pid 杀，杀不掉再按端口杀（npm 会把信号转给 node）
  local name=$1 port=$2 p
  if alive "$name"; then
    p=$(cat "$RUN/$name.pid"); kill "$p" 2>/dev/null && echo "  · $name (pid $p) 已停"
  fi
  for p in $(port_pids "$port"); do
    kill "$p" 2>/dev/null && echo "  · $name 端口 $port 上的残留 (pid $p) 也清了"
  done
  rm -f "$RUN/$name.pid"
}

case "${1:-start}" in
stop)
  stop_one backend 8000
  stop_one frontend 5173
  exit 0 ;;
status)
  alive backend  && echo "  backend  运行中（pid $(cat "$RUN/backend.pid")）"  || echo "  backend  没跑"
  alive frontend && echo "  frontend 运行中（pid $(cat "$RUN/frontend.pid")）" || echo "  frontend 没跑"
  exit 0 ;;
esac

echo "1) 数据库"
if docker ps --format '{{.Names}}' | grep -qx cwq-pg; then
  echo "  · cwq-pg 已在跑"
else
  docker start cwq-pg >/dev/null && echo "  · cwq-pg 已启动"
fi

echo "2) 后端 :8000"
# --reload 只盯 app/：改后端代码立刻生效，不用每次手工重启。
# 不加的话 uvicorn 会把改过的文件一直缓存着 —— 页面上看着像 bug 修了，
# 其实跑的还是十分钟前的旧代码（踩过：结转守卫改了，报错文案还是旧的）。
#
# APP_ENV=dev 是必需的：生产配置校验默认按 prod 处理（fail-closed），
# 本机没有 .env 时会带着默认密钥被拒绝启动。显式声明开发环境即可跳过。
# 用 export 而不是「VAR=x 命令」的前缀写法：前缀写法在 bash 里作用于
# **函数**时，变量会在函数返回后仍然留着，容易在后面几行意外生效。
export APP_ENV="${APP_ENV:-dev}"
start_one backend "$ROOT/backend" api .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app

echo "3) 前端 :5173"
start_one frontend "$ROOT/frontend" vite npm run dev -- --host 127.0.0.1 --port 5173

echo "4) 就绪探测"
wait_http "http://127.0.0.1:8000/api/books" backend api
wait_http "http://127.0.0.1:5173/" frontend vite

echo
echo "好了：http://127.0.0.1:5173   （停：./dev.sh stop）"
