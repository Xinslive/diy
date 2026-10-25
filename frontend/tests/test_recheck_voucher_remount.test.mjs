/**
 * N5：跨重挂不许「再建一张凭证」。
 *
 * 复核方实测确认的现场：
 *   · 新建凭证落库后，VoucherEdit 用 ``history.replaceState`` 把地址栏换成
 *     ``/vouchers/{id}``，**但不同步 router** —— router 内部还停在
 *     ``/vouchers/new``（走 router 会把组件整个重挂，正在填的表单、
 *     光标、绿行全没了，这是当时刻意做的取舍）；
 *   · 于是组件一重挂（切期间 / 切账套 / 点刷新），``route.params.id``
 *     仍是 undefined → ``myId = 0`` → ``voucherId = 0`` → ``wasNew``；
 *   · 幂等键是**组件内 state**，重挂即丢 → 下一趟自动保存**又 POST 一张**，
 *     库里多出一张几乎一样的。
 *
 * 修法：「这张凭证已经建出来了」这个事实记在 **store** 里（跨得过重挂），
 * 重挂后凭证页把它捡回来 —— 于是它认得「这不是新凭证，是刚才建的那张」。
 *
 * 这里跑的是**真实路径**：真实 store（经 vite 加载）+ 真实组件源码里抠出来的
 * load() / save()。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { createServer } from 'vite'
import { computed, reactive, ref } from 'vue'
import { moneyText } from '../src/utils/format.js'
import { centsToText, moneyCents } from '../src/utils/amountGrid.js'

const ROOT = new URL('../', import.meta.url)
const read = (p) => fs.readFileSync(new URL(p, ROOT), 'utf8')
const VOUCHER_EDIT = read('src/views/voucher/VoucherEdit.vue')
const MAIN_LAYOUT = read('src/layouts/MainLayout.vue')

// store 在模块顶层就读 localStorage —— 必须先装好再加载
globalThis.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] ?? null },
  setItem(k, v) { this._m[k] = String(v) },
  removeItem(k) { delete this._m[k] }
}
globalThis.location = { hash: '#/vouchers/new' }

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/** 配平括号：跳过字符串字面量、注释与正则字面量。 */
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

/** 抠出 `watch(...)` 整个调用。 */
function grabWatchCall(src, marker) {
  const i = src.indexOf(marker)
  assert.ok(i !== -1, `源码里找不到 ${marker}`)
  const open = src.indexOf('(', i)
  const close = matchBracket(src, open, '(', ')')
  assert.ok(close !== -1, 'watch(...) 的括号没配平')
  return src.slice(i, close + 1)
}

/** 金额那一段（centsOf / centsText / payableEntries / balanced …）。 */
function amountBlock() {
  const a = VOUCHER_EDIT.indexOf('// ---- 金额：一律整数分')
  const b = VOUCHER_EDIT.indexOf('// ---- 金额段结束')
  assert.ok(a !== -1 && b > a, '抠不出金额那一段 —— 标记挪位置了？')
  const block = VOUCHER_EDIT.slice(a, b)
  for (const c of ['payableEntries', 'rowsMissingSubject', 'balanced']) {
    assert.ok(block.includes(`const ${c} =`), `抠出来的段里没有 ${c}`)
  }
  return block
}
const BLOCK = amountBlock()

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

t('抠取器自测：四个目标都抠得出来（抠空了要让断言失败，别让用例空转）', () => {
  assert.ok(grabFn('load', VOUCHER_EDIT).includes('createdVoucher'),
    'load() 里没有把「已经建出来了」那个事实捡回来')
  assert.ok(grabFn('save', VOUCHER_EDIT).includes('createdVoucher'),
    'save() 里没有把「已经建出来了」那个事实记下来')
  assert.ok(grabWatchCall(MAIN_LAYOUT, 'watch(() => `${route.name}').includes('store.createdVoucher = null'),
    'MainLayout 的路由 watcher 里没有清那个记号')
})

/**
 * 搭一套凭证页（load() + save()）：金额相关的判据全部来自**真实实现**。
 * @param {object} store 真的 store
 */
function page(store, over = {}) {
  const said = []
  const requests = []
  const form = reactive({
    voucher_no: '', voucher_date: '2026-01-01', status: 'UNPOSTED',
    attachments: 0, remark: '',
    entries: [
      { subject_id: 1, direction: 'DEBIT', amount: '100.00', summary: '' },
      { subject_id: 2, direction: 'CREDIT', amount: '100.00', summary: '' }
    ],
    legacySubjects: []
  })
  const amount = new Function('computed', 'moneyText', 'form', 'moneyCents', 'centsToText', `${BLOCK}
    return { payableEntries, rowsMissingSubject, totals, total, diffCents, balanced,
             centsOf, centsText, voucherTotals }`)(computed, moneyText, form, moneyCents, centsToText)

  const deps = {
    route: { params: { id: '' }, query: {} },
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
    formBookId: ref(0),
    voucherVersion: ref(''),
    versionConflict: ref(false),
    createKey: ref(''),
    form,
    subjectRefs: { clear() {} },
    rowEls: { clear() {} },
    sumRefs: { clear() {} },
    amtRefs: { clear() {} },
    ensureRows: () => {},
    seedCommitted: () => {},
    loadSubjects: async () => {},
    loadNeighbour: async () => {},
    yearOfVoucher: () => 2026,
    loadNextDefaults: async () => ({ voucher_no: '记-0009', voucher_date: '2026-01-01' }),
    loadInsertPreview: async () => null,
    defaultDate: () => '2026-01-01',
    // save() 用的那几样
    readonly: { value: false },
    posted: { value: false },
    locked: { value: false },
    balanced: amount.balanced,
    saving: { value: false },
    newNonLeafRows: { value: [] },
    payableEntries: amount.payableEntries,
    rowsMissingSubject: amount.rowsMissingSubject,
    centsOf: amount.centsOf,
    centsText: amount.centsText,
    rowProblemText: (r) => `非末级科目 ${r.subject_id}`,
    newIdempotencyKey: () => 'idem-key-1',
    api: {
      getVoucher: async (bookId, id) => {
        requests.push(['get', bookId, id])
        return { id, voucher_no: '记-0007', period_id: 5, entries: [] }
      },
      createVoucher: async (bookId, payload, key) => {
        requests.push(['create', bookId, payload, key])
        return { id: 7, period_id: 5, version: 'v1' }
      },
      updateVoucher: async (bookId, id, payload) => {
        requests.push(['update', bookId, id, payload])
        return { id, period_id: 5, version: 'v1' }
      }
    },
    history: { state: null, replaceState() {} },
    tabs: { open: () => {} },
    router: { push: () => {} },
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
    let savingPromise = null
    let autoSaveTimer = null
    const AUTO_SAVE_DELAY = 700
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
    const readonly = d.readonly
    const posted = d.posted
    const locked = d.locked
    const balanced = d.balanced
    const saving = d.saving
    const newNonLeafRows = d.newNonLeafRows
    const payableEntries = d.payableEntries
    const rowsMissingSubject = d.rowsMissingSubject
    const centsOf = d.centsOf
    const centsText = d.centsText
    const rowProblemText = d.rowProblemText
    const newIdempotencyKey = d.newIdempotencyKey
    const history = d.history
    const tabs = d.tabs
    const bookName = (id) => {
      const b = (store.books || []).find((x) => x.id === id)
      return b?.company_name || b?.name || ('账套 ' + id)
    }
    ${grabFn('load', VOUCHER_EDIT)}
    ${grabFn('save', VOUCHER_EDIT)}
    return { load, save }`)
  const fns = factory(deps)
  return { ...fns, deps, said, requests }
}

const vite = await createServer({
  server: { middlewareMode: true }, appType: 'custom', logLevel: 'error'
})
const { useSessionStore } = await vite.ssrLoadModule('/src/stores/session.js')
const { setActivePinia, createPinia } = await import('pinia')

/** 一个干净的 store（真的一份）。 */
function freshStore() {
  localStorage.removeItem('cwq_book')
  localStorage.removeItem('cwq_period')
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = useSessionStore()
  store.$reset()
  store.books = [{ id: 1, name: '甲账套', company_name: '甲公司' }]
  store.bookId = 1
  store.periods = [{ id: 5, code: '2026-01', year: 2026, month: 1, ord: 1, locked: false }]
  store.periodsBookId = 1
  store.periodId = 5
  return store
}

console.log('== N5 重挂之后不许「再建一张凭证」 ==')

t('**行为**：新建落库后 → 「已经建出来了」这个事实记在 store 里（跨得过重挂）', async () => {
  const store = freshStore()
  const p = page(store)
  await p.deps.formBookId.value
  p.deps.formBookId.value = 1
  const ok = await p.save()
  assert.equal(ok, true, '新凭证没存成')
  assert.deepEqual(p.requests.map((r) => r[0]), ['create'], '走的不是新建那条路')
  assert.ok(store.createdVoucher, '「已经建出来了」这个事实没记下来 —— 重挂就丢')
  assert.equal(store.createdVoucher.id, 7, '记下来的 id 不对')
  assert.equal(store.createdVoucher.bookId, 1, '记下来的账套不对')
  assert.ok(store.createdVoucher.createKey, '幂等键没记下来')
})

t('**行为**：重挂之后（router 还停在 /vouchers/new）→ 认出「这就是刚才建的那张」', async () => {
  const store = freshStore()
  // 第一趟：新建落库
  const first = page(store)
  first.deps.formBookId.value = 1
  await first.save()
  // **重挂**：新组件实例，router 内部仍然是 /vouchers/new（route.params.id 空）
  const second = page(store)
  await second.load()
  assert.deepEqual(second.requests.map((r) => [r[0], r[2]]), [['get', 7]],
    `重挂后没有去取刚才建的那张（#7）：${JSON.stringify(second.requests)}`)
  assert.equal(second.deps.voucherId.value, 7, '重挂后不认得这张凭证')
})

t('**行为**：重挂之后的那一趟自动保存走的是 PUT（改那张），不是再 POST 一张', async () => {
  const store = freshStore()
  const first = page(store)
  first.deps.formBookId.value = 1
  await first.save()
  const second = page(store)
  await second.load()
  second.requests.length = 0
  const ok = await second.save()
  assert.equal(ok, true, '重挂后那一趟没存成')
  assert.deepEqual(second.requests.map((r) => r[0]), ['update'],
    `重挂后又 POST 了一张：${JSON.stringify(second.requests.map((r) => r[0]))}`)
  assert.equal(second.requests[0][2], 7, '改的不是刚才建的那张')
})

t('**对称**：那个记号属于**别的账套** → 不捡（换账套后不能拿旧账套的 id 去查）', async () => {
  const store = freshStore()
  store.createdVoucher = { bookId: 99, id: 7, createKey: 'k' }
  const p = page(store)
  await p.load()
  assert.deepEqual(p.requests.filter((r) => r[0] === 'get'), [],
    '拿别的账套的 id 去查了')
  assert.equal(p.deps.voucherId.value, 0, '把别的账套那张当成本账套的了')
})

t('**对称**：响应在路上丢了（还没拿到 id）→ 幂等键跟着活下来，下一趟还是同一把键', async () => {
  const store = freshStore()
  // 「正在建、还没落库」的记号：id 还是 0，键已经在了
  store.createdVoucher = { bookId: 1, id: 0, createKey: 'idem-key-1' }
  const p = page(store)
  p.deps.formBookId.value = 1
  await p.load()
  assert.equal(p.deps.createKey.value, 'idem-key-1',
    '幂等键没被捡回来 —— 重挂后就是一把新键，服务端会当真新建一张')
})

t('**行为**：换了页面 → 那个记号要清（点「新增凭证」不该被带回刚才那张）', () => {
  const src = grabWatchCall(MAIN_LAYOUT, 'watch(() => `${route.name}')
  let captured = null
  const factory = new Function('watch', 'route', 'tabs', 'store', `${src}\n    return null`)
  const route = { name: 'voucher-new', fullPath: '/vouchers/new', path: '/vouchers/new' }
  const store = { prevBookId: 0, createdVoucher: { bookId: 1, id: 7, createKey: 'k' } }
  factory((getter, cb, options) => {
    captured = { getter, cb, options }
    if (options?.immediate) cb()
  }, route, { open: () => {} }, store)
  assert.ok(captured, '没抠到那个 watch')
  // 换页面 → 回调跑 → 那个记号被清
  captured.cb()
  assert.equal(store.createdVoucher, null,
    '换了页面却不清那个记号 —— 点「新增凭证」会被带回刚才建的那张')
})

for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK   ' + name)
    pass++
  } catch (e) {
    console.log('  FAIL ' + name + '\n       ' + String(e.stack || e.message).split('\n').slice(0,5).join('\n       '))
    fail++
  }
}

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await vite.close()
process.exit(fail === 0 ? 0 : 1)
