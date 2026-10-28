import axios from 'axios'
import { ElMessage } from 'element-plus'
import { apiBase } from '@/config'
import queryCache, { CACHE_DEFAULTS } from '@/data/queryCache'
import { QUERY_CACHE_VERSION } from '@/data/queryKeys'

/**
 * API 基址来自运行时配置（见 @/config）。
 *
 * **为什么不是写死的 `/api`**：动静分离之后，API 可能不在源站同源位置 ——
 * 前端挂在 CDN、API 留在源站时，浏览器发出的请求就变成跨域，`/api` 这个
 * 相对路径会打到 CDN 域名上去（404，或者更糟：命中 CDN 上别人的路径）。
 * 写死 `/api` 的那一刻，「前后端分域名」这件事就不可能了。
 *
 * 回落值是 `/api`（同源），所以不改配置时行为与从前完全一致。
 */
export const http = axios.create({ baseURL: apiBase, timeout: 30000 })

/**
 * 登录页的地址。**唯一一份**。
 *
 * 路由表里注册的是 `path: '/login'`（见 router/index.js 的 routes），
 * 而本项目用的是 **hash 路由**（createWebHashHistory），所以浏览器地址是
 * `#/login`。以前这两个字符串各写一份（这里是 '#/login'，那边是 '/login'），
 * 改了一处忘了另一处 —— 令牌一失效就跳到一个不存在的地址，页面上只剩一片白。
 * 改路由表里的登录页路径时，**这里也要一起改**（反之亦然）。
 */
export const LOGIN_HASH = '#/login'

/**
 * 由 main.js 在 Pinia 建好之后注入：``clearSession() => useSessionStore().logout()``。
 *
 * 为什么要注入而不是直接 import：
 *   client.js 被所有 api 模块 import，session.js 又 import 了 api/auth ——
 *   在这里 import session 就是 client → session → api/auth → client 的环，
 *   ESM 下会拿到未初始化的模块（undefined），而且**不报错**，只是静默失效。
 *   那个 try/catch 版本正好踩中：require 在 ESM 里不存在，抛错被吞掉，
 *   测试立刻从 3 条失败变成「通过」—— 恒绿测试的又一次。
 *
 *   注入则由 app 装配时决定，环不存在，失败也不会被静默吞掉。
 */
let clearSession = () => {
  localStorage.removeItem('cwq_token')
}
export function onUnauthorized(fn) { clearSession = fn || clearSession }
let pluginRejected = () => {}
export function onPluginRejected(fn) { pluginRejected = fn }

/**
 * 「高危动作撞到二次验证闸门」的处理函数。**注入**，不 import。
 *
 * 理由与上面 ``clearSession`` 完全一样：重新验证要走 ``api/auth.js``
 * （mfaReauth / mfaPoll），而 api/auth.js import 了本文件 ——
 * client → auth → client 是个环，ESM 下不报错，只是静默拿到 undefined。
 *
 * 处理函数收到 403，返回 **true** 表示「已经拿到带新鲜验证时刻的新令牌，
 * 可以重试原动作了」，返回 false / 抛错表示「没通过，原动作没执行」。
 */
let mfaReauthHandler = null
export function onMfaReauth(fn) { mfaReauthHandler = fn }

/**
 * 后端「需要近期二次验证」时回的机器可读错误码。
 *
 * **与后端同一个字符串**（backend/app/core/deps.py::MFA_REAUTH_REQUIRED）——
 * 那边的用例会跨语言核对一遍，改了一边忘了另一边会被测出来。
 *
 * **为什么不靠中文文案去匹配**：拿协议去猜协议（``detail.includes('二次验证')``）
 * 的话，文案一改就**静默失效** —— 403 还是那个 403，只是没人弹引导了，
 * 管理员撞死路，而且没有任何报错。
 */
export const MFA_REAUTH_REQUIRED = 'MFA_REAUTH_REQUIRED'

/**
 * 「这一趟已经因为 MFA 闸门重试过一次」的记号。
 *
 * **必须有**：重新验证之后若还是 403（宽限期是 0、通道坏了、……），
 * 没有这个记号就会**再进一次重新验证** —— 无限循环，每一轮都发一封信。
 */
const MFA_RETRIED = '__cwqMfaRetried'

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('cwq_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (r) => {
    invalidateAfterMutation(r.config)
    return r
  },
  async (error) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail

    // ---- 高危动作撞到「近期二次验证」闸门 ---------------------------
    //
    // 「该操作需要近期完成邮箱二次验证」这条 403 在闸门扩到 13 条受管控
    // 写路由之后，是管理员**唯一**会撞上的墙（审计 §3.2）。没有下文的话，
    // 管理员只会看到一句 403 —— 界面上没有「去重新验证」的入口，
    // 撞死路，而且没有任何报错提示他该做什么。
    //
    // 这里做三件事：
    //   1. 认错误码（不是认文案）；
    //   2. 交给注入的处理器去做重新验证（发信 → 点链接 → 换新令牌）；
    //   3. 拿到新令牌后**自动重试原动作**。
    //
    // **重试是安全的**：闸门在**依赖**里，FastAPI 先跑依赖再跑路由体 ——
    // 被 403 拦下的那一刻，路由体**一行都没执行**，没有任何副作用。
    // 所以原样重发一遍不会「做两遍」。
    if (status === 403 && detail?.code === MFA_REAUTH_REQUIRED
        && mfaReauthHandler && error.config && !error.config[MFA_RETRIED]) {
      error.config[MFA_RETRIED] = true
      let ok = false
      try {
        ok = await mfaReauthHandler(error)
      } catch (e) {
        // 处理器自己抛错（发信失败、轮询出错……）—— 当作「没通过」。
        // 下面统一按失败处理，这里不把异常往上抛（那会让调用方拿到
        // 一个与「重新验证」无关的错，掩盖真正的原因）。
        ok = false
      }
      if (ok) {
        // 重试要走 http.request(config)：请求拦截器会**重新**从 localStorage
        // 取令牌，那儿已经是重新验证换来的新令牌了。
        return http.request(error.config)
      }
      // 没通过：处理器**已经把话说清楚了**（对话框里写着为什么不行），
      // 所以下面**不再**弹一次一样的 toast —— 同一件事说两遍只会让人
      // 更困惑（「到底成没成」）。
      return Promise.reject(error)
    }

    if (status === 401) {
      // 令牌过期/失效。清 localStorage 之外，**必须同步清 Pinia 内存态**。
      //
      // 原来只 removeItem('cwq_token')，而 session store 的 token/user/books
      // 是登录时写进 state 的副本，还留在内存里 —— isAuthed 读的是 s.token，
      // 于是页面在**不刷新**的前提下继续按「已登录」渲染，菜单、账套、
      // 期间全都在，直到某个请求再失败一次才被发现。
      //
      // 这里不能在模块顶层 import store：client.js 被所有 api 模块 import，
      // session.js 又 import 了 api/auth —— 顶层 import 会形成环。
      // 改成惰性取：store 还没被创建（用户从没登录）时就不清，那本来也没有
      // 需要清的内存态。
      if (clearSession) clearSession()
      if (!location.hash.includes('/login')) location.hash = LOGIN_HASH
      return Promise.reject(error)
    }
    if (status === 404 && detail?.code === 'PLUGIN_NOT_FOUND_OR_UNAVAILABLE') {
      const match = error.config?.url?.match(/^\/plugins\/([a-z0-9.-]+)\//)
      if (match) pluginRejected(match[1])
    }
    // **取消不是错误**：被 abort 掉的请求同样没有 response。
    // 它是「用户自己放弃的那一趟」（切期间时掐掉上一趟，见 getLatest），
    // 弹一条红字是纯噪音，还会盖住同期真正需要报的错误。
    if (isCanceled(error)) return Promise.reject(error)
    // **这一趟的失败是「预期之中」**：调用方在请求上标了 ``silent``。
    //
    // 有些请求拿不到**本来就该拿不到** —— 比如「可制证清单」那个接口会写库
    // （要持久化去重标记），后端按设计把只读账号挡在外面；只读账号打开
    // 「流水明细」时那一趟必然 403，而**流水行其实好好地显示着**。
    // 这种失败弹一条红字是**假错误**：用户看着满屏正常的数据，却被告知
    // 「当前账套为只读权限」，还以为自己点错了什么。
    //
    // 标了 ``silent`` 的请求失败时**拦截器不出声**，由调用方自己降级
    // （``catch`` 里那一段）—— 该弹的红字一条都不少，只少这一类。
    if (error.config?.silent) return Promise.reject(error)
    const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail[0]?.msg : detail?.message
    if (msg) ElMessage.error(msg)
    else if (!error.response) {
      // **网络失败 / 超时**：后端根本没回话，所以没有 detail 可显示。
      // 以前这里一声不吭 —— 页面把「加载失败」渲染成「没有数据」，
      // 用户打开总账看到一张空表、零提示，于是以为这个科目一整年没发生额
      // （审计 §3.25）。这里补一条兜底提示，让失败**至少**有声音。
      ElMessage.error(error.code === 'ECONNABORTED'
        ? '请求超时（30 秒），请检查网络后重试'
        : '网络异常：请求没有发出去或没有收到回应，请检查网络后重试')
    } else if (status >= 500) {
      // **服务端说「我出错了」，却没说清是什么错**（比如 500，而响应体里没有 detail）。
      // 以前这里同样一声不吭：``if (msg)`` 不成立、``!error.response`` 也不成立 →
      // 页面把「加载失败」渲染成「没有数据」，用户看着一张空表、零提示，
      // 于是以为这个月真的一笔都没有（审计 N3 / §3.25）。
      // 兜底提示：失败**至少**要有声音。
      //
      // 只兜 **5xx**：4xx 是「请求本身不对」（权限、参数、找不到），
      // 那几条路前端自己清楚，弹一句笼统的话反而盖住真正的信息。
      ElMessage.error(`服务器出错了（${status}），请稍后重试；若一直这样，请联系管理员`)
    }
    return Promise.reject(error)
  }
)

const uncachedGet = http.get.bind(http)

function safeStorage(key) {
  try { return typeof localStorage === 'undefined' ? null : localStorage.getItem(key) }
  catch { return null }
}

function requestPath(url) {
  try { return new URL(url, apiBase || location.origin).pathname.toLowerCase() }
  catch { return String(url || '').split('?')[0].toLowerCase() }
}

function requestBookId(path, params) {
  const inPath = path.match(/\/books\/(\d+)(?:\/|$)/)
  const fromParams = params?.book_id ?? params?.bookId
  if (inPath) return Number(inPath[1])
  if (fromParams && Number.isFinite(Number(fromParams))) return Number(fromParams)
  const current = Number(safeStorage('cwq_book') || 0)
  return current || null
}

function userNamespace() {
  const token = safeStorage('cwq_token') || ''
  try {
    const user = JSON.parse(safeStorage('cwq_user') || 'null')
    if (user?.id != null) return `user:${user.id}`
  } catch { /* stale user snapshot; token still isolates the request */ }
  return token ? `token:${token.slice(-16)}` : 'anonymous'
}

function canCacheGet(path, config = {}) {
  if (config.cache === false || config.skipCache) return false
  if (config.responseType && config.responseType !== 'json') return false
  if (/(?:^|\/)api\/(?:auth|plugins)(?:\/|$)/.test(path)
      || /^(?:\/)?(?:auth|plugins)(?:\/|$)/.test(path)) return false
  if (/(?:^|[/-])(?:download|export|upload|parse|next|neighbour|neighbor|mfa|poll|tick)(?:[-/]|$)/.test(path)) return false
  if (/\/print\/preview(?:\/|$)/.test(path)) return false
  if (/\/print(?:\/|$)/.test(path) && !/\/print\/templates(?:\/|$)/.test(path)) return false
  return true
}

function queryProfile(path) {
  if (/\/books\/\d+\/periods(?:\/|$)/.test(path)
      || /\/system\/(?:users|permissions|roles)(?:\/|$)/.test(path)
      || /period-check/.test(path)
      || /\/books\/\d+\/init(?:\/|$)/.test(path)) return 'immediate'
  if (/\/ledger\/|\/balance(?:\/|$)|\/reports\/\d+\/render/.test(path)) return 'ledger'
  if (/\/books(?:\/|$)|\/subjects(?:\/|$)|\/print-templates(?:\/|$)|\/templates(?:\/|$)/.test(path)) return 'metadata'
  return 'list'
}

function queryTags(path, bookId) {
  const tags = []
  if (/\/vouchers?(?:\/|$)|\/invoices?(?:\/|$)|\/bank-statements?(?:\/|$)/.test(path)) tags.push('vouchers')
  if (/\/ledger\/|\/balance(?:\/|$)/.test(path)) tags.push('ledger')
  if (/\/reports?(?:\/|$)/.test(path)) tags.push('reports')
  if (/\/subjects?(?:\/|$)/.test(path)) tags.push('subjects')
  if (/\/periods?(?:\/|$)/.test(path)) tags.push('periods')
  if (/\/books?(?:\/|$)/.test(path)) tags.push('books')
  if (/audit/.test(path)) tags.push('audit')
  if (/\/users?(?:\/|$)|permissions|roles/.test(path)) tags.push('users')
  if (/print|template/.test(path)) tags.push('templates')
  if (/scheduler/.test(path)) tags.push('scheduler')
  if (!tags.length) tags.push(`resource:${path.split('/').filter(Boolean).slice(1, 3).join(':') || 'api'}`)
  if (bookId) tags.push(`book:${bookId}`)
  return tags
}

function axiosCachedResponse(data, config) {
  return { data, status: 200, statusText: 'OK', headers: {}, config }
}

function requestCacheKey(path, config = {}) {
  const params = config.params instanceof URLSearchParams
    ? Object.fromEntries([...config.params.entries()].sort(([a], [b]) => a.localeCompare(b)))
    : (config.params || {})
  return [
    QUERY_CACHE_VERSION,
    'http-get',
    userNamespace(),
    Number(safeStorage('cwq_book') || 0) || null,
    Number(safeStorage('cwq_period') || 0) || null,
    path,
    params,
  ]
}

http.get = function cachedGet(url, config = {}) {
  const path = requestPath(url)
  if (!canCacheGet(path, config)) return uncachedGet(url, config)

  const key = requestCacheKey(path, config)
  const bookId = requestBookId(path, config.params)
  const profile = queryProfile(path)
  const policy = CACHE_DEFAULTS[profile]
  const tags = queryTags(path, bookId)
  const scope = bookId ? 'book' : 'user'
  const snapshot = queryCache.peek(key)
  const networkConfig = { ...config }
  delete networkConfig.cache
  delete networkConfig.skipCache

  const fetchData = () => uncachedGet(url, {
    ...networkConfig,
    cache: false,
    silent: true,
    __cwqBackground: true,
  }).then((response) => response.data)
  if (snapshot) {
    if (snapshot.stale) {
      void queryCache.fetch(key, fetchData, {
        ...policy, profile, tags, scope, cacheBackground: true
      }).catch(() => {})
    }
    return Promise.resolve(axiosCachedResponse(snapshot.data, config))
  }

  return queryCache.fetch(key, fetchData, { ...policy, profile, tags, scope })
    .then((data) => axiosCachedResponse(data, config))
}

function invalidationFor(path) {
  if (/\/auth(?:\/|$)/.test(path)) return []
  if (/\/vouchers?(?:\/|$)|\/invoices?(?:\/|$)|\/bank-statements?(?:\/|$)/.test(path)) {
    return ['vouchers', 'ledger', 'reports', 'periods', 'audit']
  }
  if (/\/subjects?(?:\/|$)/.test(path)) return ['subjects', 'ledger', 'reports', 'voucher-form', 'audit']
  if (/\/periods?(?:\/|$)|\/openings?(?:\/|$)|year-end|carry-forward/.test(path)) {
    return ['books', 'periods', 'vouchers', 'ledger', 'reports', 'openings', 'audit']
  }
  if (/\/reports?(?:\/|$)|report-template/.test(path)) return ['reports', 'templates', 'audit']
  if (/\/users?(?:\/|$)|permissions|roles/.test(path)) return ['users', 'context', 'books', 'audit']
  if (/\/books?(?:\/|$)/.test(path)) return ['books', 'context', 'periods', 'subjects', 'audit']
  if (/\/print|template/.test(path)) return ['templates']
  return ['audit']
}

function invalidateAfterMutation(config = {}) {
  const method = String(config.method || 'get').toLowerCase()
  if (method === 'get' || config.__cwqBackground) return
  const path = requestPath(config.url)
  const tags = invalidationFor(path)
  if (!tags.length) return
  const bookId = requestBookId(path, config.params)
  const globalMutation = /\/(?:books|users|permissions|roles)(?:\/|$)/.test(path)
    || /\/auth(?:\/|$)/.test(path)
  queryCache.invalidate({
    tags,
    ...(!globalMutation && bookId ? { scope: 'book', bookId } : {}),
  })
}

export const apiUrl = (path, params = {}) => {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
  ).toString()
  return `${apiBase}${path}${qs ? `?${qs}` : ''}`
}

// ------------------------------------------------------------------
// 「只要最新一趟」的 GET：切期间/切账套时用来掐掉上一趟还在途的请求。
// ------------------------------------------------------------------

/** key → endpoint-level latest request. Different query conditions replace the old one. */
const inflight = new Map()

/**
 * 发一趟 GET，并自动 abort 掉**同一把 key** 的上一趟。
 *
 *  为什么除了代次守卫还要 abort：代次守卫能把「旧结果不该写进页面」挡住，
 *  但那次往返本身已经发生了 —— 带宽、后端连接、一次 JSON 解析都白花。
 *  用户在期间下拉上连点五次，就有四趟是这样的白跑。
 *
 *  key 用来区分「互相抢」的关系：总账翻页不该掐掉余额表那次，所以按
 *  端点分槽，而不是全局只留一趟。
 *
 *  两道防线都得留，缺一不可：
 *   · 只有 abort —— 取消信号到达之前就已经 resolve 的那次，照样会写状态；
 *   · 只有代次 —— 白跑的往返照样发生。
 *  所以配对使用，见 composables/useLatestLoad.js。
 *
 *  @param {string} key 抢占槽位标识（同一个 key 的请求互相取消）
 *  @param {string} url
 *  @param {object} [params]
 *  @returns {Promise<any>} 直接给 data，省得每个调用方再 .then(r => r.data)
 */
export async function getLatest(key, url, params) {
  const path = requestPath(url)
  const cacheKey = requestCacheKey(path, { params })
  const serialized = JSON.stringify(cacheKey)
  const previous = inflight.get(key)
  if (previous && previous.serialized !== serialized) previous.controller.abort()
  if (previous && previous.serialized === serialized) {
    return previous.promise
  }

  const ctl = new AbortController()
  const bookId = requestBookId(path, params)
  const profile = queryProfile(path)
  const policy = CACHE_DEFAULTS[profile]
  const tags = queryTags(path, bookId)
  const scope = bookId ? 'book' : 'user'
  const snapshot = queryCache.peek(cacheKey)
  const request = () => uncachedGet(url, {
    params,
    signal: ctl.signal,
    cache: false,
  }).then((r) => r.data)
  const promise = snapshot
    ? (snapshot.stale
        ? queryCache.fetch(cacheKey, request, { ...policy, profile, tags, scope })
        : Promise.resolve(snapshot.data))
    : queryCache.fetch(cacheKey, request, { ...policy, profile, tags, scope })
  const record = { controller: ctl, serialized, promise }
  inflight.set(key, record)
  promise.finally(() => {
    // 只摘自己这一趟：期间可能已经换人了
    if (inflight.get(key) === record) inflight.delete(key)
  }).catch(() => {})
  if (snapshot?.stale) {
    // The old value is immediately usable; the network refresh is shared above.
    return snapshot.data
  }
  return promise
}

/**
 * 「这个错是我们自己掐掉的」与「这个错是真出事了」必须分得开。
 *
 * axios 取消请求会 reject 一个 CanceledError。调用方若把它当普通错误，
 * 就会给用户弹一条红字，指的还是一个**早就被用户自己放弃**的条件 ——
 * 纯噪音，而且会盖住同期真正需要报的错误。拦截器那边同理。
 */
export function isCanceled(e) {
  if (!e) return false
  return e.name === 'CanceledError' || e.name === 'AbortError'
    || e.code === 'ERR_CANCELED' || (typeof axios.isCancel === 'function' && axios.isCancel(e))
}
