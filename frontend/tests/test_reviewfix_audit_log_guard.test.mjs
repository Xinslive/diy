/**
 * 审查 P2-6：页内加载点的代次守卫样板（views/system/AuditLog.vue）。
 *
 * 19 个视图里 15 个的异步加载点没有显式守卫，但**多数不是活跃 bug** ——
 * MainLayout 的 viewKey（`${route.name}-${store.bookId}-${scope}-${refreshKey}`）
 * 让绝大多数「切账套 / 切期间」场景整页重挂，旧组件的响应写进已销毁的实例，
 * 用户无感。
 *
 * **但页内的动作不受重挂保护**：翻页、改页大小、排序、页内筛选 ——
 * 这些都发生在**同一个还活着的组件实例**里，旧响应会真的写进页面。
 * AuditLog 这一页就是这样的一个点（切账套 + 组件自己的加载可能撞在一起），
 * 拿它当「页内加载点」的样板接上 useLatestLoad。
 *
 * 守卫的完整清单写在 composables/useLatestLoad.js 的文件头 ——
 * 那里说明了「哪些接了、哪些靠重挂兜底、页内这条边界在哪」。
 *
 * 这里测的是**行为**：两趟并发，先发后回的那一趟不许覆盖表格。
 */
import { createServer } from 'vite'
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const VIEW = fs.readFileSync(
  new URL('../src/views/system/AuditLog.vue', import.meta.url), 'utf8')

const server = await createServer({
  root: ROOT, logLevel: 'error',
  server: { middlewareMode: true },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { useLatestLoad } = await server.ssrLoadModule('/src/composables/useLatestLoad.js')
after(() => server.close())

/** 抠出 `function <name>(...) {...}` 的整段源码（按大括号配平）。 */
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

test('这一页接上了守卫，key 是 system/audit', () => {
  assert.match(VIEW, /useLatestLoad\('system\/audit'\)/,
    '守卫的 key 不对 —— 与 useLatestLoad 文件头那份清单会对不上')
  assert.match(LOAD_SRC, /await run\(/, 'load() 没有走 run()')
  assert.match(LOAD_SRC, /if \(isStale\(d\)\) return/, 'load() 没有认过期的那一趟')
})

test('**行为**：两趟并发，先发后回的那一趟不许覆盖表格', async () => {
  const { run, isStale } = useLatestLoad('system/audit')
  const logs = { value: [] }
  const pending = []
  const api = {
    auditLogs: (bookId, limit) => new Promise((resolve) => {
      pending.push(() => resolve([{ id: limit === 1 ? 'old' : 'new', username: 'u' }]))
    }),
  }
  const factory = new Function('deps', `
    const store = deps.store
    const run = deps.run
    const isStale = deps.isStale
    const api = deps.api
    const logs = deps.logs
    ${LOAD_SRC}
    return load
  `)
  const load = factory({ store: { bookId: 7 }, run, isStale, api, logs })

  const p1 = load()
  const p2 = load()
  assert.equal(pending.length, 2, '两趟请求没有并发发出去 —— 测的不是竞态')

  pending[1]()
  await p2
  assert.equal(logs.value[0]?.id, 'new', '后发的那一趟没写进表格')

  pending[0]()
  await p1
  assert.equal(logs.value[0]?.id, 'new',
    '先发后回的那一趟覆盖了表格 —— 页面上是另一批日志')
})

test('没有账套就不发请求', async () => {
  const { run, isStale } = useLatestLoad('system/audit')
  const logs = { value: [] }
  let called = 0
  const factory = new Function('deps', `
    const store = deps.store
    const run = deps.run
    const isStale = deps.isStale
    const api = deps.api
    const logs = deps.logs
    ${LOAD_SRC}
    return load
  `)
  const load = factory({
    store: { bookId: 0 }, run, isStale,
    api: { auditLogs: async () => { called++; return [] } },
    logs,
  })
  await load()
  assert.equal(called, 0, '没有账套也去问后端')
})
