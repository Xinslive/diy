#!/usr/bin/env bash
# =============================================================================
# esa-realip-update.sh —— 把 ESA 的回源节点 IP 列表装成 nginx 的真实 IP 还原
#
#   用法（跑在源站上）：
#       bash esa-realip-update.sh <ip-list-file>
#
#   <ip-list-file> 就是 ESA 控制台「安全防护 → 源站防护 → 配置 → 开启 →
#   复制 IP 地址」给的那份列表（一行一个 IP 或网段，注释行和空行会跳过）。
#   也可以用 OpenAPI esa:GetOriginProtection 取：
#       CurrentIPWhitelist.IPv4 / .IPv6
#
#   装出来的文件：
#       /etc/nginx/cwq-include/cwq-esa-realip.conf
#   两份 nginx 模板里那行 `include /etc/nginx/cwq-include/cwq-esa-realip*.conf;`
#   是**通配** include —— 文件不存在时不报错，所以「没装」就是干净的关闭状态。
#
# ---------------------------------------------------------------------------
# 为什么非要这个
# ---------------------------------------------------------------------------
# 本系统有一层**按 IP 分桶**的登录节流（LOGIN_IP_*）。它取客户端 IP 的口径是
# 「先看 X-Real-IP，没有才看 X-Forwarded-For 最右一个」，而 X-Real-IP 是
# nginx 的 ``proxy_set_header X-Real-IP $remote_addr`` —— 也就是**源站看到的
# TCP 对端**。
#
# 源站直连时那是用户的 IP。**CDN 在前面时那是 CDN 的边缘节点 IP** ——
# 节点后面连着成千上万个用户，于是：
#
#   * 登录节流按「边缘节点」分桶 → 一个人在连着试错，同一节点上的其他人
#     一起被挡（误伤）；
#   * 审计日志里的 IP 全是节点地址 → 出事时根本查不到是谁。
#
# 而且这个故障**不报错**：节流照常工作，只是分桶的维度错了。
#
# ---------------------------------------------------------------------------
# 为什么 set_real_ip_from 的网段列表是安全边界，不能写 0.0.0.0/0
# ---------------------------------------------------------------------------
# 写成 ``set_real_ip_from 0.0.0.0/0`` 的话，任何能连到源站的人都可以自己塞
# 一个 ``X-Forwarded-For: 1.2.3.4``，把 IP 伪造成任意地址 —— 于是 IP 维度的
# 节流与审计全部失效（攻击者每个请求换一个头就换一个桶）。
#
# 所以这里必须**逐条列出 ESA 的回源网段**。
#
# **本脚本会自己拦这条边界**：解析完之后有一道前缀长度闸
# （IPv4 不得短于 /8、IPv6 不得短于 /16），
# ``0.0.0.0/0`` 与 ``::/0`` 会被直接拒绝并非零退出。
# 口径与后端一致（backend/app/core/config.py 的 _MIN_PROXY_PREFIXLEN = 8）——
# 同一件事，两边都要有闸。
#
# ESA 的回源网段**不是一份静态公开列表**：它按站点给出「收敛后的节点 IP」，
# 而且会变（阿里云会通过站内信/邮件通知）。两个来源：
#   1. ESA 控制台 → 站点 → 安全防护 → 源站防护 → 配置 → 开启 → 复制 IP 地址
#   2. OpenAPI ``esa:GetOriginProtection``（需要该站点的 SiteId）
#
# **网段变了要重跑一次这个脚本** —— 漏了新的网段，那批节点回源时真实 IP 就不
# 会还原（退化成分桶不准，不会报错，也不会更安全）。
#
# ---------------------------------------------------------------------------
# 头为什么用 X-Forwarded-For
# ---------------------------------------------------------------------------
# 实测（2026-10-24，本机对 acct.xins.live）：
#
#   客户端伪造 `X-Forwarded-For: 6.6.6.6` → 源站看到 `"6.6.6.6, 39.144.252.250"`
#
# 即 ESA 是**追加**语义：把自己看到的真实客户端 IP 追加在**最右边**。
# nginx 的 realip 模块默认（``real_ip_recursive off``）取 XFF 的**最后一个**
# 元素 —— 正好就是 ESA 看到的那个真实客户端 IP，伪造的前缀自动被忽略。
#
# 实测 ESA 是**单层回源**（XFF 只有 1 个元素），所以最右元素就是客户端本人。
# 若将来 ESA 启用多层缓存，XFF 可能变成 `客户端, 中间层地址`，那时最右元素
# 就不再是客户端了 —— 那时要改成 ``real_ip_recursive on``（从右往左跳过
# 受信网段取第一个不受信的）。
# =============================================================================
set -uo pipefail

LIST_FILE="${1:-}"
# 装到哪里。**默认是生产路径**，但允许用环境变量改 ——
# 理由有二：
#   * 测试要在临时目录里跑（这个脚本会往目标路径写文件，
#     而测试机上的 /etc/nginx 不是测试的垃圾桶）；
#   * 非标准部署形态（nginx 不在 /etc/nginx 下）也能用。
TARGET="${CWQ_REALIP_TARGET:-/etc/nginx/cwq-include/cwq-esa-realip.conf}"

# ---------------------------------------------------------------------------
# 生效的 vhost：**探测出来**，不写死。
# ---------------------------------------------------------------------------
#
# 原来这里声明了一个 `CONF="/etc/nginx/conf.d/acct.xins.live.conf"` 却
# **从头到尾没用过**（死变量）—— 而它写死的那个路径本身也不一定对：
# 仓库里唯一的 vhost 模板自己写着它落在宝塔的
# /www/server/panel/vhost/nginx/acct.xins.live.conf。
#
# 现在它有用了：**日志路径从生效配置自己的 access_log 里取**，
# 不然下面那句「验证：源站日志里的 remote_addr 应该变成真实客户端 IP」
# 会让运维去 tail 一个不存在的文件（宝塔形态是
# /www/wwwlogs/acct.xins.live.log，cwq.conf 形态是 /var/log/nginx/access.log）。
#
# 判据是内容：哪个候选文件里真的有 ssl_verify_client 指令，它就是生效的那份。
# 覆盖：CWQ_MTLS_CONF=/path/to/vhost.conf。
# ---------------------------------------------------------------------------
CONF_CANDIDATES=(
  "/etc/nginx/conf.d/acct.xins.live.conf"
  "/www/server/panel/vhost/nginx/acct.xins.live.conf"
  "/etc/nginx/conf.d/cwq.conf"
  "/www/server/panel/vhost/nginx/cwq.conf"
)
detect_conf(){
  # 先问 nginx 自己（``nginx -T`` 打出的是它**真正加载的**每一份配置，
  # 每一份前面带一行 ``# configuration file <路径>:``）——
  # 「哪一份里有 ssl_verify_client」由 nginx 自己回答，不靠我们猜路径。
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
[ -n "$CONF" ] || CONF="$(detect_conf || true)"
LOG=""
[ -n "$CONF" ] && LOG="$(log_path_from_conf "$CONF" || true)"

if [[ -z "$LIST_FILE" ]]; then
  echo "用法: $0 <ip-list-file>" >&2
  echo "  <ip-list-file>: ESA 控制台「源站防护」复制出来的 IP 列表" >&2
  exit 2
fi
if [[ ! -r "$LIST_FILE" ]]; then
  echo "✗ 读不到列表文件：$LIST_FILE" >&2
  exit 1
fi

# ---- 解析列表：只留 IPv4 / IPv6 的 IP 或网段 -------------------------------
IPS=()
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"                      # 去掉行尾注释
  line="$(echo "$line" | tr -d ' \t\r')"  # 去空白
  [[ -z "$line" ]] && continue
  # 允许 "1.2.3.4" / "1.2.3.0/24" / "2409:8c54::/32"
  if [[ "$line" =~ ^[0-9a-fA-F:.]+/[0-9]+$ ]]; then
    IPS+=("$line")
  elif [[ "$line" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    IPS+=("$line")
  else
    echo "· 跳过不认识的行：$line"
  fi
done < "$LIST_FILE"

if [[ "${#IPS[@]}" -eq 0 ]]; then
  echo "✗ 列表里一条可用的 IP 都没有 —— 不改任何东西" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 前缀长度闸：装上去之前先问「这条网段有多宽」
# ---------------------------------------------------------------------------
#
# 上面那个正则 `^[0-9a-fA-F:.]+/[0-9]+$` **照单全收** 0.0.0.0/0 与 ::/0 ——
# 而本文件开头（第 36-41 行）自己把「不能写 0.0.0.0/0」列为**安全边界**：
# 装上它之后，任何能连到源站的人都可以自己塞一个 X-Forwarded-For，
# 把自己的 IP 伪造成任意地址 —— 于是 IP 维度的节流与审计全部失效。
# 换句话说：**装上它，这个脚本存在的目的就被反转了。**
#
# 口径与后端一致（backend/app/core/config.py 的 _MIN_PROXY_PREFIXLEN = 8）：
# IPv4 的前缀不得短于 /8。
#
# IPv6 用 /16 而不是 /8：IPv6 的 /8 已经覆盖了半个地址空间，
# 而 ESA 的回源节点是按 /32 粒度给的 —— /16 足够宽松，又不至于把整个
# IPv6 空间交出去。后端那一侧对 v4/v6 用的是同一个 /8，
# 这里对 v6 更严，方向是安全的。
# ---------------------------------------------------------------------------
MIN_PREFIX_V4=8
MIN_PREFIX_V6=16

for entry in "${IPS[@]}"; do
  addr="$entry"
  prefix=""
  if [[ "$entry" == */* ]]; then
    addr="${entry%%/*}"
    prefix="${entry##*/}"
  fi
  # 单个 IP（没有前缀）= /32 或 /128，天然最窄，放行。
  [ -n "$prefix" ] || continue
  if [[ "$addr" == *:* ]]; then
    min="$MIN_PREFIX_V6"; max=128
  else
    min="$MIN_PREFIX_V4"; max=32
  fi
  if [[ "$prefix" -gt "$max" ]]; then
    echo "✗ ${entry} 的前缀长度超过 ${max} —— 不是合法网段。不改任何东西。" >&2
    exit 1
  fi
  if [[ "$prefix" -lt "$min" ]]; then
    echo "✗ 拒绝 ${entry}：前缀 /${prefix} 覆盖了几乎整个地址空间（最低要求 /${min}）。" >&2
    echo "   装上去之后，任何能直连源站的人都能自己塞一个 X-Forwarded-For，" >&2
    echo "   把自己的 IP 伪造成任意地址 —— IP 维度的节流与审计会全部失效。" >&2
    echo "   正是这个脚本要解决的情况，会被这一行反转。" >&2
    echo "   请从 ESA 控制台复制**具体的回源网段**（/24、/32 之类）。不改任何东西。" >&2
    exit 1
  fi
done
echo "· ${#IPS[@]} 条网段通过前缀闸（IPv4 ≥ /${MIN_PREFIX_V4}，IPv6 ≥ /${MIN_PREFIX_V6}）"

# ---------------------------------------------------------------------------
# nginx -t 的输出落盘：**必须用 mktemp**，不能写死 /tmp 下的固定文件名。
# ---------------------------------------------------------------------------
#
# shell 的 `>` 跟随符号链接并截断目标，而这个脚本是以 root 跑的。
# 固定路径 /tmp/nginx-t-realip.log ⇒ 本地任意低权账号先
# `ln -sf /opt/cwq/backend/.env /tmp/nginx-t-realip.log`，
# 运维随后以 root 跑本脚本 → **root 把 .env 截断成 nginx 的自检输出**。
# 这台机是共用主机（同时跑着另外 11 个子域与 3 个 Docker 容器），
# 这样的账号是存在的。
#
# mktemp 造出来的文件是 600、名字随机，本地账号抢不到也猜不到。
# ---------------------------------------------------------------------------
NGINX_T_LOG="$(mktemp)"
cleanup(){ rm -f "$NGINX_T_LOG"; }
trap cleanup EXIT

# ---- 生成配置文件 ----------------------------------------------------------
TMP="$(mktemp)"
{
  echo "# /etc/nginx/cwq-include/cwq-esa-realip.conf"
  echo "#"
  echo "# 由 deploy/esa-realip-update.sh 生成，请勿手改。"
  echo "# 生成时间: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "# 网段来源: ESA 控制台 → 站点 → 安全防护 → 源站防护（收敛后的回源节点 IP）"
  echo "#"
  echo "# ESA 的回源网段会变。**变了一定要重跑脚本**，否则新网段的回源不会"
  echo "# 还原真实 IP（退化成分桶不准，不报错）。"
  echo "#"
  echo "# 只有对端在下面这些网段里，才会采信 X-Forwarded-For —— 写 0.0.0.0/0"
  echo "# 等于让任何人都能伪造自己的 IP。"
  echo ""
  for ip in "${IPS[@]}"; do
    echo "set_real_ip_from $ip;"
  done
  echo ""
  echo "# 真实客户端 IP 取自 X-Forwarded-For 的**最后一个**元素。"
  echo "# ESA 是追加语义：把自己看到的真实客户端 IP 追加在最右边"
  echo "#（客户端伪造的前缀会被自动忽略 —— 实测过）。"
  echo "real_ip_header X-Forwarded-For;"
} > "$TMP"

# ---- 装上去（带备份 + nginx -t + 失败回滚）---------------------------------
install -d -m 0755 "$(dirname "$TARGET")"
BAK=""
if [[ -f "$TARGET" ]]; then
  BAK="${TARGET}.bak-$(date +%Y%m%d-%H%M%S)"
  cp -a "$TARGET" "$BAK"
  echo "· 已备份旧文件 → $BAK"
fi
# **写不进就要说清楚** —— 这个脚本没有 set -e，装失败会被忽略，
# 然后它还会打印「✅ 已写入」，那是一个**假的成功**。
#
# 属主：能以 root 跑就显式交给 root（nginx 以 root 读它），
# 不是 root 就别装 -o/-g（那会直接失败），文件归当前用户。
if [ "$(id -u)" = "0" ]; then
  INSTALL_OWNER=(-o root -g root)
else
  INSTALL_OWNER=()
fi
if ! install -m 0644 "${INSTALL_OWNER[@]}" "$TMP" "$TARGET"; then
  rm -f "$TMP"
  echo "✗ 写不进 ${TARGET} —— 不改任何东西（要 root 身份，或换一个可写的路径）" >&2
  exit 1
fi
rm -f "$TMP"
echo "✅ 已写入 ${TARGET}（${#IPS[@]} 条网段）"

if ! nginx -t >"$NGINX_T_LOG" 2>&1; then
  if [[ -n "$BAK" ]]; then cp -a "$BAK" "$TARGET"; echo "✗ nginx -t 失败 —— 已装回备份"; fi
  sed 's/^/    /' "$NGINX_T_LOG"
  exit 1
fi
echo "· nginx -t 通过"

if ! nginx -s reload; then
  if [[ -n "$BAK" ]]; then cp -a "$BAK" "$TARGET"; nginx -t >/dev/null 2>&1; echo "✗ reload 失败 —— 已装回备份"; fi
  exit 1
fi
echo "✅ 已 reload"

echo
echo "== 验证：源站日志里的 remote_addr 应该变成真实客户端 IP =="
if [ -n "$LOG" ]; then
  echo "   ssh root@<源站> 'tail -20 $LOG'"
else
  echo "   （日志路径没从生效的 vhost 配置里取到 —— 用 nginx -T 找到 access_log 的路径）"
fi
