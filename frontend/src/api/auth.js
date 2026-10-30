import { http } from './client'
export const login = (username, password) => http.post('/auth/login', { username, password }).then(r => r.data)
export const register = (payload) => http.post('/auth/register', payload).then(r => r.data)
export const me = () => http.get('/auth/me').then(r => r.data)
export const bootstrap = (bookId, periodId) => http.get('/bootstrap', {
  cache: false,
  params: {
    book_id: bookId || undefined,
    period_id: periodId || undefined,
  }
}).then(r => r.data)

/**
 * 改**自己的**姓名。
 *
 * 请求体里**没有「改谁」这个字段** —— 目标永远是令牌对应的那个人，
 * 所以不存在「改到别人头上」的可能，也不需要再问一句「你是不是管理员」。
 *
 * 顺带说明：接口**不返回**新的令牌。改姓名不动密码，因此**不必**重新登录，
 * 现有的会话照旧有效。
 */
export const updateProfile = (payload) =>
  http.patch('/auth/me', payload).then(r => r.data)

/**
 * 改**自己的**密码。
 *
 * **当前密码必填**（``current_password``）—— 会话令牌是可以被拿走的
 * （XSS、共享电脑、令牌落在别人手里），没有这道关，拿到令牌的人
 * 顺手就把账号的密码改了。
 *
 * **成功之后必须重新登录**：改密码会撤销这个账号**所有**在用令牌
 * （``token_version`` 加一，审计 P2-01），攻击者手里的旧令牌当场作废。
 * 回执里的 ``reauth_required`` 就是这句话的机器可读版本，调用方据此
 * 清本地令牌并回登录页 —— 而不是等下一个请求撞 401 才被动发现。
 */
export const changePassword = (payload) =>
  http.post('/auth/me/password', payload).then(r => r.data)

/**
 * 登录页用的匿名配置：自助注册开没开。
 *
 * 必须在**还没有令牌**时就能读，所以它不能挂在带鉴权的接口下 —— 登录页正是
 * 最没有令牌的那一刻。回执里只有一个布尔，别往里加任何别的字段：这个接口对
 * 匿名开放。
 *
 * **失败要按「关着」处理**（调用方负责）：接口挂了就先不显示注册入口，好过
 * 先亮出来再撞一个 403。
 */
export const publicConfig = () => http.get('/auth/config').then(r => r.data)

/**
 * 登出：**吊销这一枚令牌**，并留痕。
 *
 *  后端这个接口现在会**真的吊销**：登录令牌不设时间上限（载荷里没有
 *  `exp`），所以「登出」不能只是客户端把手里的令牌丢掉 —— 那枚令牌会一直
 *  有效下去。服务端把它的 jti 记进吊销名单，下一次带它来的请求立刻 401
 *  （见 backend/app/api/auth.py 里 logout 的说明）。
 *
 *  **只吊销这一枚**，不是这个账号的全部会话：同一账号在另一台设备上的
 *  令牌照常能用（账号级撤销是「退出全部设备」的语义，不该由点一下退出触发）。
 *
 *  令牌要**显式带上**：调用方在这之前已经把本地令牌清掉了，
 *  共享 http 的请求拦截器是从 localStorage 取令牌的，那会儿已经取不到了。
 *  这条请求失败（断网 / 服务没起）一律由调用方吞掉 ——
 *  人已经离开了，登出不登得出去不能由这条请求决定。
 */
export const logout = (token) =>
  http.post('/auth/logout', null, token ? { headers: { Authorization: `Bearer ${token}` } } : {})

/**
 * 管理员二次验证：轮询挑战状态。
 *
 *  拿到 ``{status:"PENDING"}`` = 还没点邮件里的链接；拿到 ``access_token``
 *  = 验证通过，**这一个令牌就是正式的会话令牌**（与 /auth/login 同构）。
 *
 *  **为什么轮询而不是「点链接直接换令牌」**：邮件链接会被网关扫描、被手机
 *  锁屏预览、被 IT 留档。让它直接等于会话令牌，等于「谁截获了那封信谁就是
 *  管理员」。点链接只把挑战标成已验证，**令牌由发起登录的这台机器来换** ——
 *  这台机器手里有 ``client_secret``，别处没有。
 *
 *  @param {number} challengeId
 *  @param {string} clientSecret
 */
export const mfaPoll = (challengeId, clientSecret) =>
  http.post('/auth/mfa/poll', {
    challenge_id: challengeId,
    client_secret: clientSecret
  }).then(r => r.data)

/**
 * 重新发送验证邮件。
 *
 *  服务端按最短间隔节流（默认 60 秒），超了回 429 并带 ``Retry-After`` ——
 *  收件箱**就是通知通道本身**，把它刷爆等于把告警通道一起刷爆了。
 *
 *  **重发会换一个新的挑战**（新的 challenge_id 与 client_secret），所以调用
 *  方**必须**用返回值覆盖自己手里那一对 —— 旧的那对已经作废。
 */
export const mfaResend = (challengeId, clientSecret) =>
  http.post('/auth/mfa/resend', {
    challenge_id: challengeId,
    client_secret: clientSecret
  }).then(r => r.data)

/**
 * 已登录的管理员请求「重新验证」（高危动作要求近期验证时用）。
 *
 *  走完这一套之后，用**同一个 poll 接口**换到的新令牌里带着新鲜的验证时刻，
 *  拿它去重试那个高危动作。
 */
export const mfaReauth = () => http.post('/auth/mfa/reauth', {}).then(r => r.data)

