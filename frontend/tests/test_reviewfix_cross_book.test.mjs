/**
 * 审查 P2-5：切账套后停留在凭证详情页，同一 URL 的 id 会解析成新账套的另一张凭证，
 * 且**无任何提示**。
 *
 * 凭证 id 是**账套内自增**的，不同账套之间会撞号（VoucherEdit.vue 自己的注释就写着）。
 * 切账套后代码主动 resetForm() 再 load()，**用同一个 id 去新账套里查**：
 *   · 新账套恰好有这个 id → 页面**静默打开另一本账的另一张凭证**；
 *   · 新账套没有 → 报错跳列表（那半处理得体面）。
 *
 * 修法：**只加提示，不改加载行为** —— 切过去之后明示一次
 * 「原凭证是账套 A 的 #1581，这里打开的是本账套的 #1581」。
 *
 * 这里从**真实组件源码**里抠出 resetForm() 与 load() 跑一遍，
 * 测的是页面上真在用的那段代码（与 voucher_balance.test.mjs 同一个路子）。
 *
 * **记号放在 store 里，不在组件里**（审计 §3.33）：切账套时凭证页会被
 * **整个重挂**（MainLayout 的 viewKey 变了），组件内的 watch 随 scope 一起
 * 停用 —— 以前把「上一张是哪个账套的哪张」记在 resetForm 里，那段代码
 * **永远执行不到**，于是这句提示一次也没发出来过。现在记号放在
 * stores/session.js 的 prevBookId 里，重挂之后才来取。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

/**
 * 抠出 `function <name>(...) {...}` 的整段源码（按大括号配平）。
 * 模板串里的 `${...}` 花括号是配平的，不影响计数。
 */
function grabFn(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(SRC)
  assert.ok(m, `源码里找不到 function ${name}(`)
  const i = m.index
  let depth = 0, j = SRC.indexOf('(', i)
  for (; j < SRC.length; j++) {
    if (SRC[j] === '(') depth++
    else if (SRC[j] === ')') { depth--; if (depth === 0) break }
  }
  assert.equal(SRC[j], ')', `${name} 的参数表没配平`)
  const bodyStart = SRC.indexOf('{', j)
  assert.ok(bodyStart !== -1, `${name} 找不到函数体`)
  depth = 0
  for (j = bodyStart; j < SRC.length; j++) {
    if (SRC[j] === '{') depth++
    else if (SRC[j] === '}') { depth--; if (depth === 0) break }
  }
  const src = SRC.slice(i, j + 1)
  assert.ok(src.endsWith('}'), `${name} 抠出来的不是一段完整的函数`)
  return src
}

const RESET_SRC = grabFn('resetForm')
const LOAD_SRC = grabFn('load')

const ref = (v) => ({ value: v })

/** 搭一套「重挂后的凭证页」：状态与依赖全由外面喂进去。 */
function page(over = {}) {
  const said = []
  const pushed = []
  const deps = {
    route: { params: { id: '1581' }, query: {} },
    store: {
      bookId: 2, periods: [], currentYear: 2026,
      books: [
        { id: 1, name: '甲账套', company_name: '甲公司' },
        { id: 2, name: '乙账套', company_name: '乙公司' },
      ],
      /**
       * **换账套前的那一本**（0 = 没切账套）。
       *
       * 这份记号在 store 里，不在组件里 —— 因为切账套时凭证页会被**整个重挂**
       * （MainLayout 的 viewKey 变了），组件内的变量活不到那一步，
       * 于是「已切换到账套…」那句提示永远发不出来（审计 §3.33）。
       */
      prevBookId: 0,
      takePrevBookId() {
        const prev = this.prevBookId
        this.prevBookId = 0
        return prev
      },
    },
    voucherId: ref(0),
    voucherPeriod: ref(null),
    voucherPeriodId: ref(0),
    voucherPeriodInfo: null,
    insertCtx: ref(null),
    savedAt: ref(''),
    failedSig: ref(''),
    lastSaved: ref(''),
    entrySig: ref('sig'),
    prevId: ref(null),
    nextId: ref(null),
    neighbourScope: ref(''),
    activeRow: ref(null),
    hoverRow: ref(null),
    markingRows: ref(new Set()),
    // 「已提交的数」：键是分录对象，换凭证/切账套时一起清掉（见 VoucherEdit.vue）
    committedAmounts: ref(new Map()),
    crossBook: ref(null),
    formBookId: ref(1),
    // 乐观锁令牌与冲突标志（审计 §3.15 的 409 契约）——
    // load() 会往 voucherVersion 里写单张响应给的令牌。
    voucherVersion: ref(''),
    versionConflict: ref(false),
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
    api: { getVoucher: async () => ({ id: 1581, voucher_no: '记-0001', period_id: 5, entries: [] }) },
    router: { push: (p) => pushed.push(p) },
    ElMessage: {
      error: (m) => said.push(['error', m]),
      warning: (m) => said.push(['warning', m]),
      info: (m) => said.push(['info', m]),
      success: (m) => said.push(['success', m]),
    },
    ...over,
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
    const neighbourScope = d.neighbourScope
    const activeRow = d.activeRow
    const hoverRow = d.hoverRow
    const markingRows = d.markingRows
    const committedAmounts = d.committedAmounts
    const crossBook = d.crossBook
    const formBookId = d.formBookId
    // 乐观锁令牌与冲突标志（审计 §3.15 的 409 契约）
    const voucherVersion = d.voucherVersion
    const versionConflict = d.versionConflict
    const form = d.form
    const subjectRefs = d.subjectRefs
    const rowEls = d.rowEls
    const sumRefs = d.sumRefs
    const amtRefs = d.amtRefs
    const ensureRows = d.ensureRows
    // 打开一张凭证后把每一行「定下来」（余额从一开始就是对的）—— 见 VoucherEdit.vue
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
    ${RESET_SRC}
    ${LOAD_SRC}
    return { resetForm, load }
  `)
  const fns = factory(deps)
  return { ...fns, deps, said, pushed }
}

test('切账套时「上一本账」从 store 取到，并明示一次', async () => {
  const p = page()
  p.deps.store.prevBookId = 1        // 换账套前的那一本
  await p.load()
  // crossBook 是**用完即清**的（提示发出去就 null），所以这里断言的是
  // 「提示真的发出来了」—— 那才是这件事的验收标准。
  assert.equal(p.said.length, 1, `应该明示一次：${JSON.stringify(p.said)}`)
  assert.match(p.said[0][1], /甲公司/, '提示里要指名原账套')
  assert.match(p.said[0][1], /#1581/, '提示里要指名原凭证的号')
  assert.equal(p.deps.store.prevBookId, 0, '这份记号是一次性的：提示完就清')
})

test('新增页（本来就没有凭证 id）不记 —— 那不是撞号', async () => {
  const p = page()
  p.deps.route.params.id = ''        // 新增页没有凭证 id
  p.deps.store.prevBookId = 1
  await p.load()
  assert.equal(p.deps.crossBook.value, null)
})

test('新账套恰好有同号凭证：明示一次「原凭证是账套 A 的 #1581，这里是本账套的 #1581」', async () => {
  const p = page()
  // 模拟切账套：store 里记着「上一本账」，当前账套已经是新的
  p.deps.store.prevBookId = 1
  await p.load()

  assert.equal(p.said.length, 1, `应该只提示一次：${JSON.stringify(p.said)}`)
  const [kind, text] = p.said[0]
  assert.equal(kind, 'warning', '这不是错误，是一句「已经换人了」的提示')
  assert.match(text, /甲公司/, '提示里要指名原账套')
  assert.match(text, /乙公司/, '提示里要指名当前账套')
  assert.match(text, /#1581/, '提示里要指名原凭证的号')
  assert.match(text, /本账套/, '提示里要说清现在打开的是本账套的那张')
})

test('新账套没有这个 id：报错里也要说清是从哪本账切过来的', async () => {
  const p = page({
    api: { getVoucher: async () => { throw new Error('404') } },
  })
  p.deps.store.prevBookId = 1
  await p.load()

  assert.equal(p.said.length, 1, `应该只提示一次：${JSON.stringify(p.said)}`)
  const [kind, text] = p.said[0]
  assert.equal(kind, 'error')
  assert.match(text, /甲公司/, '报错里要指名原账套 —— 不然人只知道自己「凭证没了」')
  assert.match(text, /#1581/)
  assert.deepEqual(p.pushed, [{ name: 'vouchers' }], '读不到就该回列表')
})

test('提示**只来一次**：后面再 load() 不重复说', async () => {
  const p = page()
  p.deps.store.prevBookId = 1
  await p.load()
  assert.equal(p.said.length, 1)
  await p.load()
  assert.equal(p.said.length, 1, `提示说了一遍又一遍：${JSON.stringify(p.said)}`)
})

test('没有切账套（就是普通打开一张凭证）时一句话都不说', async () => {
  const p = page()
  await p.load()
  assert.deepEqual(p.said, [], `不该有提示：${JSON.stringify(p.said)}`)
  assert.deepEqual(p.pushed, [], '不该跳页面')
  assert.equal(p.deps.form.voucher_no, '记-0001', '凭证没加载出来')
})
