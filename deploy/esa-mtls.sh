#!/usr/bin/env bash
# =============================================================================
# cwq-esa-mtls.sh —— ESA 回源双向校验（mTLS）开关
#
#   status   看当前是 optional（过渡）还是 on（强制），以及最近的 mtls 判定
#   strict   切到强制校验：没有 ESA 客户端证书的连接一律拒绝
#   relax    切回过渡态：没带证书的放行，带了证书的仍然要验签
#
# 纪律（每一步都不可省）：
#   1. 改之前先备份；
#   2. 改完先 `nginx -t`，**过了**才 reload；
#   3. 任何一步失败自动装回备份，线上永远跑的是能起来的配置。
#   4. 只 reload 不 restart —— 旧进程继续服务，reload 失败不影响线上。
# =============================================================================
set -uo pipefail

# ---------------------------------------------------------------------------
# 生效的 vhost 与日志：**探测出来，不写死**。
# ---------------------------------------------------------------------------
#
# 原来这里写死了两件事，而**两件都可能不是真的**：
#
#   CONF="/etc/nginx/conf.d/acct.xins.live.conf"
#   LOG="/var/log/nginx/acct.xins.live.log"
#
# 而仓库里唯一的 vhost 模板（deploy/nginx/acct.xins.live.conf）自己在第 5 行
# 写着它落在**宝塔**的路径下：
#
#   /www/server/panel/vhost/nginx/acct.xins.live.conf
#
# 它第 503 行的 access_log 又是第三个路径：
#
#   access_log /www/wwwlogs/acct.xins.live.log cwq_mtls;
#
# 后果是这个脚本**永远拿不到它存在的唯一理由**：切 strict 之前要的那份证据。
#   * `status` 在 $CONF 里找不到 ssl_verify_client → 打印
#     「✗ 配置里找不到 ssl_verify_client 指令」退出 1；
#   * 就算配置路径对了，$LOG 也不存在 → 永远打印「日志还没有」→
#     mtls= 的判定证据**永远看不到**。
#
# 而这个脚本存在的意义恰恰是在切 on 之前拿到证据（下面 do_switch 里的警告
# 说的就是：切到 on 会让回源和直连测试域名一起断）。
#
# **判据是内容而不是路径**：哪个候选文件里**真的有** ssl_verify_client 指令，
# 它就是生效的那一份。这样两种部署形态（/etc/nginx/conf.d/ 与 宝塔的
# /www/server/panel/vhost/nginx/）都能用，路径写错了也不会误判。
#
# 覆盖：CWQ_MTLS_CONF=/path/to/vhost.conf 可以直接指定（探不出来时用）。
# ---------------------------------------------------------------------------
CONF_CANDIDATES=(
  "/etc/nginx/conf.d/acct.xins.live.conf"
  "/www/server/panel/vhost/nginx/acct.xins.live.conf"
  "/etc/nginx/conf.d/cwq.conf"
  "/www/server/panel/vhost/nginx/cwq.conf"
)

# 找出真正生效的那份 vhost 配置（里面有 ssl_verify_client 指令的那份）。
#
# **先问 nginx 自己**：`nginx -T` 会把它**真正加载的**每一份配置都打出来，
# 每一份前面都带一行 ``# configuration file <路径>:`` ——
# 于是「哪一份里有 ssl_verify_client」由 nginx 自己回答，而不是我们猜路径。
# 这直接消掉了审计里那条「生产机 vhost 的真实落盘路径需要登录目标机确认」
# 的不确定性：不管它落在 /etc/nginx/conf.d/ 还是宝塔的
# /www/server/panel/vhost/nginx/，甚至是个别的路径，nginx 都知道。
#
# 问不到（没装 nginx / 非 root / -T 失败）时退回候选列表按内容探。
detect_conf(){
  local t
  if t="$(nginx -T 2>/dev/null)"; then
    local hit
    hit="$(printf '%s\n' "$t" | awk '
      /^# configuration file /{ f=$4; sub(/:$/,"",f); next }
      /^[[:space:]]*ssl_verify_client[[:space:]]/{
        if (f != "") { print f; exit }
      }
    ')"
    if [ -n "$hit" ] && [ -r "$hit" ]; then
      printf '%s' "$hit"
      return 0
    fi
  fi
  local c
  for c in "${CONF_CANDIDATES[@]}"; do
    [ -r "$c" ] || continue
    grep -qE '^[[:space:]]*ssl_verify_client[[:space:]]+' "$c" || continue
    printf '%s' "$c"
    return 0
  done
  return 1
}

# 日志路径**跟着配置走**：从 vhost 自己的 access_log 指令里取。
#
# 为什么不能写死：宝塔那份写的是 /www/wwwlogs/…，而 cwq.conf 那份写的是
# /var/log/nginx/access.log（还是与其它站点共用的那个）。
# 写死一个路径 ⇒ 另一个形态下永远打印「日志还没有」。
#
# 取的是**第一条**指向真实文件的 access_log；`access_log off;` 要跳过
# （location 级关日志是常见写法，先撞上它就取错了）。
log_path_from_conf(){
  local conf="$1" line p
  while IFS= read -r line; do
    p="$(printf '%s' "$line" | awk '{print $2}')"
    p="${p%;}"
    [ -n "$p" ] || continue
    [ "$p" != "off" ] || continue
    case "$p" in /*) ;; *) continue ;; esac
    printf '%s' "$p"
    return 0
  done < <(grep -E '^[[:space:]]*access_log[[:space:]]+' "$conf" 2>/dev/null)
  return 1
}

CONF="${CWQ_MTLS_CONF:-}"
if [ -z "$CONF" ]; then
  CONF="$(detect_conf || true)"
fi

# ESA 根证书。默认是生产路径；CWQ_MTLS_CA 可以改 ——
# 理由与 CWQ_MTLS_CONF 一样：**这个脚本需要能在测试机上被真的跑一遍**，
# 而它的每一步（备份 → 改 → nginx -t → reload）都是会改磁盘的动作。
CA="${CWQ_MTLS_CA:-/etc/nginx/ssl/esa/esa-client-ca.pem}"
LOG=""
[ -n "$CONF" ] && LOG="$(log_path_from_conf "$CONF" || true)"

usage(){ echo "用法: $0 {status|strict|relax}" >&2; exit 2; }

current_mode(){
  [ -n "$CONF" ] || return 0
  # 取指令的当前值（只认指令行本身，注释里的 optional 不算）。
  #
  # **用 `[[:space:]][[:space:]]*` 而不是 `[[:space:]]\+`** ——
  # BRE 里的 `\+` 是 GNU 的扩展，BSD sed（macOS / FreeBSD）把它当成
  # 一个字面的 `+`：**一条都匹配不到**。
  # 后果不是「报错」，而是**静默地什么都读不出来**：status 永远显示空，
  # 而 do_switch 的那条 sed 更是改不动配置却照样打印「✅ 已切到 on」。
  # 目标机是 Debian（GNU sed）所以那边没暴露，但脚本不该依赖 sed 的实现。
  sed -n 's/^[[:space:]]*ssl_verify_client[[:space:]][[:space:]]*\([a-z_]*\);.*/\1/p' "$CONF" | head -1
}

# ---------------------------------------------------------------------------
# nginx -t 的输出落盘：**必须用 mktemp**。
# ---------------------------------------------------------------------------
#
# 原来写的是固定路径 `>/tmp/nginx-t-mtls.log`。shell 的 `>` **跟随符号链接并
# 截断目标** —— 而这个脚本是以 root 跑的。
#
# 这台机是共用主机（cwq-api.service 自述「同时跑着另外 11 个子域与 3 个
# Docker 容器」），于是任意本地低权账号只要先做一次：
#
#     ln -sf /etc/nginx/conf.d/acct.xins.live.conf /tmp/nginx-t-mtls.log
#
# 运维随后以 root 跑 `bash esa-mtls.sh strict` → **root 把那个文件截断成
# nginx 的自检输出**。同法可指向 /opt/cwq/backend/.env（销毁全部密钥）、
# /etc/passwd、/root/.ssh/authorized_keys。
#
# 内容不可控（写进去的是 nginx 的自检输出），所以那是破坏/拒绝服务而不是
# 直接 RCE —— 但「root 身份任意路径覆盖」本身已越过可接受边界。
#
# mktemp 造出来的文件是 600、且名字随机，本地账号抢不到也猜不到。
# ---------------------------------------------------------------------------
NGINX_T_LOG="$(mktemp)"
cleanup(){ rm -f "$NGINX_T_LOG"; }
trap cleanup EXIT

do_switch(){ # $1 = 目标值 (on|optional)
  local target="$1"
  if [ -z "$CONF" ]; then
    echo "✗ 找不到带 ssl_verify_client 指令的 vhost 配置（试过：${CONF_CANDIDATES[*]}）" >&2
    echo "   在这台机器上先确认它在哪：" >&2
    echo "     nginx -T 2>/dev/null | grep -n ssl_verify_client" >&2
    echo "   找到之后用 CWQ_MTLS_CONF=/path/to/vhost.conf 指定，或把它加进本脚本的候选列表。" >&2
    exit 1
  fi
  local cur
  cur="$(current_mode)"
  [[ -z "$cur" ]] && { echo "✗ 配置里找不到 ssl_verify_client 指令（${CONF}）"; exit 1; }
  if [[ "$cur" == "$target" ]]; then
    echo "· 当前已经是 ${target}，无需改动"
    return 0
  fi
  if [[ ! -r "$CA" ]]; then
    echo "✗ ESA 根证书不存在：$CA —— 先装上再切强制校验"
    exit 1
  fi

  local ts bak
  ts="$(date +%Y%m%d-%H%M%S)"
  bak="${CONF}.bak-mtls-${ts}"
  cp -a "$CONF" "$bak"
  echo "· 已备份 → $bak"

  # 只改指令行：注释里出现 optional/on 不动。
  #
  # **同样不能用 `\+`**（理由见 current_mode 的注释）：BSD sed 上它匹配不到，
  # 于是配置**根本没改**却照样往下走、照样打印「✅ 已切到 on」——
  # 一个「看着成功、其实什么都没做」的状态，正是这个脚本最该避免的。
  # 改完**核对一次**：值没变就当失败处理并装回备份。
  sed -i.bak "s/^\([[:space:]]*ssl_verify_client[[:space:]][[:space:]]*\)${cur};/\1${target};/" "$CONF"
  if [[ "$(current_mode)" != "$target" ]]; then
    cp -a "$bak" "$CONF"
    rm -f "${CONF}.bak"
    echo "✗ 没能把 ${cur} 改成 ${target} —— 已自动装回备份" >&2
    echo "   （这个 sed 表达式在这台机器的 sed 上没能匹配上）" >&2
    exit 1
  fi
  rm -f "${CONF}.bak"

  if ! nginx -t >"$NGINX_T_LOG" 2>&1; then
    cp -a "$bak" "$CONF"
    echo "✗ nginx -t 失败 —— 已自动装回备份"
    sed 's/^/    /' "$NGINX_T_LOG"
    exit 1
  fi
  echo "· nginx -t 通过"

  if ! nginx -s reload; then
    cp -a "$bak" "$CONF"
    nginx -t >/dev/null 2>&1
    echo "✗ reload 失败 —— 已自动装回备份，线上仍是旧进程在服务"
    exit 1
  fi
  echo "✅ 已切到 $target 并 reload"
  echo "   （当前前置是 EdgeOne 时，切到 on 会让 EdgeOne 回源和直连测试域名一起断，"
  echo "     确认 ESA 已接管全部流量再切）"
}

case "${1:-}" in
  status)
    echo "== 当前配置 =="
    if [ -z "$CONF" ]; then
      echo "  配置文件: ✗ 没找到（试过：${CONF_CANDIDATES[*]}）"
      echo "     先确认它在哪：nginx -T 2>/dev/null | grep -n ssl_verify_client"
      echo "     找到之后用 CWQ_MTLS_CONF=/path/to/vhost.conf 指定"
    else
      echo "  配置文件: $CONF"
    fi
    echo "  ESA 根证书: $( [[ -r $CA ]] && echo "已安装 ($(openssl x509 -in $CA -noout -enddate 2>/dev/null | cut -d= -f2))" || echo "缺失！")"
    echo "  ssl_verify_client: $(current_mode)"
    echo
    echo "== 最近 20 条请求的 mTLS 判定 =="
    if [ -n "$LOG" ] && [[ -r "$LOG" ]]; then
      grep -oE 'mtls=[A-Za-z:0-9-]+' "$LOG" | tail -20 | sort | uniq -c | sed 's/^/  /'
      echo "  （NONE = 客户端没带证书：CDN 回源 / 直连测试；SUCCESS = ESA 证书验签通过）"
    else
      echo "  日志还没有：${LOG:-<没从配置里取到 access_log 路径>}"
      [ -n "$LOG" ] || echo "     日志路径是从 vhost 的 access_log 指令里取的；该配置里没找到可用的 access_log"
    fi
    ;;
  strict) do_switch on ;;
  relax)  do_switch optional ;;
  *)      usage ;;
esac
