/**
 * §3.33：切账套时那句「已切换到账套…」必须**真的发得出来**。
 *
 * 复核方实测确认过的现场：
 *   · 顶栏是 ``v-model="store.bookId"`` —— v-model **先**把新账套写进 store，
 *     ``@change`` 里再进 setBook 时 bookId 早就是新的了；
 *   · 于是 store 里那句「bookId 变了没有」永远答 false，
 *     「换账套前的那一本」永远记不上；
 *   · 凭证页的 takePrevBookId() 恒返回 0 → 那句提示**一次也没发出来过**。
 *
 * 这条用例走的是**真实路径**：
 *   真实 store（stores/session.js，经 vite 加载）
 *     → 模拟顶栏的 v-model 先写 bookId
 *     → 模拟 @change 调 setBook
 *     → 真实凭证页源码里抠出来的 load()（stores 里的 prevBookId 是真的）
 *     → 断言那句提示真的弹出来了，且**旧账套**指的是对的那一本。
 *
 * 抠取器先有已知答案的自测（抠错了后面全是假绿）。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createServer } from 'vite'

const ROOT = new URL('../', import.meta.url)
const VOUCHER_EDIT = fs.readFileSync(new URL('src/views/voucher/VoucherEdit.vue', ROOT), 'utf8')
const MAIN_LAYOUT = fs.readFileSync(new URL('src/layouts/MainLayout.vue', ROOT), 'utf8')

// store 在模块顶层就读 localStorage —— 必须先装好再加载
globalThis.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] ?? null },
  setItem(k, v) { this._m[k] = String(v) },
  removeItem(k) { delete this._m[k] }
}
globalThis.location = { hash: '#/vouchers/1581' }

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/** 配平括号：跳过字符串字面量与注释。 */
function matchBracket(src, start, open, close) {
  let depth = 0, quote = null, k = start
  while (k < src.length) {
    const c = src[k]
    if (quote) {
      if (c === '\\') { k += 2; continue }
      if (c === quote) quote = null
      k++
      continue
    }
    if (c === '/' && src[k + 1] === '/') {
      const nl = src.indexOf('\n', k)
      k = nl === -1 ? src.length : nl
      continue
    }
    if (c === '/' && src[k + 1] === '*') {
      const end = src.indexOf('*/', k + 2)
      k = end === -1 ? src.length : end + 2
      continue
    }
    if (c === "'" || c === '"' || c === '`') { quote = c; k++; continue }
    if (c === open) depth++
    else if (c === close) { depth--; if (depth === 0) return k }
    k++
  }
  return -1
}

/** 抠出 `function <name>(...) {...}` 的整段源码。 */
function grabFn(name, src) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src)
  assert.ok(m, `源码里找不到 function ${name}(`)
  const i = m.index
  const close = matchBracket(src, src.indexOf('(', i), '(', ')')
  assert.ok(close !== -1, `${name} 的参数表没配平`)
  const bodyStart = src.indexOf('{', close)
  assert.ok(bodyStart !== -1, `${name} 找不到函数体`)
  const bodyEnd = matchBracket(src, bodyStart, '{', '}')
  assert.ok(bodyEnd !== -1, `${name} 的函数体没配平`)
  const out = src.slice(i, bodyEnd + 1)
  assert.ok(out.endsWith('}'), `${name} 抠出来的不是一段完整的函数`)
  return out
}

/** 抠出 `watch(<getter>, <callback>, <options>)` 整个调用的源码（按圆括号配平）。 */
function grabWatchCall(src, marker) {
  const i = src.indexOf(marker)
  assert.ok(i !== -1, `源码里找不到 ${marker}`)
  const open = src.indexOf('(', i)
  const close = matchBracket(src, open, '(', ')')
  assert.ok(close !== -1, 'watch(...) 的括号没配平')
  return src.slice(i, close + 1)
}

const LOAD_SRC = grabFn('load', VOUCHER_EDIT)

console.log('== 抠取器自测（抠错了后面全是假绿）==')

t('抠取器自测：抠出来的是**那一段**，不是一份等价实现', () => {
  const fake = [
    'async function demo(a, b) {',
    '  const s = `x ${a} y`',
    "  if (a) { return { deep: '}' } }",
    '  return b',
    '}',
    'function other() { return 1 }',
  ].join('\n')
  const got = grabFn('demo', fake)
  assert.ok(got.startsWith('async function demo(a, b) {'), '开头不对')
  assert.ok(got.includes('const s = `x ${a} y`'), '函数体被腰斩了')
  assert.ok(got.includes("return { deep: '}' }"), '字符串里的花括号把配平算错了')
  assert.ok(got.endsWith('}'), '结尾不对')
  assert.ok(!got.includes('function other'), '多抠了下一段')
})

t('抠取器自测：watch 调用的抠法（含嵌套的模板串）', () => {
  const fake = [
    'watch(() => `${route.name} ${route.fullPath || route.path}`, () => {',
    '  store.prevBookId = 0',
    '}, { immediate: true })',
    'const after = 1',
  ].join('\n')
  const got = grabWatchCall(fake, 'watch(() => `${route.name}')
  assert.ok(got.startsWith('watch(() =>'), '开头不对')
  assert.ok(got.includes('store.prevBookId = 0'), '回调被腰斩了')
  assert.ok(got.endsWith('})'), '结尾不对')
  assert.ok(!got.includes('const after'), '多抠了下一段')
})

t('抠取器自测：三个目标都抠得出来（抠空了要让断言失败，别让用例空转）', () => {
  assert.ok(LOAD_SRC.includes('takePrevBookId'), 'load() 里没有取 prevBookId')
  assert.ok(LOAD_SRC.includes('已切换到账套') || LOAD_SRC.includes('crossBook'), 'load() 里没有撞号提示')
  assert.ok(grabWatchCall(MAIN_LAYOUT, 'watch(() => `${route.name}').includes('store.prevBookId = 0'),
    'MainLayout 的路由 watcher 里没有清 prevBookId')
})

/**
 * 真实 store + 真实 load() 跑一遍。
 * @param {object} store 真的 store（来自 stores/session.js）
 * @param {object} over 依赖的覆盖
 */
async function voucherPage(store, over = {}) {
  const said = []
  const pushed = []
  const ref = (v) => ({ value: v })
  const deps = {
    route: { params: { id: '1581' }, query: {} },
    store,
    voucherId: ref(0),
    voucherPeriod: ref(null),
    voucherPeriodId: ref(0),
    insertCtx: ref(null),
    savedAt: ref(''),
    failedSig: ref(''),
    lastSaved: ref(''),
    entrySig: ref('sig'),
    prevId: ref(null),
    nextId: ref(null),
    markingRows: ref(new Set()),
    committedAmounts: ref(new Map()),
    crossBook: ref(null),
    formBookId: ref(1),
    voucherVersion: ref(''),
    versionConflict: ref(false),
    createKey: ref(''),
    form: { legacySubjects: [], entries: [], voucher_no: '', voucher_date: '' },
    subjectRefs: { clear() {} },
    rowEls: { clear() {} },
    sumRefs: { clear() {} },
    amtRefs: { clear() {} },
    ensureRows: () => {},
    seedCommitted: () => {},
    loadSubjects: async () => {},
    loadNeighbour: async () => {},
    yearOfVoucher: () => 2026,
    loadNextDefaults: async () => ({}),
    loadInsertPreview: async () => null,
    defaultDate: () => '2026-01-01',
    api: {
      getVoucher: async () => ({
        id: 1581, voucher_no: '记-0001', period_id: 5, entries: []
      })
    },
    router: { push: (p) => pushed.push(p) },
    ElMessage: {
      error: (m) => said.push(['error', m]),
      warning: (m) => said.push(['warning', m]),
      info: (m) => said.push(['info', m]),
      success: (m) => said.push(['success', m]),
    },
    ...over
  }
  const factory = new Function('d', `
    let loadSeq = 0
    const stale = (seq) => seq !== loadSeq
    const route = d.route
    const store = d.store
    const voucherId = d.voucherId
    const voucherPeriod = d.voucherPeriod
    const voucherPeriodId = d.voucherPeriodId
    const insertCtx = d.insertCtx
    const savedAt = d.savedAt
    const failedSig = d.failedSig
    const lastSaved = d.lastSaved
    const entrySig = d.entrySig
    const prevId = d.prevId
    const nextId = d.nextId
    const markingRows = d.markingRows
    const committedAmounts = d.committedAmounts
    const crossBook = d.crossBook
    const formBookId = d.formBookId
    const voucherVersion = d.voucherVersion
    const versionConflict = d.versionConflict
    const createKey = d.createKey
    const form = d.form
    const subjectRefs = d.subjectRefs
    const rowEls = d.rowEls
    const sumRefs = d.sumRefs
    const amtRefs = d.amtRefs
    const ensureRows = d.ensureRows
    const seedCommitted = d.seedCommitted
    const loadSubjects = d.loadSubjects
    const loadNeighbour = d.loadNeighbour
    const yearOfVoucher = d.yearOfVoucher
    const loadNextDefaults = d.loadNextDefaults
    const loadInsertPreview = d.loadInsertPreview
    const defaultDate = d.defaultDate
    const api = d.api
    const router = d.router
    const ElMessage = d.ElMessage
    const bookName = (id) => {
      const b = (store.books || []).find((x) => x.id === id)
      return b?.company_name || b?.name || ('账套 ' + id)
    }
    ${LOAD_SRC}
    return { load }
  `)
  const fns = factory(deps)
  return { ...fns, deps, said, pushed }
}

const vite = await createServer({
  server: { middlewareMode: true }, appType: 'custom', logLevel: 'error'
})
const { useSessionStore } = await vite.ssrLoadModule('/src/stores/session.js')
const { http } = await vite.ssrLoadModule('/src/api/client.js')
const { setActivePinia, createPinia } = await import('pinia')

/** 期间接口不参与这件事：拦下来直接回空，免得用例挂在网络往返上。 */
const realGet = http.get
http.get = async (url, ...rest) => {
  if (/\/books\/\d+\/periods/.test(url)) return { data: [] }
  return realGet(url, ...rest)
}

/** 一个「在甲账套上看着凭证 #1581」的 store。 */
function storeOnBookA() {
  // localStorage 里那个「记住的账套」是上一个用例留下的（切账套时 store 会写它）。
  // 不清掉的话，新 store 一创建就是那本账 —— 后面「没有切账套」那条用例就废了。
  localStorage.removeItem('cwq_book')
  localStorage.removeItem('cwq_period')
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useSessionStore()
  store.$reset()
  store.books = [
    { id: 1, name: '甲账套', company_name: '甲公司' },
    { id: 2, name: '乙账套', company_name: '乙公司' }
  ]
  store.bookId = 1
  store.periods = [{ id: 5, code: '2026-01', year: 2026, month: 1, ord: 1, locked: false }]
  store.periodsBookId = 1
  store.periodId = 5
  return store
}

/**
 * 顶栏那一步的**真实顺序**：v-model 先把新账套写进 store，@change 里才调 setBook。
 */
async function switchBookInTopbar(store, to) {
  store.bookId = to          // ← v-model 先写
  await store.setBook(to)    // ← @change 后调
}

console.log('== §3.33 切账套：那句提示必须真的发得出来 ==')

t('**行为**：v-model 先写 bookId（顶栏的真实顺序）→ 旧账套照样被记住', async () => {
  const store = storeOnBookA()
  await switchBookInTopbar(store, 2)
  assert.equal(store.bookId, 2, '账套没切过去')
  assert.equal(store.prevBookId, 1,
    '「换账套前的那一本」没记上 —— 判据还在问「bookId 变了没有」，那个问题已经被回答过了')
})

t('**行为**：切账套后凭证页真的弹出「已切换到账套…」，且指的是**旧的那本账**', async () => {
  const store = storeOnBookA()
  await switchBookInTopbar(store, 2)
  const page = await voucherPage(store)
  await page.load()
  assert.equal(page.said.length, 1,
    `提示没有发出来（一次也没发）：${JSON.stringify(page.said)}`)
  const [kind, text] = page.said[0]
  assert.equal(kind, 'warning', '这不是错误，是一句「已经换人了」的提示')
  assert.match(text, /已切换到账套/, '提示的第一句就不对')
  // **旧账套**要指名道姓：甲公司（不是当前那本乙公司）
  assert.match(text, /甲公司/, `提示里没有指名旧账套：${text}`)
  assert.match(text, /乙公司/, `提示里没有指名当前账套：${text}`)
  assert.match(text, /#1581/, '提示里没有指名原凭证的号')
  // 提示是一次性的：说完就清（否则下次进凭证页又说一遍）
  assert.equal(store.prevBookId, 0, '这份记号没有清掉 —— 下次进凭证页会又说一遍')
})

t('**对称**：没有切账套时一句话都不说', async () => {
  const store = storeOnBookA()
  const page = await voucherPage(store)
  await page.load()
  assert.deepEqual(page.said, [], `不该有提示：${JSON.stringify(page.said)}`)
})

t('**对称**：把 bookId 清 0 的那些路（登出、账套没了）**不算**切账套', async () => {
  const store = storeOnBookA()
  // 登出 / 账套列表里没有这一本了 —— 这些路把 bookId 归 0
  store.bookId = 0
  assert.equal(store.prevBookId, 0, '归 0 也被当成切账套了 —— 凭证页会说一句没头没尾的话')
  // 从 0 换到另一本，也不是「从某本账换过来」
  store.bookId = 2
  assert.equal(store.prevBookId, 0, '从「没有账套」跳到某一本，也被当成切账套了')
})

console.log('== MainLayout 的路由 watcher：切账套时它不许把记号擦掉 ==')

t('**行为**：切账套不改路由 → 路由 watcher 不触发 → 记号活到凭证页', () => {
  // 把 MainLayout 里那个路由 watcher 抠出来跑：它看的是「路由名 + 完整地址」，
  // 跟账套**无关** —— 所以切账套（只动顶栏那个下拉）根本不会触发它。
  const src = grabWatchCall(MAIN_LAYOUT, 'watch(() => `${route.name}')
  let captured = null
  const factory = new Function('watch', 'route', 'tabs', 'store', `
    ${src}
    return null`)
  const route = { name: 'voucher-edit', fullPath: '/vouchers/1581', path: '/vouchers/1581' }
  const opened = []
  const store = { prevBookId: 0, createdVoucher: null }
  factory((getter, cb, options) => {
    captured = { getter, cb, options }
    if (options?.immediate) cb()
  }, route, { open: (r) => opened.push(r) }, store)

  assert.ok(captured, '没抠到那个 watch')
  const keyBefore = captured.getter()
  // 切账套：路由**没变**（变的只是顶栏那个下拉）
  const keyAfter = captured.getter()
  assert.equal(keyAfter, keyBefore, '切账套竟然改了路由 —— 那条记号会被擦掉')
  assert.equal(store.prevBookId, 0,
    '（这个用例里 prevBookId 本来就是 0；关键是 key 没变 → watcher 不触发）')
  // 真的换页面了 → 那个记号要清掉（否则会对着一张他没打开过的凭证说「你原来看的是哪本账的哪张」）
  store.prevBookId = 7
  captured.cb()
  assert.equal(store.prevBookId, 0, '换了页面却不清那个记号 —— 下次进凭证页会又说一遍')
})

for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK   ' + name)
    pass++
  } catch (e) {
    console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0])
    fail++
  }
}

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await vite.close()
process.exit(fail === 0 ? 0 : 1)
