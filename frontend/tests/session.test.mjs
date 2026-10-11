/**
 * session store 与 401 处理的回归测试（审计 P3-05 + P3-06）。
 *
 * 没装 vitest/jest —— 加测试框架要动 package.json 和 pnpm-lock.yaml，
 * 而这两个文件当前都有未提交的改动，混在一起很难分辨是谁改的。
 * 这里用 vite 自带的 ssrLoadModule 直接加载**真实的 store 源码**，
 * 零新依赖，测的就是线上跑的那份代码（不是复制出来的逻辑）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'

// store 在模块顶层就读 localStorage，必须先装好再加载
globalThis.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] ?? null },
  setItem(k, v) { this._m[k] = String(v) },
  removeItem(k) { delete this._m[k] }
}
globalThis.location = { hash: '#/voucher' }

const vite = await createServer({
  server: { middlewareMode: true }, appType: 'custom', logLevel: 'error'
})
const { useSessionStore } = await vite.ssrLoadModule('/src/stores/session.js')
const { http, onUnauthorized } = await vite.ssrLoadModule('/src/api/client.js')
const { setActivePinia, createPinia } = await import('pinia')

let pass = 0, fail = 0
const t = async (name, fn) => {
  try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message.split('\n')[0]); fail++ }
}

let pinia
function fresh(setup = {}) {
  pinia = createPinia()
  setActivePinia(pinia)
  const s = useSessionStore()
  s.$reset()
  Object.assign(s, setup)
  // 复现 main.js 里的装配
  onUnauthorized(() => useSessionStore(pinia).logout())
  return s
}

console.log('== P3-06 canWrite 必须 fail-closed ==')

await t('没有当前账套时不能写（加载中/加载失败的中间态）', () => {
  const s = fresh({ books: [], bookId: 0 })
  assert.equal(s.canWrite, false, 'currentBook 为 null 时 canWrite 居然是 true')
})

await t('账套列表还没加载完（books 空但 bookId 有值）也不能写', () => {
  const s = fresh({ books: [], bookId: 7 })
  assert.equal(s.canWrite, false)
})

await t('VIEWER 不能写', () => {
  const s = fresh({ books: [{ id: 7, role: 'VIEWER' }], bookId: 7 })
  assert.equal(s.canWrite, false)
})

await t('ACCOUNTANT 能写', () => {
  const s = fresh({ books: [{ id: 7, role: 'ACCOUNTANT' }], bookId: 7 })
  assert.equal(s.canWrite, true)
})

await t('OWNER 能写', () => {
  const s = fresh({ books: [{ id: 7, role: 'OWNER' }], bookId: 7 })
  assert.equal(s.canWrite, true)
})

await t('未知角色也先当不能写（白名单而非黑名单）', () => {
  const s = fresh({ books: [{ id: 7, role: 'SUPERUSER' }], bookId: 7 })
  assert.equal(s.canWrite, false, '未知角色被放行了 —— 白名单只认 OWNER/ACCOUNTANT')
})

console.log('== P3-05 401 必须同步清掉内存态 ==')

/** 造一个必定返回 401 的请求，走完整的响应拦截器。 */
async function fire401() {
  const handler = http.interceptors.response.handlers[0].rejected
  const err = { response: { status: 401 }, config: {} }
  try { await handler(err) } catch { /* 预期 reject */ }
}

await t('401 后 token / user / books 都被清空', async () => {
  const s = fresh({
    token: 'stale-token',
    user: { id: 1, username: 'someone', is_admin: true },
    books: [{ id: 7, role: 'OWNER' }],
    bookId: 7,
    periods: [{ id: 3, year: 2026 }],
    periodId: 3
  })
  localStorage.setItem('cwq_token', 'stale-token')

  await fire401()

  assert.equal(s.token, '', '内存里的 token 还在 —— isAuthed 仍为真，UI 会继续按已登录渲染')
  assert.equal(s.user, null, '内存里的 user 还在')
  assert.equal(s.books.length, 0, '账套列表还在')
  assert.equal(s.isAuthed, false, '401 后仍认为已登录')
})

await t('401 后 canWrite 不再因为残留 state 而为真', async () => {
  const s = fresh({
    token: 'stale', books: [{ id: 7, role: 'OWNER' }], bookId: 7
  })
  await fire401()
  assert.equal(s.canWrite, false)
})

await t('401 后 localStorage 也清干净', async () => {
  fresh({ token: 'stale' })
  localStorage.setItem('cwq_token', 'stale')
  localStorage.setItem('cwq_user', '{"id":1}')
  localStorage.setItem('cwq_book', '7')

  await fire401()

  assert.equal(localStorage.getItem('cwq_token'), null)
  assert.equal(localStorage.getItem('cwq_user'), null)
  assert.equal(localStorage.getItem('cwq_book'), null)
})

await t('401 后跳回登录页', async () => {
  fresh({ token: 'stale' })
  globalThis.location.hash = '#/voucher'
  await fire401()
  assert.equal(globalThis.location.hash, '#/login')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await vite.close()
process.exit(fail === 0 ? 0 : 1)
