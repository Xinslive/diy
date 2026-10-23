/**
 * 审查 P1-2：凭证列表的分页 / 排序没有竞态守卫。
 *
 * 分页器的 `@current-change` / `@size-change` 与表头的 `toggleSort` 都能**连点**。
 * 连点第 3、4 页 → 请求 A/B 并发 → B 先回、A 后回 →
 * **页脚停在第 4 页、表格里却是第 3 页的数据**（没有任何报错）。
 *
 * 修法与账簿三视图同源（见 api/ledger.js 的注释）：
 *   ① api 层：queryVouchers 改走 getLatest('voucher/list', ...) —— 掐掉上一趟；
 *   ② 页面层：load() 接入 useLatestLoad('voucher/list') —— 代次守卫，
 *      挡住「abort 到达之前就已经 resolve」的那一次。
 * 两道防线缺一不可。
 *
 * 这里测的是**行为**，不是文本：
 *   · 真的把两趟并发跑一遍，靠后回来那一趟**不许**覆盖表格；
 *   · api 层真的发出的是 getLatest（带 key），不是普通 http.get。
 * 页面那一段从**真实源码**里抠出来跑（与 voucher_balance.test.mjs 同一个路子）。
 */
import { createServer } from 'vite'
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/VoucherList.vue', import.meta.url), 'utf8')

/**
 * api/client 的**桩**：只记调用，不发请求。
 *
 * 记到 globalThis 上 —— ssrLoadModule 里的模块实例与测试文件里的不是同一个，
 * 认全局最省事（也让断言读起来就是「发了什么」）。
 */
const CLIENT_STUB = `
  const rec = (...a) => { globalThis.__voucherApiCalls.push(a) }
  export const http = {
    get: (url, opt) => { rec('get', url, opt); return Promise.resolve({ data: 'plain' }) },
    post: () => Promise.resolve({ data: 'plain' }),
    put: () => Promise.resolve({ data: 'plain' }),
    delete: () => Promise.resolve({ data: 'plain' })
  }
  export const apiUrl = (p) => '/api' + p
  export function getLatest(key, url, params) {
    rec('getLatest', key, url, params)
    return Promise.resolve({ items: [], total: 0 })
  }
`

/**
 * 一个 vite server 干两件事：
 *   · 加载页面用的守卫（composables/useLatestLoad.js，线上那份源码）；
 *   · 加载 api/voucher.js，把它的 './client' 换成上面的桩。
 * 只拦 api/voucher.js 那一个调用点 —— useLatestLoad 用的仍是**真的** client。
 */
const server = await createServer({
  root: ROOT, logLevel: 'error',
  server: { middlewareMode: true },
  // 依赖预扫描会在关闭时抛「server is being closed」——测试里没人要预打包
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'stub-api-client',
    // enforce: 'pre' —— vite 自己的 resolve 插件是核心插件，跑在普通插件**之前**，
    // 不抢在它前面的话 './client' 早就被解析成真模块了，桩根本轮不上。
    enforce: 'pre',
    resolveId(source, importer) {
      if (source === './client' && /api[\\/]voucher\.js$/.test(importer || '')) return '\0stub-client'
      return null
    },
    load(id) { return id === '\0stub-client' ? CLIENT_STUB : null },
  }],
})
const { useLatestLoad } = await server.ssrLoadModule('/src/composables/useLatestLoad.js')
const voucherApi = await server.ssrLoadModule('/src/api/voucher.js')
// vite 的 dev server 攥着端口与文件监听，不关掉的话 node:test 永远等不到进程退出
after(() => server.close())

/**
 * 抠出 `function <name>(...) {...}` 的整段源码（按大括号配平）。
 * 抠完立刻自检：抠错了要让断言失败，别让测试空转。
 */
function grabFn(name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(VIEW)
  assert.ok(m, `源码里找不到 function ${name}(`)
  const i = m.index
  let depth = 0, j = VIEW.indexOf('(', i)
  for (; j < VIEW.length; j++) {
    if (VIEW[j] === '(') depth++
    else if (VIEW[j] === ')') { depth--; if (depth === 0) break }
  }
  assert.equal(VIEW[j], ')', `${name} 的参数表没配平`)
  const bodyStart = VIEW.indexOf('{', j)
  assert.ok(bodyStart !== -1, `${name} 找不到函数体`)
  depth = 0
  for (j = bodyStart; j < VIEW.length; j++) {
    if (VIEW[j] === '{') depth++
    else if (VIEW[j] === '}') { depth--; if (depth === 0) break }
  }
  const src = VIEW.slice(i, j + 1)
  assert.ok(src.endsWith('}'), `${name} 抠出来的不是一段完整的函数`)
  return src
}

const LOAD_SRC = grabFn('load')

/** 用真源码 + 真守卫搭一套 load()；api 与 store 由测试喂进去。 */
function buildLoad({ run, isStale, api, rows, total, checks = [] }) {
  const factory = new Function('deps', `
    const store = deps.store
    const queryParams = deps.queryParams
    const run = deps.run
    const isStale = deps.isStale
    const api = deps.api
    const rows = deps.rows
    const total = deps.total
    const loadCheck = deps.loadCheck
    ${LOAD_SRC}
    return load
  `)
  return factory({
    store: { bookId: 7, periodsReady: true },
    queryParams: () => ({ page: 3 }),
    run, isStale, api, rows, total,
    loadCheck: () => checks.push('check'),
  })
}

/** 一个「resolve 由测试决定」的 api 假实现。 */
function deferredApi() {
  const pending = []
  return {
    pending,
    api: {
      queryVouchers: (bookId, params) => new Promise((resolve) => {
        pending.push(() => resolve({ items: [{ id: params.page }], total: params.page }))
      }),
    },
  }
}

test('load() 真的接上了代次守卫（抠出来的就是页面上那一段）', () => {
  assert.match(LOAD_SRC, /await run\(/, 'load() 没有走 run() —— 代次守卫没接上')
  assert.match(LOAD_SRC, /if \(isStale\(d\)\) return/, 'load() 没有认过期的那一趟')
  assert.match(VIEW, /useLatestLoad\('voucher\/list'\)/,
    "守卫的 key 不是 'voucher/list' —— 与 api 层对不上，两道防线就掐不到同一把槽位")
})

test('**行为**：两趟并发，先发后回的那一趟不许覆盖表格', async () => {
  const { run, isStale } = useLatestLoad('voucher/list')
  const rows = { value: [] }
  const total = { value: 0 }
  const { api, pending } = deferredApi()
  const load = buildLoad({ run, isStale, api, rows, total })

  // 连点第 3 页、第 4 页：两趟并发
  const p3 = load()
  const p4 = load()
  assert.equal(pending.length, 2, '两趟请求没有并发发出去 —— 测的不是竞态')

  // 第 4 页先回
  pending[1]()
  await p4
  assert.deepEqual(rows.value, [{ id: 3 }], '第 4 页那一趟没写进表格')

  // 第 3 页后回 —— **它必须被丢掉**
  pending[0]()
  await p3
  assert.deepEqual(rows.value, [{ id: 3 }],
    '先发后回的那一趟覆盖了表格 —— 页脚停在第 4 页、表格里是第 3 页的数据')
  assert.equal(total.value, 3)
})

test('后发的那一趟照样写进表格（守卫不是「一律不写」）', async () => {
  const { run, isStale } = useLatestLoad('voucher/list')
  const rows = { value: [] }
  const total = { value: 0 }
  const { api, pending } = deferredApi()
  const load = buildLoad({ run, isStale, api, rows, total })

  const p = load()
  pending[0]()
  await p
  assert.deepEqual(rows.value, [{ id: 3 }], '只有一趟时反而不写了')
  assert.equal(total.value, 3)
})

test('api 层：queryVouchers 发的是 getLatest("voucher/list", …)，不是普通 http.get', async () => {
  // 测的是「真的发了 getLatest」，而不是源码里有没有这个词。
  globalThis.__voucherApiCalls = []
  const data = await voucherApi.queryVouchers(7, { page: 4, page_size: 50 })
  assert.deepEqual(data, { items: [], total: 0 }, 'queryVouchers 没有把 getLatest 的结果原样返回')
  assert.deepEqual(globalThis.__voucherApiCalls,
    [['getLatest', 'voucher/list', '/books/7/vouchers', { page: 4, page_size: 50 }]],
    `api 层没有走 getLatest，或 key 不对：${JSON.stringify(globalThis.__voucherApiCalls)}`)
  delete globalThis.__voucherApiCalls
})
