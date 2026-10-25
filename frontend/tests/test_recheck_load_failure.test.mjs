/**
 * N3：加载失败**不能**被渲染成「没有数据」。
 *
 * 复核方实测确认的现场：ReportView / VoucherList / AuditLog 三处的
 * ``await run(...)`` **没有 try/catch**，也没从 useLatestLoad 解构 error ——
 * 于是一次 500/断网就是：
 *   · run() 把真错照原样往外抛（这是对的，见 useLatestLoad 的注释）；
 *   · 没人接 → 变成一个没人管的 promise rejection；
 *   · 页面上表格空着、**零提示** → 用户以为这个月真的一笔都没有。
 *
 * 另外：**HTTP 500 且响应体没有 detail** 时，api/client.js 一声不吭
 * （``if (msg)`` 不成立、``!error.response`` 也不成立）→ 连红字都没有。
 *
 * 这里把三处的 load() 从**真实源码**里抠出来跑，守卫（useLatestLoad）
 * 也用**真的**那一份；拦截器那段也从真实源码里抠出来跑。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { ref } from 'vue'

const ROOT = new URL('../', import.meta.url)
const read = (p) => fs.readFileSync(new URL(p, ROOT), 'utf8')

const REPORT = read('src/views/report/ReportView.vue')
const LIST = read('src/views/voucher/VoucherList.vue')
const AUDIT = read('src/views/system/AuditLog.vue')
const LATEST = read('src/composables/useLatestLoad.js')
const CLIENT = read('src/api/client.js')

let pass = 0, fail = 0
const tests = []
/** 注册用例。别在这里 await —— 统一在末尾串行跑。 */
const t = (name, fn) => tests.push([name, fn])

/**
 * 这个 '/' 是**正则字面量**的开头吗？
 *
 * 判据：看它前面那个非空白字符。出现在「运算/分隔」位置上的 '/' 是除号，
 * 出现在「值」位置上的 '/' 是正则。关键字后面（return / typeof …）也是正则。
 */
function isRegexStart(src, k) {
  let j = k - 1
  while (j >= 0 && /\s/.test(src[j])) j--
  if (j < 0) return true
  const c = src[j]
  if ('([{,;=:!&|?+-*%<>~^'.includes(c)) return true
  const m = /([A-Za-z_$][\w$]*)$/.exec(src.slice(0, j + 1))
  if (m && ['return', 'typeof', 'case', 'in', 'of', 'new', 'delete', 'void',
    'instanceof', 'do', 'else', 'yield', 'await'].includes(m[1])) return true
  return false
}

/** 配平括号：跳过字符串字面量、注释**与正则字面量**。 */
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
    // **正则字面量**：/^\/plugins\/([a-z0-9.-]+)\// 这种里面藏着 '//'，
    // 不认它就会把后面整行当成注释 —— 抠出来的那一段就腰斩了（实测踩过）。
    if (c === '/' && isRegexStart(src, k)) {
      k++
      let inClass = false
      while (k < src.length) {
        if (src[k] === '\\') { k += 2; continue }
        if (src[k] === '[') inClass = true
        else if (src[k] === ']') inClass = false
        else if (src[k] === '/' && !inClass) { k++; break }
        else if (src[k] === '\n') break
        k++
      }
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

/** 抠出 `useLatestLoad` 那一整份（守卫也是真的）。 */
function grabLatestLoad() {
  const i = LATEST.indexOf('function useLatestLoad')
  assert.ok(i !== -1, '源码里找不到 useLatestLoad')
  const close = matchBracket(LATEST, LATEST.indexOf('(', i), '(', ')')
  const bodyStart = LATEST.indexOf('{', close)
  const bodyEnd = matchBracket(LATEST, bodyStart, '{', '}')
  return LATEST.slice(i, bodyEnd + 1)
}

/** 抠出 `http.interceptors.response.use(...)` 整个调用。 */
function grabInterceptorCall() {
  const marker = 'http.interceptors.response.use('
  const i = CLIENT.indexOf(marker)
  assert.ok(i !== -1, 'client.js 里找不到响应拦截器')
  const open = CLIENT.indexOf('(', i)
  const close = matchBracket(CLIENT, open, '(', ')')
  assert.ok(close !== -1, '拦截器调用的括号没配平')
  return CLIENT.slice(i, close + 1)
}

/** 抠出 `isCanceled`（拦截器里用它把「自己掐掉的」与「真错」分开）。 */
function grabIsCanceled() {
  const i = CLIENT.indexOf('function isCanceled')
  assert.ok(i !== -1, 'client.js 里找不到 isCanceled')
  const close = matchBracket(CLIENT, CLIENT.indexOf('(', i), '(', ')')
  const bodyStart = CLIENT.indexOf('{', close)
  const bodyEnd = matchBracket(CLIENT, bodyStart, '{', '}')
  return CLIENT.slice(i, bodyEnd + 1)
}

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

t('抠取器自测：正则字面量里的 // 不会被当成注释（实测踩过：抠出来的那段被腰斩）', () => {
  const fake = [
    'function f(x) {',
    '  const m = x.match(/^\\/plugins\\/([a-z0-9.-]+)\\//)',
    '  return m ? m[1] : null',
    '}',
    'function other() { return 1 }',
  ].join('\n')
  const got = grabFn('f', fake)
  assert.ok(got.includes('plugins'), '函数体被腰斩了 —— 正则里的 // 被当成注释了')
  assert.ok(got.includes('return m ? m[1] : null'), '函数体后半截丢了')
  assert.ok(!got.includes('function other'), '多抠了下一段')
})

t('抠取器自测：四个目标都抠得出来（抠空了要让断言失败，别让用例空转）', () => {
  assert.ok(grabLatestLoad().includes('const error = ref'), 'useLatestLoad 里没有 error')
  assert.ok(grabIsCanceled().includes('CanceledError'), 'isCanceled 抠错了')
  assert.ok(grabInterceptorCall().includes('interceptors.response.use'), '拦截器抠错了')
  assert.ok(grabFn('load', REPORT).includes('isStale'), 'ReportView 的 load 抠错了')
  assert.ok(grabFn('load', LIST).includes('isStale'), 'VoucherList 的 load 抠错了')
  assert.ok(grabFn('load', AUDIT).includes('isStale'), 'AuditLog 的 load 抠错了')
})

/** 真的 useLatestLoad（抠自源码，语义与页面上那一份一致）。 */
function latestLoad(key) {
  const factory = new Function('ref', 'isCanceled', `
    const STALE = Symbol('stale-response')
    ${grabLatestLoad()}
    return useLatestLoad`)
  // 拿到的是**真的** useLatestLoad 的返回值（守卫的语义与页面上那一份一致）
  return factory(ref, () => false)(key)
}

/** 三个视图各自的 load()：依赖全由外面喂进去。 */
function harness(view, over = {}) {
  const said = []
  const deps = {
    store: { bookId: 7, periodId: 3, periodsReady: true, periods: [] },
    templateId: ref(1),
    allTemplates: ref([{ id: 1, name: '资产负债表' }]),
    template: ref(null),
    data: ref({ rows: [], cols: [], errors: [] }),
    designMode: ref(false),
    dirty: {},
    rows: ref([]),
    total: ref(0),
    logs: ref([]),
    selection: ref([]),
    check: ref(null),
    letterOf: (n) => String.fromCharCode(64 + n),
    queryParams: () => ({ page: 1 }),
    loadCheck: async () => {},
    api: {
      render: async () => ({ rows: [], cols: [] }),
      queryVouchers: async () => ({ items: [], total: 0 }),
      auditLogs: async () => []
    },
    ...over
  }
  const src = grabFn('load', view)
  const factory = new Function('deps', `
    const store = deps.store
    const templateId = deps.templateId
    const allTemplates = deps.allTemplates
    const template = deps.template
    const data = deps.data
    const designMode = deps.designMode
    const dirty = deps.dirty
    const rows = deps.rows
    const total = deps.total
    const logs = deps.logs
    const selection = deps.selection
    const check = deps.check
    const letterOf = deps.letterOf
    const queryParams = deps.queryParams
    const loadCheck = deps.loadCheck
    const api = deps.api
    const run = deps.run
    const isStale = deps.isStale
    ${src}
    return load`)
  const load = factory(deps)
  return { load, deps, said }
}

console.log('== N3 三处加载失败不许渲染成「没有数据」 ==')

for (const [name, view, key, oldData] of [
  ['ReportView', REPORT, 'report/render', { rows: [{ id: 1 }], cols: [] }],
  ['VoucherList', LIST, 'voucher/list', { items: [{ id: 1 }], total: 1 }],
  ['AuditLog', AUDIT, 'system/audit', [{ id: 1 }]]
]) {
  t(`**行为**：${name} 加载失败 → 旧数据留在页面上 + 挂出错误，不抛 rejection`, async () => {
    const { loading, error: loadError, run, isStale } = latestLoad(key)
    const h = harness(view, {
      run, isStale,
      api: {
        render: async () => { throw new Error('Request failed with status code 500') },
        queryVouchers: async () => { throw new Error('Request failed with status code 500') },
        auditLogs: async () => { throw new Error('Request failed with status code 500') }
      }
    })
    // 先放一份旧数据在页面上
    if (name === 'ReportView') h.deps.data.value = oldData
    if (name === 'VoucherList') h.deps.rows.value = oldData.items
    if (name === 'AuditLog') h.deps.logs.value = oldData
    // **不许抛出去**：以前这里没人接 → 变成没人管的 promise rejection
    await h.load()
    assert.ok(loadError.value, '失败了却没有错误状态 —— 页面无法区分「失败」与「空」')
    assert.equal(loading.value, false, '结束后要收掉转圈')
    if (name === 'ReportView') {
      assert.deepEqual(h.deps.data.value, oldData, '失败把旧数据清空了 —— 看着像「这一期真的一笔都没有」')
    }
    if (name === 'VoucherList') {
      assert.deepEqual(h.deps.rows.value, oldData.items, '失败把旧数据清空了')
    }
    if (name === 'AuditLog') {
      assert.deepEqual(h.deps.logs.value, oldData, '失败把旧数据清空了')
    }
  })
}

for (const [name, view] of [
  ['ReportView', REPORT],
  ['VoucherList', LIST],
  ['AuditLog', AUDIT]
]) {
  t(`${name} 页面顶部有「加载失败」这条提示，不是渲染成空表`, () => {
    assert.match(view, /v-if="loadError"/, `${name} 没有 loadError 提示`)
    assert.match(view, /加载失败/, `${name} 的提示没说到加载失败`)
    assert.match(view, /error: loadError/, `${name} 没从 useLatestLoad 解构 error`)
  })
}

console.log('== 拦截器：HTTP 500 且响应体没有 detail 时不许一声不吭 ==')

t('**行为**：500 且没有 detail → 兜底弹一条（失败至少要有声音）', async () => {
  const said = []
  let rejected = null
  const http = {
    interceptors: {
      request: { use: () => {} },
      response: { use: (ok, bad) => { rejected = bad } }
    }
  }
  const factory = new Function('http', 'ElMessage', 'axios', `
    let clearSession = () => {}
    let pluginRejected = () => {}
    ${grabIsCanceled()}
    ${grabInterceptorCall()}
    return http`)
  const axios = { isCancel: () => false }
  factory(http, { error: (m) => said.push(m) }, axios)
  assert.ok(rejected, '没抠到响应拦截器')
  await rejected({
    response: { status: 500, data: {} },
    config: { url: '/vouchers' }
  }).catch((e) => { rejected = e })
  assert.equal(said.length, 1, `500 没有兜底提示（页面会「空表 + 零提示」）：${JSON.stringify(said)}`)
  assert.match(said[0], /服务器出错|失败/, `兜底提示没说实话：${said[0]}`)
})

t('**对称**：500 但响应体里**有** detail → 弹后端给的那句（不重复弹）', async () => {
  const said = []
  let rejected = null
  const http = {
    interceptors: {
      request: { use: () => {} },
      response: { use: (ok, bad) => { rejected = bad } }
    }
  }
  const factory = new Function('http', 'ElMessage', 'axios', `
    let clearSession = () => {}
    let pluginRejected = () => {}
    ${grabIsCanceled()}
    ${grabInterceptorCall()}
    return http`)
  const axios = { isCancel: () => false }
  factory(http, { error: (m) => said.push(m) }, axios)
  await rejected({
    response: { status: 500, data: { detail: '期间已结账，不能记账' } },
    config: { url: '/vouchers' }
  }).catch(() => {})
  assert.deepEqual(said, ['期间已结账，不能记账'],
    `后端给了明说的话就不该再叠一条兜底：${JSON.stringify(said)}`)
})

t('**对称**：被自己掐掉的那一趟（取消）**不弹**（那是用户自己放弃的）', async () => {
  const said = []
  let rejected = null
  const http = {
    interceptors: {
      request: { use: () => {} },
      response: { use: (ok, bad) => { rejected = bad } }
    }
  }
  const factory = new Function('http', 'ElMessage', 'axios', `
    let clearSession = () => {}
    let pluginRejected = () => {}
    ${grabIsCanceled()}
    ${grabInterceptorCall()}
    return http`)
  const axios = { isCancel: () => false }
  factory(http, { error: (m) => said.push(m) }, axios)
  await rejected({ name: 'CanceledError', code: 'ERR_CANCELED', config: {} }).catch(() => {})
  assert.deepEqual(said, [], `取消也弹了一条红字：${JSON.stringify(said)}`)
})

for (const [name, fn] of tests) {
  try {
    await fn()
    console.log('  OK   ' + name)
    pass++
  } catch (e) {
    console.log('  FAIL ' + name + '\n       ' + String(e.stack || e.message).split('\n').slice(0, 6).join('\n       '))
    fail++
  }
}

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
