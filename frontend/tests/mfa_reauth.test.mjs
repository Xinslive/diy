/**
 * 高危动作撞到「近期二次验证」闸门之后，前端这条链的行为级用例。
 *
 * **这条链是什么**（审计 §3.2）：
 *
 *   管理员做高危动作 → 403（``detail.code = MFA_REAUTH_REQUIRED``）
 *     → 前端弹出引导 → 发信 → 点链接 → 换到带新鲜 ``mfa_at`` 的新令牌
 *     → **自动重试**那个被拦下的动作 → 管理员不用重新点一遍那个按钮。
 *
 * **为什么它是「行为级」**：这条链跨了三层（拦截器 / store / 后端协议），
 * 每一层单看都能绿，**接错了整条链就是断的**：
 *   · 拦截器不认错误码 → 永远停在 403；
 *   · store 不换令牌 → 重试还是 403（而且会**无限重试**，每轮发一封信）；
 *   · 重试不带新令牌 → 后端再拦一次。
 *
 * 所以这里真的跑 axios 的**拦截器**（用自定义 adapter 造出真实的 403），
 * 断言「第二趟真的发出去了、而且带的是新令牌」。
 *
 * 没有装 vitest —— 与 mfa_login.test.mjs 同一套理由：加测试框架要动
 * package.json 与 lockfile，而那两个文件当前都有未提交的改动。
 * 用 vite 自带的 ssrLoadModule 加载**真实的源码**，零新依赖。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import axios from 'axios'

globalThis.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] ?? null },
  setItem(k, v) { this._m[k] = String(v) },
  removeItem(k) { delete this._m[k] }
}
globalThis.location = { hash: '#/vouchers' }

const vite = await createServer({
  server: { middlewareMode: true }, appType: 'custom', logLevel: 'error'
})
const { http, onMfaReauth, MFA_REAUTH_REQUIRED } =
  await vite.ssrLoadModule('/src/api/client.js')
const { useSessionStore } = await vite.ssrLoadModule('/src/stores/session.js')
const { setActivePinia, createPinia } = await import('pinia')

let pass = 0, fail = 0
const t = async (name, fn) => {
  try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message.split('\n')[0]); fail++ }
}

/**
 * 造一个 axios 形状的 HTTP 错误。
 *
 * **必须带 response**：拦截器判的是 ``error.response?.status`` 与
 * ``error.response?.data?.detail`` —— 没有 response 的错（网络失败、
 * 被 abort）走的是完全不同的分支。
 */
function httpError(config, status, data) {
  return new axios.AxiosError(
    'Request failed with status code ' + status,
    'ERR_BAD_REQUEST', config, null,
    { data, status, statusText: 'x', headers: {}, config }
  )
}

/** 装一个假 adapter：按顺序回第 n 个结果。 */
function stubAdapter(results) {
  const calls = []
  const real = http.defaults.adapter
  http.defaults.adapter = (config) => {
    calls.push({ url: config.url, method: config.method, headers: { ...config.headers } })
    const next = results[Math.min(calls.length - 1, results.length - 1)]
    if (next.status >= 400) return Promise.reject(httpError(config, next.status, next.data))
    return Promise.resolve({ data: next.data, status: next.status, statusText: 'OK', headers: {}, config })
  }
  return { calls, restore() { http.defaults.adapter = real } }
}

let pinia
function freshStore() {
  pinia = createPinia()
  setActivePinia(pinia)
  localStorage._m = {}
  const s = useSessionStore()
  s.$reset()
  return s
}

const MFA_403 = {
  status: 403,
  data: { detail: { code: 'MFA_REAUTH_REQUIRED', message: '该操作需要近期完成邮箱二次验证：请在 5 分钟内通过 POST /api/auth/mfa/reauth 重新验证后再试' } }
}

console.log('== 拦截器：认错误码、重新验证、自动重试 ==')

await t('403(MFA_REAUTH_REQUIRED) → 重新验证 → **自动重试**，第二趟带新令牌', async () => {
  const s = freshStore()
  localStorage.setItem('cwq_token', 'tok-old')
  const stub = stubAdapter([MFA_403, { status: 200, data: { ok: true } }])
  let asked = 0
  onMfaReauth(async () => {
    asked++
    // 重新验证换到新令牌（store 做的事，这儿直接模拟结果）
    s.applyToken({ access_token: 'tok-new' })
    return true
  })
  try {
    const r = await http.get('/system/users')
    assert.equal(r.data.ok, true, '重试没有成功返回')
    assert.equal(asked, 1, '没有走重新验证')
    assert.equal(stub.calls.length, 2, '没有重试（只发了 ' + stub.calls.length + ' 趟）')
    assert.equal(stub.calls[1].headers.Authorization, 'Bearer tok-new',
      '重试用的是旧令牌 —— 后端会再拦一次')
  } finally { stub.restore() }
})

await t('重新验证**没通过** → 不重试，原动作确实没执行', async () => {
  freshStore()
  localStorage.setItem('cwq_token', 'tok-old')
  const stub = stubAdapter([MFA_403])
  onMfaReauth(async () => false)
  try {
    await assert.rejects(() => http.get('/system/users'))
    assert.equal(stub.calls.length, 1, '没通过却重试了 —— 管理员明确拒绝了')
  } finally { stub.restore() }
})

await t('**不会无限重试**：重试之后还是 403，就到此为止', async () => {
  freshStore()
  localStorage.setItem('cwq_token', 'tok-old')
  const stub = stubAdapter([MFA_403])
  let asked = 0
  onMfaReauth(async () => { asked++; return true })
  try {
    await assert.rejects(() => http.get('/system/users'))
    assert.equal(stub.calls.length, 2, '应该只重试一次，实际 ' + stub.calls.length + ' 趟')
    assert.equal(asked, 1, '重新验证被触发了 ' + asked + ' 次 —— 每轮都发一封信')
  } finally { stub.restore() }
})

await t('**别的 403 不走这条流程**（没有错误码的 403 照旧报错）', async () => {
  freshStore()
  const stub = stubAdapter([{ status: 403, data: { detail: '只有系统管理员可以访问' } }])
  let asked = 0
  onMfaReauth(async () => { asked++; return true })
  try {
    await assert.rejects(() => http.get('/system/users'))
    assert.equal(asked, 0, '不是 MFA 闸门也去重新验证了')
    assert.equal(stub.calls.length, 1)
  } finally { stub.restore() }
})

console.log('== store：等待、裁决、换令牌 ==')

await t('requestMfaReauth 在管理员走完之前**不结算**', async () => {
  const s = freshStore()
  let settled = null
  const p = s.requestMfaReauth().then((v) => { settled = v })
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(settled, null, '还没走完就结算了 —— 原动作会被提前重试')
  assert.ok(s.mfaReauth, '没有打开引导的状态')
  s.cancelMfaReauth()
  await p
  assert.equal(settled, false, '取消要裁决成 false（= 原动作没执行）')
})

await t('验证通过 → 裁决成 true，且**令牌真的换了**', async () => {
  const s = freshStore()
  s.token = 'tok-old'
  localStorage.setItem('cwq_token', 'tok-old')
  const p = s.requestMfaReauth()
  s.applyToken({ access_token: 'tok-new', user: { id: 1, username: 'boss', is_admin: true } })
  s.finishMfaReauth()
  assert.equal(await p, true)
  assert.equal(s.token, 'tok-new')
  assert.equal(localStorage.getItem('cwq_token'), 'tok-new')
})

await t('重新验证换令牌时**不动账套列表**（它在一次请求的中途）', async () => {
  const s = freshStore()
  let booksCalls = 0
  const realGet = http.get
  http.get = (url) => {
    if (url === '/books') booksCalls++
    return Promise.resolve({ data: [] })
  }
  try {
    const realPost = http.post
    http.post = (url) => Promise.resolve({
      data: url === '/auth/mfa/poll'
        ? { status: 'VERIFIED', access_token: 'tok-new', user: { id: 1, is_admin: true } }
        : { challenge_id: 7, client_secret: 'sec', expires_in: 600, email_hint: 'o***s@example.com' }
    })
    await s.startMfaReauth()
    assert.equal(s.mfaReauth.challenge.challengeId, 7)
    assert.equal(s.mfaReauth.emailHint, 'o***s@example.com')
    const r = await s.pollMfaReauth()
    assert.equal(r.status, 'VERIFIED')
    assert.equal(s.token, 'tok-new')
    assert.equal(booksCalls, 0, '重新验证时去拉了账套列表 —— 多余，且可能把链路拖失败')
    http.post = realPost
  } finally { http.get = realGet }
})

await t('链接过期（410）→ 判为**这条路走不通**，不是「还没验证」', async () => {
  const s = freshStore()
  const realPost = http.post
  http.post = (url) => {
    if (url === '/auth/mfa/poll') {
      return Promise.reject(httpError({}, 410, { detail: '验证链接已过期，请重新登录' }))
    }
    return Promise.resolve({ data: { challenge_id: 7, client_secret: 'sec', expires_in: 600 } })
  }
  try {
    await s.startMfaReauth()
    const r = await s.pollMfaReauth()
    assert.equal(r.status, 'FAILED')
    assert.equal(s.mfaReauth.phase, 'failed')
    assert.match(s.mfaReauth.error, /过期/)
  } finally { http.post = realPost }
})

await t('**网络抖动不算失败**（下一轮还会再试）', async () => {
  const s = freshStore()
  const realPost = http.post
  http.post = (url) => {
    if (url === '/auth/mfa/poll') return Promise.reject(new Error('Network Error'))
    return Promise.resolve({ data: { challenge_id: 7, client_secret: 'sec', expires_in: 600 } })
  }
  try {
    await s.startMfaReauth()
    const r = await s.pollMfaReauth()
    assert.equal(r.status, 'PENDING')
    assert.equal(s.mfaReauth.phase, 'sent', '网络抖一下就把界面判成失败了')
  } finally { http.post = realPost }
})

await t('登出会**就地结束**正在等的重新验证（裁决 false，不重试）', async () => {
  const s = freshStore()
  s.token = 'tok-old'
  const p = s.requestMfaReauth()
  s.clearLocal()
  assert.equal(await p, false, '登出后那个请求还悬着（或者更糟：被判成通过）')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await vite.close()
process.exit(fail === 0 ? 0 : 1)
