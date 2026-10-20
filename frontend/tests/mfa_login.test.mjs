/**
 * 管理员二次验证（邮箱链接）的登录流程测试。
 *
 * 没装 vitest/jest —— 加测试框架要动 package.json 和 pnpm-lock.yaml，
 * 而这两个文件当前都有未提交的改动（见 session.test.mjs 的同一段说明）。
 * 这里用 vite 自带的 ssrLoadModule 直接加载**真实的 store 源码**，
 * 零新依赖，测的就是线上跑的那份代码。
 *
 * **这个文件钉的是三条边界**：
 *
 * 1. **拿到 mfa_required 绝不写令牌** —— 令牌此刻还不存在，写了就等于
 *    「绕过二次验证也拿到了会话」；
 * 2. **轮询拿到令牌之后才写**，而且走的是与普通登录**同一条**落盘路径；
 * 3. **重发受最短间隔约束** —— 收件箱就是通知通道本身，刷爆它等于把
 *    告警通道一起刷爆。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'

globalThis.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] ?? null },
  setItem(k, v) { this._m[k] = String(v) },
  removeItem(k) { delete this._m[k] }
}
globalThis.location = { hash: '#/login' }

const vite = await createServer({
  server: { middlewareMode: true }, appType: 'custom', logLevel: 'error'
})
const { useSessionStore } = await vite.ssrLoadModule('/src/stores/session.js')
const { http } = await vite.ssrLoadModule('/src/api/client.js')
const { setActivePinia, createPinia } = await import('pinia')

let pass = 0, fail = 0
const t = async (name, fn) => {
  try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message.split('\n')[0]); fail++ }
}

/**
 * 装一套假的 http。
 *
 * routes 形如 { '/auth/login': (body) => ({...}) }，返回的是**回执**。
 * 记下每一次调用，测试据此断言「到底调了哪个接口、带了什么」。
 */
function stubHttp(routes) {
  const calls = []
  const realPost = http.post
  const realGet = http.get
  http.post = (url, body, cfg) => {
    calls.push({ method: 'POST', url, body, cfg })
    const fn = routes[url]
    if (!fn) return Promise.reject(new Error(`未预期的接口：${url}`))
    return Promise.resolve({ data: fn(body, cfg) })
  }
  http.get = (url) => {
    calls.push({ method: 'GET', url })
    return Promise.resolve({ data: [] })
  }
  return { calls, restore() { http.post = realPost; http.get = realGet } }
}

let pinia
function fresh(setup = {}) {
  pinia = createPinia()
  setActivePinia(pinia)
  // **先把 localStorage 清干净**：store 的初始 state 是从 localStorage 读的，
  // 不清的话上一个用例写进去的令牌会跟着进到这个用例里 ——
  // 「这个用例里没有令牌」这件事就不成立了。
  localStorage._m = {}
  const s = useSessionStore()
  s.$reset()
  Object.assign(s, setup)
  return s
}

const MFA_REPLY = {
  mfa_required: true,
  challenge_id: 42,
  client_secret: 'the-client-secret',
  expires_in: 600,
  email_hint: 'o***s@example.com'
}

console.log('== 二次验证：登录阶段 ==')

await t('拿到 mfa_required：**不写任何令牌**', async () => {
  const s = fresh()
  const stub = stubHttp({ '/auth/login': () => MFA_REPLY })
  try {
    const r = await s.login('boss', 'secret')
    assert.equal(r.mfa_required, true)
    assert.equal(s.token, '', '内存里出现了令牌 —— 二次验证没过就发令牌了')
    assert.equal(localStorage.getItem('cwq_token'), null, 'localStorage 里出现了令牌')
    assert.equal(localStorage.getItem('cwq_user'), null)
    assert.ok(s.mfa, '没有记住挑战信息')
    assert.equal(s.mfa.challengeId, 42)
    assert.equal(s.mfa.clientSecret, 'the-client-secret')
    assert.equal(s.mfa.emailHint, 'o***s@example.com')
  } finally { stub.restore() }
})

await t('mfa_required 时**不**去拉账套列表（还没登录）', async () => {
  const s = fresh()
  const stub = stubHttp({ '/auth/login': () => MFA_REPLY })
  try {
    await s.login('boss', 'secret')
    assert.equal(stub.calls.filter((c) => c.url === '/books').length, 0)
  } finally { stub.restore() }
})

await t('普通登录（没有 mfa_required）照旧写令牌并拉账套', async () => {
  const s = fresh()
  const stub = stubHttp({
    '/auth/login': () => ({
      access_token: 'tok-plain',
      token_type: 'bearer',
      expires_at: 999,
      user: { id: 3, username: 'clerk', is_admin: false }
    })
  })
  try {
    const r = await s.login('clerk', 'secret')
    assert.equal(r.mfa_required, false)
    assert.equal(s.token, 'tok-plain')
    assert.equal(localStorage.getItem('cwq_token'), 'tok-plain')
    assert.equal(localStorage.getItem('cwq_user'), '{"id":3,"username":"clerk","is_admin":false}')
    assert.equal(stub.calls.filter((c) => c.url === '/books').length, 1)
  } finally { stub.restore() }
})

console.log('== 二次验证：轮询阶段 ==')

await t('轮询返回 PENDING：不写令牌，也**不算失败**', async () => {
  const s = fresh({ mfa: { challengeId: 42, clientSecret: 'sec', expiresIn: 600, emailHint: 'o***s@example.com', resendAfter: 60, sentAt: Date.now() } })
  const stub = stubHttp({ '/auth/mfa/poll': () => ({ status: 'PENDING' }) })
  try {
    const r = await s.pollMfa()
    assert.equal(r.status, 'PENDING')
    assert.equal(s.token, '')
    assert.equal(localStorage.getItem('cwq_token'), null)
    // 挑战还在（用户可以继续等）
    assert.ok(s.mfa)
  } finally { stub.restore() }
})

await t('轮询拿到令牌：写进本地，**走的是登录那条路**', async () => {
  const s = fresh({ mfa: { challengeId: 42, clientSecret: 'sec', expiresIn: 600, emailHint: 'o***s@example.com', resendAfter: 60, sentAt: Date.now() } })
  const stub = stubHttp({
    '/auth/mfa/poll': () => ({
      status: 'VERIFIED',
      access_token: 'tok-mfa',
      expires_at: 999,
      user: { id: 1, username: 'boss', is_admin: true }
    })
  })
  try {
    const r = await s.pollMfa()
    assert.equal(r.status, 'VERIFIED')
    assert.equal(s.token, 'tok-mfa')
    assert.equal(localStorage.getItem('cwq_token'), 'tok-mfa')
    assert.equal(localStorage.getItem('cwq_user'), '{"id":1,"username":"boss","is_admin":true}')
    // 二次验证过来的令牌也要拉账套 —— 与普通登录同一条路
    assert.equal(stub.calls.filter((c) => c.url === '/books').length, 1)
    // 挑战用掉了
    assert.equal(s.mfa, null)
  } finally { stub.restore() }
})

await t('轮询时 challenge_id 与 client_secret 都要带上', async () => {
  const s = fresh({ mfa: { challengeId: 42, clientSecret: 'sec', expiresIn: 600, emailHint: '', resendAfter: 60, sentAt: Date.now() } })
  const stub = stubHttp({ '/auth/mfa/poll': () => ({ status: 'PENDING' }) })
  try {
    await s.pollMfa()
    const call = stub.calls.find((c) => c.url === '/auth/mfa/poll')
    assert.equal(call.body.challenge_id, 42)
    assert.equal(call.body.client_secret, 'sec')
  } finally { stub.restore() }
})

console.log('== 二次验证：重新发送 ==')

await t('重发受最短间隔约束：间隔没到**根本不发请求**', async () => {
  const s = fresh({ mfa: { challengeId: 42, clientSecret: 'sec', expiresIn: 600, emailHint: '', resendAfter: 60, sentAt: Date.now() } })
  const stub = stubHttp({ '/auth/mfa/resend': () => ({ challenge_id: 43, client_secret: 'sec2', expires_in: 600 }) })
  try {
    const r = await s.resendMfa()
    assert.equal(r.ok, false)
    assert.equal(r.reason, 'too_soon')
    assert.ok(r.wait > 0, '没告诉人还要等几秒')
    assert.equal(stub.calls.filter((c) => c.url === '/auth/mfa/resend').length, 0,
      '间隔没到也发了请求 —— 白白把收件箱刷一遍')
  } finally { stub.restore() }
})

await t('间隔到了才发：并且**用新的挑战覆盖旧的**', async () => {
  const s = fresh({ mfa: { challengeId: 42, clientSecret: 'sec', expiresIn: 600, emailHint: 'o***s@example.com', resendAfter: 60, sentAt: Date.now() - 120000 } })
  const stub = stubHttp({
    '/auth/mfa/resend': () => ({ challenge_id: 43, client_secret: 'sec2', expires_in: 600, email_hint: 'o***s@example.com', resend_after: 60 })
  })
  try {
    const r = await s.resendMfa()
    assert.equal(r.ok, true)
    assert.equal(stub.calls.filter((c) => c.url === '/auth/mfa/resend').length, 1)
    assert.equal(s.mfa.challengeId, 43, '还在用旧挑战 —— 旧的已经被作废了')
    assert.equal(s.mfa.clientSecret, 'sec2')
  } finally { stub.restore() }
})

console.log('== 二次验证：退出 ==')

await t('退出等待：清掉挑战，**不动令牌**（此刻本来也没有）', async () => {
  const s = fresh({ mfa: { challengeId: 42, clientSecret: 'sec', expiresIn: 600, emailHint: '', resendAfter: 60, sentAt: Date.now() } })
  s.cancelMfa()
  assert.equal(s.mfa, null)
  assert.equal(s.token, '')
  assert.equal(localStorage.getItem('cwq_token'), null)
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await vite.close()
process.exit(fail === 0 ? 0 : 1)
