import { http } from './client'
export const login = (username, password) => http.post('/auth/login', { username, password }).then(r => r.data)
export const register = (payload) => http.post('/auth/register', payload).then(r => r.data)
export const me = () => http.get('/auth/me').then(r => r.data)

/**
 * 登出留痕。
 *
 *  后端这个接口**只留痕、不撤销**（令牌是自签 JWT、服务端不存会话，
 *  见 backend/app/api/auth.py 里 logout 的说明）：客户端丢掉 localStorage
 *  里的令牌就是登出的全部语义。不调它，「登出」这条审计记录永远是空的 ——
 *  出事之后第一个该问的「最后一个离开的人是谁」就问不出来。
 *
 *  令牌要**显式带上**：调用方在这之前已经把本地令牌清掉了，
 *  共享 http 的请求拦截器是从 localStorage 取令牌的，那会儿已经取不到了。
 *  这条请求失败（断网 / 服务没起 / 令牌已过期）一律由调用方吞掉 ——
 *  人已经离开了，登出不登得出去不能由这条留痕请求决定。
 */
export const logout = (token) =>
  http.post('/auth/logout', null, token ? { headers: { Authorization: `Bearer ${token}` } } : {})
