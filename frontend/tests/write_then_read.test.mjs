/**
 * 「写完立刻读」必须读到**写之后**的数据 —— 这一条被应用级缓存咬过一次。
 *
 * **背景（2026-10-28，CI run 40695475118）**：
 *
 *   提交 ``perf: add app-wide cached query loading`` 给前端加了应用级查询缓存
 *   （``src/data/queryCache.js`` + ``src/api/client.js`` 里的 ``http.get`` 包装）。
 *   同一个提交里，CI 的「签名插件安装与授权浏览器演练」直接红了：
 *   装完扩展，扩展管理页的列表还是空的（截图里写���「尚未安装扩展」），
 *   演练脚本等 ``com.cwq.periodnote`` 那一行等了 30 秒超时。
 *
 *   根因有两条，两条都在这个文件里钉住：
 *
 *   ① **管理端（/system/*）被缓存了，而写操作作废不到它**。
 *      ``canCacheGet()`` 本来就排除了 /api/plugins、/api/auth 这些「必须新鲜」的接口，
 *      但漏了 ``/api/system/plugins`` —— 管理端整个命名空间都落进了缓存。
 *      而 ``invalidationFor()`` 只认业务标签（vouchers/ledger/…），
 *      写到 ``/system/plugins/install`` 只会作废 'audit' 标签的条目，
 *      扩展列表这条的标签是 'resource:plugins' —— **两边对不上**。
 *      于是写完之后读到的还是写之前的值，要等 staleTime（15s）过期才恢复，
 *      而扩展管理页 15 秒轮询一次 → 演练脚本那 30 秒里它根本没机会刷新。
 *
 *   ② **作废（invalidated）和过期（stale）被当成同一件事**。
 *      写操作会把相关条目标记成 invalidated，但读的时候只看了 stale，
 *      于是「作废后的第一趟读」照样先给旧值、后台再刷新。
 *      视图层用的是裸 ``http.get`` / ``getLatest``，**没有订阅这个缓存**
 *      （``useCachedQuery`` 那套订阅机制实际上没有页面在用），
 *      后台刷新把缓存更新了也没人通知界面 —— 界面就一直停在写之前的状态。
 *      同一个毛病在业务页上更明显：**凭证存好了，回到列表却看不见刚存的那张**。
 *
 * 所以这里守三件事：
 *   · 管理端的写后读能读到新值；
 *   · 业务端的写后读能读到新值（http.get 与 getLatest 两条路都要）；
 *   · **缓存本身还得有效** —— 同样的 GET 连着问两次，只该发一趟网络请求
 *     （这次改动的目的是性能，不能顺手把缓存整个关掉）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'

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
const { http, getLatest } = await vite.ssrLoadModule('/src/api/client.js')
const { default: queryCache } = await vite.ssrLoadModule('/src/data/queryCache.js')

let pass = 0, fail = 0
const t = async (name, fn) => {
  try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message.split('\n')[0]); fail++ }
}

/**
 * 假后端：所有写操作把「版本」加一，所有读操作回当前版本。
 * 于是「读到的版本有没有变」就是「界面有没有停在写之前」的直接判据。
 */
function stubBackend() {
  const calls = []
  let version = 1
  const real = http.defaults.adapter
  http.defaults.adapter = (config) => {
    const method = String(config.method || 'get').toLowerCase()
    calls.push(`${method.toUpperCase()} ${config.url}`)
    if (method === 'get') {
      return Promise.resolve({ data: { version, rows: [{ id: version }] }, status: 200, statusText: 'OK', headers: {}, config })
    }
    version += 1
    return Promise.resolve({ data: { ok: true, version }, status: 200, statusText: 'OK', headers: {}, config })
  }
  return {
    calls,
    get version() { return version },
    countGets(path) { return calls.filter(c => c === `GET ${path}`).length },
    restore() { http.defaults.adapter = real }
  }
}

function freshContext() {
  queryCache._resetForTests()
  localStorage._m = {}
  localStorage.setItem('cwq_token', 'tok-admin')
  localStorage.setItem('cwq_user', JSON.stringify({ id: 1, username: 'admin' }))
  localStorage.setItem('cwq_book', '11')
}

console.log('== 管理端（/system/*）：写完立刻读 ==')

await t('装完扩展，扩展列表立刻能读到（当年 CI 红的就是这一条）', async () => {
  freshContext()
  const be = stubBackend()
  try {
    await http.get('/system/plugins')
    await http.post('/system/plugins/install', { name: 'x.cwp' })
    const after = await http.get('/system/plugins')
    assert.notEqual(after.data.version, 1,
      '装完扩展读到的还是装之前的列表 —— 界面上仍然是「尚未安装扩展」')
    assert.ok(be.countGets('/system/plugins') >= 2,
      '装完之后压根没有重新发请求，读的是缓存里的旧值')
  } finally { be.restore() }
})

await t('管理端**不进**应用级缓存（写完不用等 15s 过期才恢复）', async () => {
  freshContext()
  const be = stubBackend()
  try {
    await http.get('/system/plugins')
    await http.get('/system/plugins')
    assert.equal(be.countGets('/system/plugins'), 2,
      '/system/* 被缓存了 —— 写操作作废不到它，要等 staleTime 过期才恢复')
  } finally { be.restore() }
})

console.log('')
console.log('== 业务端：写完立刻读 ==')

await t('http.get：写完之后的第一趟读拿到的是新值', async () => {
  freshContext()
  const be = stubBackend()
  try {
    const before = await http.get('/books/11/vouchers')
    await http.post('/books/11/vouchers', { x: 1 })
    const after = await http.get('/books/11/vouchers')
    assert.notEqual(after.data.version, before.data.version,
      '保存凭证后回到列表，列表还是旧的 —— 刚存的那张看不见')
  } finally { be.restore() }
})

await t('getLatest：写完之后的第一趟拿到的是新值（凭证页拉行走的就是它）', async () => {
  freshContext()
  const be = stubBackend()
  try {
    const before = await getLatest('voucher/list', '/books/11/vouchers', { page: 1 })
    await http.post('/books/11/vouchers', { x: 1 })
    const after = await getLatest('voucher/list', '/books/11/vouchers', { page: 1 })
    assert.notEqual(after.version, before.version,
      'getLatest 先给了旧值 —— 凭证页存完凭证回到列表会看不见新凭证')
  } finally { be.restore() }
})

await t('流水行被忽略之后，明细页立刻读到新状态', async () => {
  freshContext()
  const be = stubBackend()
  try {
    const before = await http.get('/books/11/bank-statements/2/rows')
    await http.post('/books/11/bank-statements/2/rows/99/ignore', { x: 1 })
    const after = await http.get('/books/11/bank-statements/2/rows')
    assert.notEqual(after.data.version, before.data.version,
      '忽略一行之后明细还是旧状态')
  } finally { be.restore() }
})

console.log('')
console.log('== MCP 密钥（个人资料页那一块）：写完立刻读 ==')

// 这一路与管理端是同一个形态：生成一枚、吊销一枚之后要立刻看到新的那一屏。
// **它的坑与管理端一模一样** —— 作废标签与缓存键的标签对不上
// （写接口落到兜底的 ['audit']，而这一路的缓存键标签是 resource:mcp-tokens），
// 于是写完之后读到的还是写之前的值，要等 staleTime 过期才恢复。

await t('生成一枚密钥之后，密钥列表立刻读到新值', async () => {
  freshContext()
  const be = stubBackend()
  try {
    await http.get('/mcp-tokens')
    await http.post('/mcp-tokens', { name: 'x', current_password: 'y' })
    const after = await http.get('/mcp-tokens')
    assert.notEqual(after.data.version, 1,
      '生成密钥后读到的还是生成之前的列表 —— 界面上看不见刚生成的那一枚')
  } finally { be.restore() }
})

await t('MCP 密钥这一路**不进**应用级缓存', async () => {
  freshContext()
  const be = stubBackend()
  try {
    await http.get('/mcp-tokens')
    await http.get('/mcp-tokens')
    assert.equal(be.countGets('/mcp-tokens'), 2,
      '/mcp-tokens 被缓存了 —— 写操作作废不到它，要等 staleTime 过期才恢复')
  } finally { be.restore() }
})

console.log('')
console.log('== 缓存本身还得有效（这次改动的目的是性能，不能顺手把缓存关掉） ==')

await t('同样的 GET 连着问两次，只发一趟网络请求', async () => {
  freshContext()
  const be = stubBackend()
  try {
    await http.get('/books/11/vouchers')
    await http.get('/books/11/vouchers')
    assert.equal(be.countGets('/books/11/vouchers'), 1,
      '业务端的缓存没了 —— 这次改动本来是要省往返的')
  } finally { be.restore() }
})

await t('作废之后的第一趟读回网络，**之后**又回到缓存', async () => {
  freshContext()
  const be = stubBackend()
  try {
    await http.get('/books/11/vouchers')
    await http.post('/books/11/vouchers', { x: 1 })
    await http.get('/books/11/vouchers')   // 作废后的第一趟：回网络
    const after = be.countGets('/books/11/vouchers')
    await http.get('/books/11/vouchers')   // 之后：走缓存
    assert.equal(be.countGets('/books/11/vouchers'), after,
      '作废后没有恢复缓存 —— 每读一次都白发一趟网络')
  } finally { be.restore() }
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
await vite.close()
process.exit(fail === 0 ? 0 : 1)
