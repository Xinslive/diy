/**
 * 切账套时**旧账套的期间必须被清干净**（src/stores/session.js，**线上那份源码**）。
 *
 * 用户报的现象：在凭证列表（#/vouchers）和总账（#/general-ledger）切账套，
 * 弹「起始期间不存在」/「会计期间不存在」。
 *
 * 根因不在后端，在 store：顶栏那行是 ``v-model="store.bookId"``，
 * 而 v-model 会**先**把新账套写进 store，@change 里再调 setBook 时
 * bookId 早就是新的了 —— 于是 setBook 里那句「bookId 变了吗」永远答 false，
 * 旧账套的 periods / periodId 就一直没被清掉。
 *
 * 后面这一串全跟着错：
 *   切账套 → 新账套的期间还在路上 → 页面这时**就该**拿不到旧期间，
 *   可旧期间还在手上 → 页面拿着**上一个账套**的 period id 去查**新账套**
 *     → 后端发现那个期间不属于本账套 → 404「会计期间不存在」。
 *
 * 所以断言打在**切完之后、新期间到之前的那一瞬**（页面正是在那一刻发请求），
 * 不是打在最终状态上 —— 最终状态两份实现都一样，测不出差别。
 *
 * 修法：判据从「bookId 变没变」换成「**手上这批期间是谁的**」（periodsBookId）。
 * 这个判据跟「谁来写 bookId」无关，v-model 先写、直接调 setBook 都判得对。
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
globalThis.location = { hash: '#/vouchers' }

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

/** 造一个账套的期间（12 个月，ord 从 1 起）。 */
function periodsOf(bookId, startId) {
  return Array.from({ length: 12 }, (_, i) => ({
    id: startId + i, code: `2026-${String(i + 1).padStart(2, '0')}`,
    year: 2026, month: i + 1, ord: i + 1, locked: false
  }))
}

/**
 * 拦下期间接口，并**卡住**它 —— 「请求还在路上」正是这个 bug 的现场。
 *
 * 返回 { release(bookId), restore() }：release 之后那一趟才回来。
 */
function stubPeriods() {
  const real = http.get
  const gates = {}
  http.get = (url, ...rest) => {
    const m = /\/books\/(\d+)\/periods/.exec(url)
    if (m) {
      const id = Number(m[1])
      gates[id] = gates[id] || []
      return new Promise((res) => {
        gates[id].push(() => res({ data: periodsOf(id, id === 6 ? 73 : 1) }))
      })
    }
    return real(url, ...rest)
  }
  return {
    release(bookId) { (gates[bookId] || []).splice(0).forEach((fn) => fn()) },
    restore() { http.get = real }
  }
}

let pinia
function fresh(setup = {}) {
  pinia = createPinia()
  setActivePinia(pinia)
  const s = useSessionStore()
  s.$reset()
  Object.assign(s, setup)
  return s
}

/** 旧账套（6，启明物业）在 store 里的样子：期间 73~84，当前停在 73。 */
function oldBookState() {
  return { bookId: 6, periodsBookId: 6, periods: periodsOf(6, 73), periodId: 73 }
}

console.log('== 切账套：旧期间必须在新期间到之前就清干净 ==')

await t('v-model 先写了 bookId 的情况下，旧期间照样被清掉（本 bug 的核心场景）', async () => {
  const stub = stubPeriods()
  try {
    const s = fresh(oldBookState())
    // 顶栏下拉是 v-model="store.bookId"：新账套**先**被写进 store，
    // @change 里再调 setBook —— 进来时 bookId 已经是新的了。
    s.bookId = 1
    const p = s.setBook(1)          // 不 await：新账套的期间还在路上
    // 此刻页面正是在这一刻发请求 —— 手上必须已经没有旧账套的期间了
    assert.equal(s.periods.length, 0, '旧账套的期间还在手上 —— 页面会拿着它去查新账套')
    assert.equal(s.periodId, 0, '旧账套的 periodId 还挂在 store 上')
    assert.equal(s.periodsReady, false, '期间都没到位就说 ready 了')
    stub.release(1)
    await p
    assert.equal(s.periodsBookId, 1)
    assert.equal(s.periodId, 1, '新账套的期间没落到位')
  } finally { stub.restore() }
})

await t('直接调 setBook（不经 v-model）也一样清干净', async () => {
  const stub = stubPeriods()
  try {
    const s = fresh(oldBookState())
    const p = s.setBook(1)
    assert.equal(s.periods.length, 0)
    assert.equal(s.periodId, 0)
    stub.release(1)
    await p
    assert.equal(s.periodId, 1)
  } finally { stub.restore() }
})

await t('切账套时 localStorage 里那个旧期间被删掉（刷新后不会落回旧账）', async () => {
  const stub = stubPeriods()
  try {
    localStorage.setItem('cwq_period', '73')
    const s = fresh(oldBookState())
    const p = s.setBook(1)
    assert.equal(localStorage.getItem('cwq_period'), null, '存的还是旧账套的期间')
    stub.release(1)
    await p
    assert.equal(localStorage.getItem('cwq_period'), '1', '新账套的期间没存上')
  } finally { stub.restore() }
})

console.log('== 请求在路上又切账套：旧的那份回来不许覆盖新的 ==')

await t('旧账套的响应晚到，不覆盖新账套的期间', async () => {
  const stub = stubPeriods()
  try {
    const s = fresh(oldBookState())
    const first = s.setBook(1)          // 账套 1 的请求在路上
    s.bookId = 2
    const second = s.setBook(2)         // 又切到账套 2
    stub.release(1)                     // 账套 1 的响应先回来
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    assert.equal(s.periods.length, 0, '旧账套的期间盖上来了')
    stub.release(2)
    await first; await second
    assert.equal(s.periodsBookId, 2)
    assert.equal(s.periods[0]?.id, 1, '账套 2 的期间没到位')
    assert.equal(s.periodId, 1)
  } finally { stub.restore() }
})

console.log('== 记住的账套没了，旧期间也要清 ==')

await t('localStorage 里那个账套已经被删：旧期间一并清掉', async () => {
  const stub = stubPeriods()
  const realGet = http.get
  http.get = (url, ...rest) => {
    if (url === '/books') return Promise.resolve({ data: [{ id: 1, name: '别的账', role: 'OWNER' }] })
    return realGet(url, ...rest)
  }
  try {
    const s = fresh(oldBookState())
    const p = s.loadBooks()
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
    // 账套列表已经回来、新的期间还没到 —— 旧期间必须已经清掉
    assert.equal(s.periods.length, 0, '已删账套的期间还在手上')
    assert.equal(s.periodId, 0)
    stub.release(1)
    await p
    assert.equal(s.bookId, 1, '应该落到唯一剩下的那个账套')
    assert.equal(s.periodsBookId, 1, 'periodsBookId 没跟上')
    assert.equal(s.periodId, 1, 'periodId 还指着已删账套的期间')
  } finally { http.get = realGet; stub.restore() }
})

await t('登出：期间连同归属一起清零', async () => {
  const s = fresh(oldBookState())
  const realPost = http.post
  http.post = () => Promise.resolve({ data: { ok: true } })
  try {
    await s.logout()
    assert.equal(s.periodsBookId, 0)
    assert.equal(s.periodId, 0)
    assert.equal(s.periodsReady, false)
  } finally { http.post = realPost }
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await vite.close()
process.exit(fail === 0 ? 0 : 1)
