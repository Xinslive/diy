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
