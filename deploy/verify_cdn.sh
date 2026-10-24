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
# 各家 CDN 都用一个「这次请求在边缘是什么状态」的头，判断依据就是它。
# **两家取值不一样，别拿一家当另一家用**：
#
#   Cloudflare（``CF-Cache-Status``）
#     HIT        —— 命中边缘缓存（没回源）
#     MISS       —— 没命中，回源取了一次并缓存
#     DYNAMIC    —— **不缓存**（按规则绕过，或源站说 no-cache）
#     BYPASS     —— 被规则显式绕过
#     EXPIRED / STALE / UPDATING / REVALIDATED …
#
#   腾讯云 EdgeOne（``EO-Cache-Status``）
#     HIT        —— 命中缓存且未过期，节点直接响应
#     MISS       —— 未命中，或命中但已过期且回源校验后源站有更新（回源取了一次）
#     RefreshHit —— 命中但已过期，回源校验后源站**无**更新（304），继续用缓存响应
#     Dynamic    —— 资源无法缓存 / 未配置缓存，由源站提供
#
#   阿里云 ESA（``X-Site-Cache-Status``，另有 ``X-Cache``）
#     HIT        —— 命中 ESA 边缘缓存（没回源）
#     MISS       —— 未命中，回源取了一次
#     另有 ``X-Swift-CacheTime``：该资源在 ESA 节点上能被缓存多久；
#     为 0 表示这个请求**不能**被缓存（动态接口 / 源站没给 ETag 等）。
#     ESA 的 ``X-Cache`` 还可能是 ``TCP_HIT`` / ``TCP_MISS``（阿里云 CDN
#     体系的通用取值），那也是「命中 / 未命中」，不是别的意思。
#
#   注意 EdgeOne **没有** DYNAMIC / BYPASS 这两个写法（它用 Dynamic），
#   也没有 CF 那个 STALE / UPDATING 家族。所以下面统一**归一化**成
#   hit / miss / bypass / other 四种再判断，别直接比字符串。
#
# 「不缓存」的路径期望 bypass 或 other；「该缓存」的路径期望 hit。
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

# ---------------------------------------------------------------------------
# 识别前置 CDN 是哪一家
# ---------------------------------------------------------------------------
# **为什么要认多家**：这个脚本是给「发布后自检」用的，跑的是**生产域名**。
# 生产域名前面挂的 CDN 由运维决定，不由仓库决定 —— 脚本只认一家的话，
# 在另一家底下会满屏 FAIL，而那些 FAIL 说的其实只是「不认识这个头」。
# 认不出来时报「没上 CDN」，别报成「缓存没生效」，方向不能错。
# ---------------------------------------------------------------------------
CDN_PROVIDER=""
detect_cdn(){
  local h="$1"
  if [[ -n "$(value_of "cf-cache-status" "$h")" ]]; then
    CDN_PROVIDER="Cloudflare"
  elif [[ -n "$(value_of "eo-cache-status" "$h")" ]]; then
    CDN_PROVIDER="腾讯云 EdgeOne"
  elif [[ -n "$(value_of "x-site-cache-status" "$h")" ]]; then
    CDN_PROVIDER="阿里云 ESA"
  elif [[ -n "$(value_of "x-cache" "$h")" ]]; then
    CDN_PROVIDER="通用 X-Cache"
  else
    CDN_PROVIDER="（未识别：响应里没有任何 CDN 缓存状态头）"
  fi
}

# 取缓存状态并**归一化**：hit / miss / bypass / other
#
# 归一化而不是直接比字符串，是因为各家的取值集合不一样（见文件头）：
# EdgeOne 的 Dynamic 与 Cloudflare 的 DYNAMIC 是一回事，
# EdgeOne 的 RefreshHit（回源校验后继续用缓存）算「命中」，
# 阿里云 ESA / CDN 体系的 TCP_HIT / TCP_MISS 也是「命中 / 未命中」。
cache_status(){
  local h="$1" v
  v=$(value_of "cf-cache-status" "$h")
  [[ -n "$v" ]] || v=$(value_of "eo-cache-status" "$h")
  [[ -n "$v" ]] || v=$(value_of "x-site-cache-status" "$h")
  [[ -n "$v" ]] || v=$(value_of "x-cache" "$h")
  v=$(echo "$v" | tr 'A-Z' 'a-z' | tr -d ' ')
  case "$v" in
    hit|refreshhit|tcp_hit) echo "hit" ;;
    miss|tcp_miss)         echo "miss" ;;
    bypass|dynamic)        echo "bypass" ;;
    "")                    echo "" ;;
    *)                     echo "other" ;;
  esac
}

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

# 先认 CDN 是哪一家，后面所有判据都按它的取值口径来
detect_cdn "$(head_of "$BASE/")"
echo "-- 前置 CDN：${CDN_PROVIDER} --"

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
ST=$(cache_status "$H")
CC=$(value_of "cache-control" "$H")
if [[ -z "$ST" ]]; then
  no "响应里没有任何 CDN 缓存状态头 —— 这条路径前面**没有 CDN**（或者被跳过了加速）"
elif [[ "$ST" == "hit" ]]; then
  no "入口页被 CDN 缓存了（${CDN_PROVIDER}：hit）—— 发布之后用户会一直拿到旧版本"
  no "  修法：缓存规则里让 / 不缓存（且源站的 Cache-Control: no-cache 要保留）"
else
  ok "入口页不缓存（${CDN_PROVIDER}：${ST}）"
fi
if [[ "$CC" == *no-cache* || "$CC" == *max-age=0* ]]; then
  ok "入口页的 Cache-Control 是「${CC}」（不缓存）"
else
  no "入口页的 Cache-Control 是「${CC:-（没有）}」 —— 源站这一层也要 no-cache"
fi

# ---------------------------------------------------------------------------
# 2) 运行时配置：必须**不缓存**（它是 .js，默认会被 CDN 缓存）
# ---------------------------------------------------------------------------
echo "-- 运行时配置 --"
H=$(head_of "$BASE/runtime-config.js")
ST=$(cache_status "$H")
CC=$(value_of "cache-control" "$H")
if [[ -z "$ST" ]]; then
  no "没有 CDN 缓存状态头（前面没有 CDN？）"
elif [[ "$ST" == "hit" ]]; then
  no "运行时配置被 CDN 缓存了（${CDN_PROVIDER}：hit）—— 改了 API/CDN 地址不会生效"
  no "  修法：缓存规则里让 /runtime-config.js 不缓存（源站的 no-cache 也要保留）"
else
  ok "运行时配置不缓存（${CDN_PROVIDER}：${ST}）"
fi
if [[ "$CC" == *no-cache* || "$CC" == *max-age=0* ]]; then
  ok "运行时配置的 Cache-Control 是「${CC}」（不缓存）"
else
  no "运行时配置的 Cache-Control 是「${CC:-（没有）}」"
fi
if [[ "$(value_of "content-type" "$H")" == text/html* ]]; then
  no "运行时配置返回的是 **HTML**（不是 JS）—— 这个文件在源站上根本不存在，"
  no "  落到了 SPA 兜底。说明这版前端还没发布（运行时配置是发布时装配的）"
fi

# ---------------------------------------------------------------------------
# 3) 静态资源：必须**被缓存**，且带 immutable
# ---------------------------------------------------------------------------
echo "-- 静态资源 --"
ASSET=$(entry_asset)
if [[ -z "$ASSET" ]]; then
  no "入口页里没找到 /assets/ 下的资源引用 —— 无法验证缓存行为"
else
  # 取两次：第一次通常 MISS（回源并缓存），第二次应 HIT。
  # **连着两次都 MISS 就是缓存没生效** —— 那才是要报的故障。
  head_of "$BASE$ASSET" > /dev/null
  H=$(head_of "$BASE$ASSET")
  ST=$(cache_status "$H")
  CC=$(value_of "cache-control" "$H")
  if [[ "$ST" == "hit" ]]; then
    ok "静态资源命中边缘缓存（${CDN_PROVIDER}：hit）—— 流量不再回源"
  elif [[ "$ST" == "miss" ]]; then
    no "静态资源**连续两次都是 miss** —— 缓存**没生效**，源站照样吃满带宽"
    no "  查两处：CDN 的缓存规则是不是把 /assets/ 排除了；CDN 是不是把源站的 Cache-Control 改写了"
  elif [[ -z "$ST" ]]; then
    no "静态资源没有任何 CDN 缓存状态头 —— 前面没有 CDN，静态资源仍在回源"
  else
    no "静态资源的缓存状态是 ${ST}（${CDN_PROVIDER}）—— 期望 hit（没缓存 = 源站照样吃满带宽）"
  fi
  if [[ "$CC" == *immutable* ]]; then
    ok "静态资源的 Cache-Control 带 immutable（${CC}）"
  else
    no "静态资源对外的 Cache-Control 是「${CC:-（没有）}」 —— 缺 immutable，长缓存拿不到"
    no "  （注意 CDN 可能改写源站的头；要确认源站本身对不对，另行直连源站核）"
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
ST=$(cache_status "$H")
if unreachable "$H"; then
  no "接口连不上（站点不可达）"
elif [[ "$ST" == "hit" ]]; then
  no "接口被 CDN 缓存了（${CDN_PROVIDER}：hit）—— 缓存住业务响应 = 把一个人的数据发给另一个人"
  no "  修法：缓存规则里让 /api/ 一律不缓存"
elif [[ -z "$ST" ]]; then
  ok "接口响应里没有 CDN 缓存状态头（连上了，但没有 CDN 头 → 没走 CDN）"
else
  ok "接口不缓存（${CDN_PROVIDER}：${ST}）"
fi

echo
echo "通过 $PASS / 失败 $FAIL"
[[ "$FAIL" == "0" ]]
