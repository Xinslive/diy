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

CONF="/etc/nginx/conf.d/acct.xins.live.conf"
CA="/etc/nginx/ssl/esa/esa-client-ca.pem"
LOG="/var/log/nginx/acct.xins.live.log"

usage(){ echo "用法: $0 {status|strict|relax}" >&2; exit 2; }

current_mode(){
  # 取指令的当前值（只认指令行本身，注释里的 optional 不算）
  sed -n 's/^[[:space:]]*ssl_verify_client[[:space:]]\+\([a-z_]*\);.*/\1/p' "$CONF" | head -1
}

do_switch(){ # $1 = 目标值 (on|optional)
  local target="$1"
  local cur
  cur="$(current_mode)"
  [[ -z "$cur" ]] && { echo "✗ 配置里找不到 ssl_verify_client 指令（$CONF）"; exit 1; }
  if [[ "$cur" == "$target" ]]; then
    echo "· 当前已经是 $target，无需改动"
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

  # 只改指令行：注释里出现 optional/on 不动
  sed -i "s/^\([[:space:]]*ssl_verify_client[[:space:]]\+\)${cur};/\1${target};/" "$CONF"

  if ! nginx -t >/tmp/nginx-t-mtls.log 2>&1; then
    cp -a "$bak" "$CONF"
    echo "✗ nginx -t 失败 —— 已自动装回备份"
    sed 's/^/    /' /tmp/nginx-t-mtls.log
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
    echo "  配置文件: $CONF"
    echo "  ESA 根证书: $( [[ -r $CA ]] && echo "已安装 ($(openssl x509 -in $CA -noout -enddate 2>/dev/null | cut -d= -f2))" || echo "缺失！")"
    echo "  ssl_verify_client: $(current_mode)"
    echo
    echo "== 最近 20 条请求的 mTLS 判定 =="
    if [[ -r "$LOG" ]]; then
      grep -oE 'mtls=[A-Za-z:0-9-]+' "$LOG" | tail -20 | sort | uniq -c | sed 's/^/  /'
      echo "  （NONE = 客户端没带证书：CDN 回源 / 直连测试；SUCCESS = ESA 证书验签通过）"
    else
      echo "  日志还没有：$LOG"
    fi
    ;;
  strict) do_switch on ;;
  relax)  do_switch optional ;;
  *)      usage ;;
esac
