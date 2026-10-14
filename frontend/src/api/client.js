import axios from 'axios'
import { ElMessage } from 'element-plus'

export const http = axios.create({ baseURL: '/api', timeout: 30000 })

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

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('cwq_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (r) => r,
  (error) => {
    const status = error.response?.status
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
      if (!location.hash.includes('/login')) location.hash = '#/login'
      return Promise.reject(error)
    }
    const detail = error.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail[0]?.msg : null
    if (msg) ElMessage.error(msg)
    return Promise.reject(error)
  }
)

export const apiUrl = (path, params = {}) => {
  const qs = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
  ).toString()
  return `/api${path}${qs ? `?${qs}` : ''}`
}

// ------------------------------------------------------------------
// 「只要最新一趟」的 GET：切期间/切账套时用来掐掉上一趟还在途的请求。
// ------------------------------------------------------------------

/** key → 那一趟的 AbortController。只登记**此刻还在飞**的那些。 */
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
  inflight.get(key)?.abort()
  const ctl = new AbortController()
  inflight.set(key, ctl)
  try {
    const r = await http.get(url, { params, signal: ctl.signal })
    return r.data
  } finally {
    // 只摘自己这一趟：期间可能已经换人了
    if (inflight.get(key) === ctl) inflight.delete(key)
  }
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
