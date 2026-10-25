/**
 * 凭证编辑页的请求竞态（审计 P3-07）。
 *
 * 审计把这条定为 C 级「未复现」—— 依赖网络时序。那就把它**变成可复现的**：
 * 把 load() 里的异步链原样抽出来，用一个「先发后到」的假后端驱动，
 * 让先发起的请求最后才 resolve。
 *
 * 关键是不能只测一个写出来的等价物 —— 所以这里直接从**真实组件源码**里
 * 把 load / loadNeighbour 的实现抠出来跑（见 loadFromSource）。
 *
 * 注意 stale 是可选的：旧版组件里根本没有它。抠不到就留空，
 * 这样拿旧版源码跑本文件会**断言失败**（表单被旧响应覆盖），
 * 而不是解析阶段就崩掉 —— 崩掉证明不了修的是什么。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 用例大多是异步的，统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/**
 * 从组件源码里取出 load / loadNeighbour 的实现，做成可执行的函数。
 * 用 new Function 拼一个最小作用域，让被测代码跑的是**源码里的原话**。
 */
function loadFromSource({ getVoucher, neighbour } = {}) {
  /** 抠出 `function <name>(...) {...}` 的整段源码。
   *
   *  名字后面必须紧跟 `(` —— 用 indexOf('function load') 会先撞上 loadSubjects，
   *  抠出来的就是另一个函数了，而断言照样「通过」。抠完立刻自检。
   */
  const grab = (name) => {
    const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(SRC)
    if (!m) throw new Error(`源码里找不到 function ${name}(`)
    const i = m.index
    let depth = 0, started = false, j = i
    for (; j < SRC.length; j++) {
      if (SRC[j] === '{') { depth++; started = true }
      else if (SRC[j] === '}') { depth--; if (started && depth === 0) return SRC.slice(i, j + 1) }
    }
    throw new Error(`${name} 的大括号没配平`)
  }
  // 修复前组件里没有 stale()，抠不到就当没有代次机制
  const staleSrc = (SRC.match(/function stale\(seq\) \{[^}]*\}/) || [''])[0]
  // yearOfVoucher 是 load() 的依赖：这一页用**哪一年的科目表**由它说了算
  // （跨年凭证的科目能显示出来，全靠它取对了那张表）。
  // 抠不到就留空 —— 那样 load() 会 ReferenceError，用例照样红，
  // 而且红的原因一眼看得出是「少了这个依赖」，不是时序搭错了。
  const yearSrc = (SRC.match(/function yearOfVoucher\(v\) \{[^}]*\}/) || [''])[0]
  const loadSrc = grab('load')
  const neighbourSrc = grab('loadNeighbour')
  // 自检：抠出来的必须是 load / loadNeighbour 本人，不能是 loadSubjects 之类
  for (const [name, src] of [['load', loadSrc], ['loadNeighbour', neighbourSrc]]) {
    const sig = src.match(/(?:async\s+)?function\s+(\w+)\s*\(/)
    assert.equal(sig[1], name, `抠出来的是 ${sig[1]}() 而不是 ${name}()，本测试会空转`)
  }

  // 组件里的顶层响应式状态，用 {value} 模拟 ref
  const voucherId = { value: 0 }
  const savedAt = { value: '' }
  const insertCtx = { value: null }
  const subjectRefs = { clear() {} }
  const rowEls = { clear() {} }
  // load() 换凭证时会把它整套清掉：标记状态拿分录对象当键，分录对象刚被换了一整套
  const markingRows = { value: new Set() }
  // 「已提交的数」同理（键也是分录对象）：换凭证时一起清掉
  const committedAmounts = { value: new Map() }
  const voucherPeriod = { value: null }
  // load() 会写它：余额显示要知道这张凭证落在哪一期（科目格左下角那个数）
  const voucherPeriodId = { value: 0 }
  const lastSaved = { value: '' }
  // 乐观锁令牌与冲突标志（审计 §3.15 的 409 契约）
  const voucherVersion = { value: '' }
  const versionConflict = { value: false }
  const prevId = { value: null }
  const nextId = { value: null }
  const neighbourScope = { value: '' }
  // 切账套撞号的提示状态（见 VoucherEdit.vue 的 crossBook 说明）。
  // 这里恒为 null —— 这一组用例测的是「翻凭证」这条，不涉及切账套。
  const crossBook = { value: null }

  const ctx = {
    route: { params: { id: '0' }, query: {} },
    form: {},
    voucherId, savedAt, insertCtx, subjectRefs, rowEls, markingRows, committedAmounts, voucherPeriod, voucherPeriodId,
    lastSaved, prevId, nextId, neighbourScope, crossBook,
    // 乐观锁令牌与冲突标志（审计 §3.15 的 409 契约）
    voucherVersion, versionConflict,
    // store：凭证页从它这里取「换账套前的那一本」（切账套撞号提示，见审计 §3.33）。
    // 这里恒为 0 = 没切账套，所以不会有提示。
    store: { bookId: 1, periods: [], books: [], takePrevBookId: () => 0 },
    listFilter: { value: {} },
    entrySig: { value: 'sig' },
    api: {
      getVoucher: (bookId, id) => getVoucher(id),
      neighbour: (bookId, id, filter) => neighbour
        ? neighbour(id, filter)
        : Promise.resolve({ prev_id: id - 1, next_id: id + 1, scope: 'all', index: 0, total: 1 }),
    },
    loadSubjects: async () => {},
    ensureRows: () => {},
    // 打开一张凭证后把每一行「定下来」（余额从一开始就是对的）—— 见 VoucherEdit.vue
    seedCommitted: () => {},
    loadInsertPreview: async () => null,
    loadNextDefaults: async () => ({ voucher_no: '记001', voucher_date: '2026-01-31' }),
    defaultDate: () => '2026-01-31',
    ElMessage: { success() {}, error() {}, warning() {} },
    router: { push() {} }, history: {}, location: {},
  }

  const factory = new Function(
    'ctx',
    `
    const { route, form, voucherId, savedAt, insertCtx, subjectRefs, rowEls, markingRows, committedAmounts,
            voucherPeriod, voucherPeriodId, lastSaved, prevId, nextId, neighbourScope, crossBook,
            voucherVersion, versionConflict, store, listFilter, entrySig,
            api, loadSubjects, ensureRows, seedCommitted, loadInsertPreview, loadNextDefaults,
            defaultDate, ElMessage, router, history, location } = ctx
    const bookName = (id) => '账套 ' + id
    ${staleSrc}
    ${yearSrc}
    let loadSeq = 0
    ${neighbourSrc}
    ${loadSrc}
    return { load, seq: () => loadSeq }
    `)
  return { ctrl: factory(ctx), ctx, hasSeqGuard: Boolean(staleSrc) }
}

/** 让微任务队列跑到「稳定」为止。 */
const drain = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }

/** 假后端：getVoucher 返回 deferred，由用例决定谁先回来。 */
function deferredBackend() {
  const pending = new Map()
  const asked = []       // 被请求过的 id，按先后
  const arrived = []     // 回来的顺序，用于「活着性」断言
  return {
    asked, arrived,
    getVoucher(id) {
      asked.push(id)
      return new Promise((resolve) => pending.set(id, (v) => { arrived.push(id); resolve(v) }))
    },
    resolve(id, data) {
      const r = pending.get(id)
      assert.ok(r, `凭证 ${id} 的请求还没发出去就 resolve 了 —— 场景没搭对`)
      r(data)
    },
  }
}

const voucher = (no, id, remark) => ({
  voucher_no: no, voucher_date: `2026-0${id}-10`, status: 'UNPOSTED',
  attachments: 0, remark, period_id: id,
  entries: [{ subject_id: id, direction: 'DEBIT', amount: String(id), summary: remark }],
})

console.log('== P3-07 过期响应不得覆盖新表单 ==')

t('后发先至时，先发的旧响应被丢弃', async () => {
  const be = deferredBackend()
  const { ctrl, ctx } = loadFromSource({ getVoucher: (id) => be.getVoucher(id) })

  // 第一次：打开凭证 1（慢）
  ctx.route.params.id = '1'
  const p1 = ctrl.load()
  await drain()

  // 第二次：用户点了「下一张」→ 凭证 2（快）
  ctx.route.params.id = '2'
  const p2 = ctrl.load()
  await drain()

  // 场景真的搭起来了：两个请求都发出去了，且是 1 在前
  assert.deepEqual(be.asked, [1, 2], `请求顺序不是先1后2：${JSON.stringify(be.asked)}`)

  // 新的先回来
  be.resolve(2, voucher('记002', 2, 'B'))
  await p2
  assert.equal(ctx.form.voucher_no, '记002', '凭证 2 自己回来后表单就该是 B')

  // 旧的最后才回来 —— 这一步就是 bug 的触发点
  be.resolve(1, voucher('记001', 1, 'A'))
  await p1
  assert.deepEqual(be.arrived, [2, 1], `返回顺序不是「先2后1」：${JSON.stringify(be.arrived)}`)

  assert.equal(ctx.form.voucher_no, '记002',
    `表单被旧响应覆盖了，实际是 ${ctx.form.voucher_no} —— 页面上会是 A 的编号配 B 的分录`)
  assert.equal(ctx.form.remark, 'B')
  assert.equal(ctx.form.entries[0].summary, 'B', '分录也被换成了旧凭证的')
})

t('连点三次「下一张」只认最后一次', async () => {
  const be = deferredBackend()
  const { ctrl, ctx } = loadFromSource({ getVoucher: (id) => be.getVoucher(id) })

  const ps = []
  for (const id of [1, 2, 3]) {
    ctx.route.params.id = String(id)
    ps.push(ctrl.load())
    await drain()
  }
  assert.deepEqual(be.asked, [1, 2, 3], `请求顺序不对：${JSON.stringify(be.asked)}`)

  be.resolve(3, voucher('记003', 3, 'C'))
  await ps[2]
  be.resolve(1, voucher('记001', 1, 'A'))
  await ps[0]
  be.resolve(2, voucher('记002', 2, 'B'))
  await ps[1]
  assert.deepEqual(be.arrived, [3, 1, 2], `返回顺序不对：${JSON.stringify(be.arrived)}`)

  assert.equal(ctx.form.voucher_no, '记003', `最后一次的表单被覆盖了：${ctx.form.voucher_no}`)
})

t('过期的那次不该去刷「上一张/下一张」导航', async () => {
  const be = deferredBackend()
  const askedNeighbour = []
  const { ctrl, ctx } = loadFromSource({
    getVoucher: (id) => be.getVoucher(id),
    neighbour: (id) => { askedNeighbour.push(id); return Promise.resolve({ prev_id: 0, next_id: 0, scope: 'all', index: 0, total: 0 }) },
  })

  ctx.route.params.id = '1'
  const p1 = ctrl.load()
  await drain()
  ctx.route.params.id = '2'
  const p2 = ctrl.load()
  await drain()

  be.resolve(2, voucher('记002', 2, 'B'))
  await p2
  be.resolve(1, voucher('记001', 1, 'A'))
  await p1

  // 两次 load 只应换来一次 neighbour —— 过期那次再刷一遍，导航条会挂在
  // 一张已经不该在屏幕上的凭证上，用户点「上一张」就被弹回去了
  assert.deepEqual(askedNeighbour, [2],
    `过期响应仍然发了 neighbour 请求，实际发出 ${JSON.stringify(askedNeighbour)}`)
  assert.equal(ctx.voucherId.value, 2, '当前凭证 id 不该被过期响应改回去')
})

let i = 0
for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK   ' + name)
    pass++
  } catch (e) {
    console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0])
    fail++
  }
  i++
}

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
