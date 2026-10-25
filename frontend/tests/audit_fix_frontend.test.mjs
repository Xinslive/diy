/**
 * 审计 §3.27 / §3.30 / §3.32 的前端修复（回归）。
 *
 * 这三条此前**只有代码、没有用例** —— 修复本身是对的，但没人钉着它，
 * 下次有人改回「体检失败就当没问题」「报表格直接用 fmt」也不会有人喊。
 *
 * ① **行为级**：从**真实源码**里抠出实现跑一遍（不是抄一份等价实现）。
 * ② **源码级**：模板上的绑定与状态机。
 *
 * ⚠️ **抠取器先有已知答案的自测**：抠取器自己出错的话，
 * 后面所有「绿」都是假绿 —— 恒真的探针比没有探针更坏。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = new URL('../', import.meta.url)

const ORGANIZE = fs.readFileSync(new URL('src/views/voucher/VoucherOrganize.vue', ROOT), 'utf8')
const REPORT = fs.readFileSync(new URL('src/views/report/ReportView.vue', ROOT), 'utf8')
const SEL = fs.readFileSync(new URL('src/composables/useCellSelection.js', ROOT), 'utf8')
const VOUCHER_EDIT = fs.readFileSync(new URL('src/views/voucher/VoucherEdit.vue', ROOT), 'utf8')
const CLIENT = fs.readFileSync(new URL('src/api/client.js', ROOT), 'utf8')
const LEDGERS = {
  GeneralLedger: fs.readFileSync(new URL('src/views/ledger/GeneralLedger.vue', ROOT), 'utf8'),
  DetailLedger: fs.readFileSync(new URL('src/views/ledger/DetailLedger.vue', ROOT), 'utf8'),
  BalanceSheet: fs.readFileSync(new URL('src/views/ledger/BalanceSheet.vue', ROOT), 'utf8'),
}

const scriptOf = (src) => src.slice(src.indexOf('<script setup>'), src.indexOf('</script>'))

let pass = 0, fail = 0
const tests = []
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

/** 抠出 `function <name>(...) {...}` 的整段源码（页面上真在用的那段）。 */
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

const flush = () => new Promise((r) => setTimeout(r, 0))

console.log('== 抠取器自测 ==')

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

t('抠取器自测：三个目标函数都抠得出来', () => {
  assert.ok(grabFn('withStructureBusy', scriptOf(REPORT)).includes('structureBusy'))
  assert.ok(grabFn('loadCheck', scriptOf(ORGANIZE)).includes('checkFailed'))
  assert.ok(grabFn('liveRows', SEL).includes('isConnected'))
})

// ======================================================= §3.27 整理凭证
console.log('== §3.27 体检失败时不能放开「按当前顺序重新编号」 ==')

t('体检失败是一个**独立状态**，不是「没问题」', () => {
  const s = scriptOf(ORGANIZE)
  assert.match(s, /const checkFailed = ref\(false\)/)
  assert.match(s, /const outOfOrder = computed/)
  // RENUM 的禁用条件 = 体检失败 **或** 时序不对
  assert.match(s, /renumBlocked = computed\(\(\) => checkFailed\.value \|\| outOfOrder\.value\)/)
  assert.match(ORGANIZE, /value="RENUM" :disabled="renumBlocked"/)
})

t('**行为**：体检请求失败 → RENUM 被按住，执行时并说清楚为什么', async () => {
  const src = scriptOf(ORGANIZE)
  const factory = new Function('deps', `
    const store = deps.store
    const check = deps.check
    const checkFailed = deps.checkFailed
    const outOfOrder = deps.outOfOrder
    const renumBlocked = deps.renumBlocked
    const strategy = deps.strategy
    const loading = deps.loading
    const plan = deps.plan
    const voucherApi = deps.voucherApi
    const ElMessage = deps.ElMessage
    ${grabFn('payload', src)}
    ${grabFn('loadCheck', src)}
    ${grabFn('preview', src)}
    return { loadCheck, preview }
  `)
  const calls = { warn: [], preview: 0 }
  const checkFailed = { value: false }
  const outOfOrder = { value: false }
  // renumBlocked 在真组件里是 computed —— 这里照它的语义做成派生值
  const renumBlocked = { get value() { return checkFailed.value || outOfOrder.value } }
  const deps = {
    store: { bookId: 7, periodId: 3 },
    check: { value: null },
    checkFailed,
    outOfOrder,
    renumBlocked,
    strategy: { value: 'RENUM' },
    loading: { value: false },
    plan: { value: null },
    voucherApi: {
      voucherPeriodCheck: async () => { throw new Error('500') },
      organizePreview: async () => { calls.preview++; return { total_changed: 0, total_gaps: 0 } },
    },
    ElMessage: {
      warning: (m) => calls.warn.push(m),
      success: () => {},
    },
  }
  const { loadCheck, preview } = factory(deps)
  await loadCheck()
  assert.equal(checkFailed.value, true, '体检失败了却没记 checkFailed')
  assert.equal(renumBlocked.value, true, '体检失败了却放开了 RENUM')
  // URL 复原出来的 strategy 可能已经是 RENUM —— 执行时要挡住并说清楚
  await preview()
  assert.equal(calls.preview, 0, '体检没跑成功还照样去整理了')
  assert.ok(calls.warn.length > 0, '体检没跑成功却一句话不说')
  assert.match(calls.warn[0], /体检/, `提示没说到体检：${calls.warn[0]}`)
})

t('体检成功 → checkFailed 归位', async () => {
  const src = scriptOf(ORGANIZE)
  const factory = new Function('deps', `
    const store = deps.store
    const check = deps.check
    const checkFailed = deps.checkFailed
    const outOfOrder = deps.outOfOrder
    const renumBlocked = deps.renumBlocked
    const strategy = deps.strategy
    const loading = deps.loading
    const plan = deps.plan
    const voucherApi = deps.voucherApi
    const ElMessage = deps.ElMessage
    ${grabFn('payload', src)}
    ${grabFn('loadCheck', src)}
    return loadCheck
  `)
  const checkFailed = { value: true }
  const deps = {
    store: { bookId: 7, periodId: 3 },
    check: { value: null },
    checkFailed,
    outOfOrder: { value: false },
    renumBlocked: { value: false },
    strategy: { value: 'KEEP' },
    loading: { value: false },
    plan: { value: null },
    voucherApi: { voucherPeriodCheck: async () => ({ time_out_of_order: [] }) },
    ElMessage: { warning: () => {}, success: () => {} },
  }
  await factory(deps)()
  assert.equal(checkFailed.value, false, '体检成功了 checkFailed 还挂着')
})

// ======================================================= §3.30 报表设计模式
console.log('== §3.30 报表设计的插行/删行不能并发 ==')

t('四个结构动作共用一个 busy，且有早退', () => {
  const s = scriptOf(REPORT)
  assert.match(s, /const structureBusy = ref\(false\)/)
  const fn = grabFn('withStructureBusy', s)
  assert.match(fn, /if \(structureBusy\.value\) return/)
  for (const name of ['insertRow', 'removeRow', 'insertCol', 'removeCol']) {
    assert.match(grabFn(name, s), /withStructureBusy\(/, `${name} 没走 withStructureBusy`)
  }
})

t('执行期间相关按钮是灰的', () => {
  const hits = REPORT.match(/:disabled="structureBusy"/g) || []
  assert.equal(hits.length, 5, `结构按钮有 ${hits.length} 处被按住（应为 5：插行/删行各两处 + 插列/删列）`)
  // 四个结构动作各自的按钮都要按住
  for (const op of ['insertCol', 'removeCol', 'insertRow', 'removeRow']) {
    const line = REPORT.split('\n').find(l => l.includes(`:disabled="structureBusy"`) && l.includes(op))
    assert.ok(line, `${op} 的按钮没被按住`)
  }
})

t('**行为**：双击「＋行」只发一次 POST', async () => {
  const s = scriptOf(REPORT)
  const factory = new Function('deps', `
    const structureBusy = deps.structureBusy
    const api = deps.api
    const load = deps.load
    const ElMessage = deps.ElMessage
    const templateId = deps.templateId
    const store = deps.store
    ${grabFn('withStructureBusy', s)}
    ${grabFn('insertRow', s)}
    return insertRow
  `)
  let release
  const gate = new Promise((r) => { release = r })
  const calls = { addRow: 0, load: 0 }
  const deps = {
    structureBusy: { value: false },
    api: { addRow: async () => { calls.addRow++; await gate } },
    load: async () => { calls.load++ },
    ElMessage: { success: () => {} },
    templateId: { value: 5 },
    store: { bookId: 7 },
  }
  const insertRow = factory(deps)
  const p1 = insertRow(3)
  const p2 = insertRow(3)   // 双击
  await flush()
  assert.equal(calls.addRow, 1, `双击发了 ${calls.addRow} 次 POST /reports/5/rows`)
  assert.equal(deps.structureBusy.value, true, '在途时 busy 必须是 true')
  release()
  await p1
  await p2
  assert.equal(calls.addRow, 1, '第二趟也发出去了')
  assert.equal(deps.structureBusy.value, false, '结束后 busy 要放开')
})

// ======================================================= §3.32 矩形选区
console.log('== §3.32 选区跨重渲染不得复制到旧表格 ==')

t('复制前先验「行还挂在文档上」', () => {
  const fn = grabFn('liveRows', SEL)
  assert.match(fn, /isConnected/)
  assert.match(fn, /return null/)
})

t('切路由 / 拆装时把选区清掉', () => {
  assert.match(SEL, /window\.addEventListener\('hashchange', onRouteChange\)/)
  assert.match(SEL, /const onRouteChange = \(\) => clear\(\)/)
  assert.match(SEL, /return \(\) => \{\s*clear\(\)/)
})

// 装一遍真模块，用假 DOM 驱动它。
// element-plus 在 import 时也会往 document 上挂监听（keydown/mousedown/…），
// 所以监听器要按「install 前后的增量」取，不能假设 handlers[type][0] 就是我们的。
const handlers = {}
// Vue 的 transition 钩子会读 ownerDocument.body.offsetHeight ——
// 假元素少了 ownerDocument，那个钩子一跑就抛（而且是异步的，会把整个进程带崩）
const docRef = { current: null }
const makeEl = (tag = 'div') => ({
  nodeType: 1, tagName: String(tag).toUpperCase(), children: [], childNodes: [],
  style: {}, attributes: {}, innerHTML: '', textContent: '', value: '',
  className: '', parentNode: null, isConnected: true,
  get ownerDocument() { return docRef.current },
  classList: { contains: () => false, add() {}, remove() {}, toggle() {} },
  setAttribute(k, v) { this.attributes[k] = v },
  getAttribute(k) { return this.attributes[k] },
  removeAttribute(k) { delete this.attributes[k] },
  appendChild(c) { this.children.push(c); c.parentNode = this; return c },
  removeChild(c) { this.children = this.children.filter(x => x !== c); return c },
  insertBefore(c) { this.children.unshift(c); c.parentNode = this; return c },
  addEventListener() {}, removeEventListener() {},
  contains: () => false,
  querySelector: () => null, querySelectorAll: () => [],
  focus() {}, blur() {}, click() {}, remove() {}, select() {},
  getBoundingClientRect: () => ({ top: 0, left: 0, width: 0, height: 0 }),
})
globalThis.document = {
  addEventListener: (type, fn) => { (handlers[type] ||= []).push(fn) },
  removeEventListener: () => {},
  createElement: () => makeEl(),
  createElementNS: (ns, tag) => makeEl(tag),
  createTextNode: (t) => ({ nodeType: 3, textContent: t }),
  createComment: (t) => ({ nodeType: 8, textContent: t }),
  querySelector: () => null,
  body: { appendChild() {}, offsetHeight: 0 },
  execCommand: () => true,
}
docRef.current = globalThis.document
globalThis.requestAnimationFrame = (cb) => { cb(); return 0 }
globalThis.window = {
  addEventListener: () => {}, removeEventListener: () => {},
  // element-plus 的浏览器探测在 isClient 为真时读 window.navigator.userAgent
  navigator: { userAgent: 'node' },
  getComputedStyle: () => ({ getPropertyValue: () => '', display: 'block' }),
}
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '', display: 'block' })

const { createServer } = await import('vite')
const vite = await createServer({
  root: fileURLToPath(new URL('../', import.meta.url)),
  logLevel: 'error', server: { middlewareMode: true },
})
const { installCellSelection } = await vite.ssrLoadModule('/src/composables/useCellSelection.js')

/**
 * 跑一遍「框选 → 表格变了 → Ctrl+C」。
 * @param {'table'|'row'} detach 脱钩的是**表格**还是**行**
 */
async function copyAfterDetach(detach) {
  installCellSelection()
  const onDown = handlers.mousedown[handlers.mousedown.length - 1]
  const onCopy = handlers.copy[handlers.copy.length - 1]

  const cell = {
    closest: null, parentElement: null, cells: [],
    classList: { contains: () => false, toggle() {}, remove() {} }, innerText: '旧内容',
  }
  const row = { cells: [cell], classList: { contains: () => false, toggle() {}, remove() {} } }
  const table = { tBodies: [{ rows: [row] }] }
  cell.parentElement = row
  cell.closest = (sel) => (sel === 'td' ? cell : sel === 'table' ? table : null)
  table.isConnected = true
  row.isConnected = true

  const down = {
    button: 0, target: cell, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true },
  }
  onDown(down)
  assert.equal(down.defaultPrevented, true, '按下没被接管 —— 假 DOM 构造有问题，这一整条用例就白测了')

  // 表格重渲染 / 弹窗关闭 / 路由切换之后：
  //   'table' = 整张表脱离文档；'row' = 表还在，但行被换掉了（行脱离文档）
  if (detach === 'table') table.isConnected = false
  else row.isConnected = false

  let setData = null
  let prevented = false
  const copy = {
    preventDefault() { prevented = true },
    clipboardData: { setData: (t, v) => { setData = v } },
  }
  // refuseCopy() 里的 ElMessage 要真 DOM 才渲染得出来，这里没有 ——
  // 但**要断言的两件事都发生在它之前**（先 preventDefault，再 refuseCopy），
  // 所以接住渲染的异常不影响判据；反过来若哪天把顺序改成「先弹提示再拦」，
  // preventDefault 就不会被调用，用例会红。
  try { onCopy(copy) } catch { /* 假 DOM 渲染不了提示条，与判据无关 */ }
  return { prevented, setData }
}

t('**行为**：整张表已经脱离文档 → 拒绝复制，不往剪贴板塞旧内容', async () => {
  const { prevented, setData } = await copyAfterDetach('table')
  assert.equal(prevented, true, '旧选区没有拦住这次复制')
  assert.equal(setData, null, `把已经不在这儿的表格内容塞进了剪贴板：${setData}`)
})

t('**行为**：表还在、行被换掉了 → 同样拒绝复制（行级校验）', async () => {
  // 这一条钉的是 ``current.rows[0].isConnected`` 那一道 ——
  // 表还在文档里（表格级校验放行），但按下时抓的那一行已经换了，
  // 复制到的是**旧行**的内容。
  const { prevented, setData } = await copyAfterDetach('row')
  assert.equal(prevented, true, '行换了却没拦住这次复制')
  assert.equal(setData, null, `把已经换掉的旧行内容塞进了剪贴板：${setData}`)
})


// ======================================================= §3.24 跨组件的 flush
console.log('== §3.24 切期间时，旧组件那一趟 flush 不能丢 ==')

t('**行为**：盒子跨组件传递那一趟 promise，且只取一次', async () => {
  // voucherFlush.js **零依赖**，直接 import 就行 ——
  // 不走 vite：同一个 vite server 在前面的用例里已经跑过一轮，
  // 再 ssrLoadModule 一个新模块可能整个挂住（实测过）。
  const { setPendingFlush, takePendingFlush } =
    await import('../src/utils/voucherFlush.js')
  let done = false
  const p = new Promise((r) => { setTimeout(() => { done = true; r('ok') }, 5) })
  setPendingFlush(p)
  const taken = takePendingFlush()
  assert.equal(taken, p, '盒子里的那一趟没取出来')
  assert.equal(takePendingFlush(), null, '取了一次之后还留着 —— 下一张凭证会白等一趟')
  await taken
  assert.equal(done, true)
})

t('没有在途的那一趟时返回 null（调用方直接放行）', async () => {
  const { takePendingFlush } = await import('../src/utils/voucherFlush.js')
  assert.equal(takePendingFlush(), null)
})

t('旧组件把 flush 挂出去，新组件**先等它**再 load()', () => {
  const s = scriptOf(VOUCHER_EDIT)
  // 旧组件：卸载时挂出去
  assert.match(s, /setPendingFlush\(flushIfDirty\(\)\)/)
  // 新组件：取出来先 await，再 load()
  const mounted = s.slice(s.indexOf('onMounted(async () =>'))
  const take = mounted.indexOf('takePendingFlush()')
  const load = mounted.indexOf('await load()')
  assert.ok(take !== -1, '新组件没有取 pending flush')
  assert.ok(load !== -1, '新组件没有 load()')
  assert.ok(take < load, '先 load 再等 —— 新页面拿到的就是改动前的值')
})

// ======================================================= §3.25 加载失败 ≠ 没有数据
console.log('== §3.25 网络/超时不能静默，账簿三视图不能把失败渲染成空表 ==')

t('拦截器对网络失败/超时有兜底提示（不静默）', () => {
  assert.match(CLIENT, /ECONNABORTED/)
  assert.match(CLIENT, /网络异常/)
  // 「取消」不算错 —— 但真正的失败必须出声
  assert.match(CLIENT, /isCanceled\(error\)/)
})

t('账簿三视图都有「加载失败」这条提示，不是渲染成空表', () => {
  for (const [name, src] of Object.entries(LEDGERS)) {
    assert.match(src, /v-if="loadError"/, `${name} 没有 loadError 提示`)
    assert.match(src, /加载失败/, `${name} 的提示没说到加载失败`)
  }
})

t('**行为**：接口失败 → 旧数据留在页面上 + 挂出错误，而不是清空', async () => {
  // useLatestLoad 也抠源码跑（理由同上：不再依赖那个 vite server）
  const { ref } = await import('vue')
  const latest = new Function('deps', `
    const ref = deps.ref
    const isCanceled = deps.isCanceled
    const STALE = Symbol('stale-response')
    ${grabFn('useLatestLoad', fs.readFileSync(new URL('src/composables/useLatestLoad.js', ROOT), 'utf8'))}
    return useLatestLoad
  `)({ ref, isCanceled: () => false })
  const src = grabFn('load', scriptOf(LEDGERS.BalanceSheet))
  const factory = new Function('deps', `
    const store = deps.store
    const periodId = deps.periodId
    const isRange = deps.isRange
    const fromId = deps.fromId
    const includeUnposted = deps.includeUnposted
    const api = deps.api
    const run = deps.run
    const isStale = deps.isStale
    const data = deps.data
    ${src}
    return load
  `)
  const { loading, error, run, isStale } = latest('ledger/balance')
  const data = { value: { rows: [{ id: 1 }] } }
  const load = factory({
    store: { bookId: 7 },
    periodId: { value: 3 },
    isRange: { value: false },
    fromId: { value: null },
    includeUnposted: { value: false },
    api: { balance: async () => { throw new Error('网络异常') } },
    run, isStale, data,
  })
  await load()
  assert.deepEqual(data.value, { rows: [{ id: 1 }] }, '失败把旧数据清空了 —— 页面上看着像「这一期真的一笔都没有」')
  assert.ok(error.value, '失败了却没有错误状态，页面无法区分「失败」与「空」')
  assert.equal(loading.value, false, '结束后要收掉转圈')
})

await vite.close()

console.log('')
for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK  ', name)
    pass++
  } catch (e) {
    console.log('  FAIL', name, '—', String(e.message).split('\n')[0])
    fail++
  }
}
console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
