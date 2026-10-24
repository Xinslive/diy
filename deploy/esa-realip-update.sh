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
TARGET="/etc/nginx/cwq-include/cwq-esa-realip.conf"
CONF="/etc/nginx/conf.d/acct.xins.live.conf"

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
install -d -m 0755 /etc/nginx/cwq-include
BAK=""
if [[ -f "$TARGET" ]]; then
  BAK="${TARGET}.bak-$(date +%Y%m%d-%H%M%S)"
  cp -a "$TARGET" "$BAK"
  echo "· 已备份旧文件 → $BAK"
fi
install -m 0644 -o root -g root "$TMP" "$TARGET"
rm -f "$TMP"
echo "✅ 已写入 $TARGET（${#IPS[@]} 条网段）"

if ! nginx -t >/tmp/nginx-t-realip.log 2>&1; then
  if [[ -n "$BAK" ]]; then cp -a "$BAK" "$TARGET"; echo "✗ nginx -t 失败 —— 已装回备份"; fi
  sed 's/^/    /' /tmp/nginx-t-realip.log
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
echo "   ssh root@<源站> 'tail -20 /var/log/nginx/acct.xins.live.log'"
