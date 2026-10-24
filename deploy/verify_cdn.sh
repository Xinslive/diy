#!/usr/bin/env bash
# CDN 自检：静态资源真的被缓存了吗？该不缓存的有没有被缓存？
#
#   bash deploy/verify_cdn.sh https://acct.example.com
#
# 退出码 0 = 全部符合预期；非 0 = 至少一条不符（并逐条说明现象）。
#
# ---------------------------------------------------------------------------
# 为什么要有这个脚本
# ---------------------------------------------------------------------------
# 「静态资源走 CDN」这件事**没有报错通道**：
#
#   * CDN 没缓存 → 源站照样吐全部流量，**带宽账单不会变**，也没有任何日志；
#   * CDN 缓存了不该缓存的（比如入口 HTML）→ 用户拿到旧版本，**看起来
#     「发布没生效」**，而服务器日志一切正常；
#   * 静态资源没带 CORS 头 → 浏览器拒绝执行脚本，页面白屏，控制台一条
#     CORS 错误，而源站日志显示 200。
#
# 三种故障的共同点是「看起来不像故障」。所以只能**主动查**。
#
# ---------------------------------------------------------------------------
# 判据从哪来
# ---------------------------------------------------------------------------
# Cloudflare 的响应头 ``CF-Cache-Status``：
#   HIT        —— 命中边缘缓存（没回源）
#   MISS       —— 没命中，回源取了一次并缓存
#   DYNAMIC    —— **不缓存**（按规则绕过，或源站说 no-cache）
#   BYPASS     —— 被规则显式绕过
#   EXPIRED / STALE / UPDATING / REVALIDATED …
#
# 「不缓存」的路径期望 DYNAMIC 或 BYPASS；「该缓存」的路径期望 HIT。
# ---------------------------------------------------------------------------
set -uo pipefail

BASE="${1:-}"
if [[ -z "$BASE" ]]; then
  echo "用法：bash deploy/verify_cdn.sh https://acct.example.com" >&2
  exit 2
fi
BASE="${BASE%/}"

PASS=0; FAIL=0
ok(){ echo "  OK   $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL $1"; FAIL=$((FAIL+1)); }

# 取某个 URL 的响应头（小写键名，便于 grep）
head_of(){ curl -sS -I -m 20 "$1" 2>/dev/null | tr -d '\r' | tr 'A-Z' 'a-z'; }

# 取某个头的值（第一个）
value_of(){ grep -m1 "^$1:" <<<"$2" | cut -d: -f2- | sed 's/^ *//'; }

# 站点不可达判定：连响应头都拿不到，说明**根本没连上**。
# 这与「连上了但响应里没有 CF-Cache-Status」是**两件事** ——
# 前者是环境问题（域名没解析、证书不对、网络不通），后者才是「没上 CDN」。
# 混为一谈的话，「站点压根不通」会被判成「没上 CDN」，方向就跑偏了。
unreachable(){ [[ -z "$1" ]]; }

# 从入口 HTML 里抠出第一个静态资源（带哈希的那个）
entry_asset(){
  curl -sS -m 20 "$BASE/" 2>/dev/null \
    | grep -oE '(src|href)="/assets/[^"]+\.(js|css)"' \
    | head -1 | sed -E 's/.*"(.*)"/\1/'
}

echo "== CDN 自检：$BASE =="

# ---------------------------------------------------------------------------
# 1) 入口 HTML：必须**不缓存**
# ---------------------------------------------------------------------------
echo "-- 入口页 --"
H=$(head_of "$BASE/")
if unreachable "$H"; then
  no "站点不可达（连响应头都拿不到）—— 先查域名解析、证书与网络，**不是 CDN 的问题**"
  echo
  echo "通过 $PASS / 失败 $FAIL"
  [[ "$FAIL" == "0" ]]
  exit $?
fi
CF=$(value_of "cf-cache-status" "$H")
CC=$(value_of "cache-control" "$H")
if [[ -z "$CF" ]]; then
  no "响应里没有 CF-Cache-Status —— 这条路径前面**没有 Cloudflare**（或者被跳过了 CDN）"
elif [[ "$CF" == "hit" || "$CF" == "expired" || "$CF" == "stale" ]]; then
  no "入口页被 CDN 缓存了（CF-Cache-Status: ${CF}）—— 发布之后用户会一直拿到旧版本"
  no "  修法：Cache Rule 里让 / 不缓存（且源站的 Cache-Control: no-cache 要保留）"
else
  ok "入口页不缓存（CF-Cache-Status: ${CF}）"
fi
if [[ "$CC" == *no-cache* ]]; then
  ok "入口页的 Cache-Control 是 no-cache"
else
  no "入口页的 Cache-Control 是「${CC:-（没有）}」 —— 源站这一层也要 no-cache"
fi

# ---------------------------------------------------------------------------
# 2) 运行时配置：必须**不缓存**（它是 .js，默认会被 CDN 缓存）
# ---------------------------------------------------------------------------
echo "-- 运行时配置 --"
H=$(head_of "$BASE/runtime-config.js")
CF=$(value_of "cf-cache-status" "$H")
CC=$(value_of "cache-control" "$H")
if [[ "$CF" == "hit" || "$CF" == "expired" || "$CF" == "stale" ]]; then
  no "运行时配置被 CDN 缓存了（CF-Cache-Status: ${CF}）—— 改了 API/CDN 地址不会生效"
  no "  修法：Cache Rule ①（uri.path eq \"/runtime-config.js\" → Bypass cache）"
elif [[ -z "$CF" ]]; then
  no "没有 CF-Cache-Status（前面没有 CDN？）"
else
  ok "运行时配置不缓存（CF-Cache-Status: ${CF}）"
fi
if [[ "$CC" == *no-cache* ]]; then
  ok "运行时配置的 Cache-Control 是 no-cache"
else
  no "运行时配置的 Cache-Control 是「${CC:-（没有）}」"
fi

# ---------------------------------------------------------------------------
# 3) 静态资源：必须**被缓存**，且带 immutable
# ---------------------------------------------------------------------------
echo "-- 静态资源 --"
ASSET=$(entry_asset)
if [[ -z "$ASSET" ]]; then
  no "入口页里没找到 /assets/ 下的资源引用 —— 无法验证缓存行为"
else
  # 取两次：第一次通常 MISS（回源并缓存），第二次应 HIT
  head_of "$BASE$ASSET" > /dev/null
  H=$(head_of "$BASE$ASSET")
  CF=$(value_of "cf-cache-status" "$H")
  CC=$(value_of "cache-control" "$H")
  if [[ "$CF" == "hit" ]]; then
    ok "静态资源命中边缘缓存（CF-Cache-Status: HIT）—— 流量不再回源"
  elif [[ "$CF" == "miss" ]]; then
    ok "静态资源第一次 MISS（刚回源并缓存）—— 再请求一次应变成 HIT"
  elif [[ -z "$CF" ]]; then
    no "静态资源没有 CF-Cache-Status —— 前面没有 CDN，静态资源仍在回源"
  else
    no "静态资源的 CF-Cache-Status 是 $CF —— 期望 HIT（没缓存 = 源站照样吃满带宽）"
  fi
  if [[ "$CC" == *immutable* ]]; then
    ok "静态资源的 Cache-Control 带 immutable（${CC}）"
  else
    no "静态资源的 Cache-Control 是「${CC:-（没有）}」 —— 缺 immutable，长缓存拿不到"
  fi
  ACAO=$(value_of "access-control-allow-origin" "$H")
  if [[ -n "$ACAO" ]]; then
    ok "静态资源带 Access-Control-Allow-Origin（换 CDN 域名时跨域加载 ES module 需要它）"
  else
    no "静态资源没有 Access-Control-Allow-Origin —— 若资源跨域加载，浏览器会拒绝执行"
  fi
fi

# ---------------------------------------------------------------------------
# 4) 业务接口：必须**不被缓存**
# ---------------------------------------------------------------------------
echo "-- 业务接口 --"
H=$(head_of "$BASE/api/health")
CF=$(value_of "cf-cache-status" "$H")
if unreachable "$H"; then
  no "接口连不上（站点不可达）"
elif [[ "$CF" == "hit" ]]; then
  no "接口被 CDN 缓存了（CF-Cache-Status: HIT）—— 缓存住业务响应 = 把一个人的数据发给另一个人"
  no "  修法：Cache Rule ②（uri.path starts_with \"/api/\" → Bypass cache）"
elif [[ -z "$CF" ]]; then
  ok "接口响应里没有 CF-Cache-Status（连上了，但没有 CDN 头 → 没走 CDN）"
else
  ok "接口不缓存（CF-Cache-Status: ${CF}）"
fi

echo
echo "通过 $PASS / 失败 $FAIL"
[[ "$FAIL" == "0" ]]
