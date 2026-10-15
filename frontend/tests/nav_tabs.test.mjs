/**
 * 导航外壳的三块纯逻辑：路由归属映射（menuKeyOf）、菜单过滤（visibleNav）与
 * 模块落地页（landingOf），加上标签页开关与**持久化**（tabs store）。
 *
 * 关标签的「关完之后该去哪」是最容易写错的地方 —— 关的不是当前标签时不能乱跳，
 * 关的是当前标签时又必须有人接盘，否则页面停在一条已经不存在的标签上。
 * 持久化那一组盯的是另一件事：**只存标签结构，一个令牌都不许带出去**。
 *
 * 用 vite 的 ssrLoadModule 加载**线上那份源码**，不复制逻辑（零新依赖）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, reactive, watch } from 'vue'

// tabs store 要读写 localStorage（持久化），必须先装好再加载
globalThis.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] ?? null },
  setItem(k, v) { this._m[k] = String(v) },
  removeItem(k) { delete this._m[k] }
}
const TABS_KEY = 'cwq_tabs'
const setUser = (id) => localStorage.setItem('cwq_user', JSON.stringify({ id, username: `u${id}` }))

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const server = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
})
const { landingOf, menuKeyOf, visibleNav, NAV } = await server.ssrLoadModule('/src/router/nav.js')
const { useTabsStore } = await server.ssrLoadModule('/src/stores/tabs.js')

let pass = 0, fail = 0
const t = (name, fn) => { try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }
/** 异步版用例（watcher 那几条要等 nextTick）。用法与 t() 一样，直接 await。 */
const ta = async (name, fn) => {
  try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

const R = (name, path = '/x', title = name) => ({ name, path, meta: { title } })
/** 重新载入页面：新建一个 pinia，store 才会真去读一次存储。
 *  同一个 pinia 里再调 useTabsStore() 拿到的还是老实例（state 已初始化），
 *  拿它去验「重开浏览器」会验成空欢喜 —— 那条要测的就是**重新读一次**。 */
const reload = () => { setActivePinia(createPinia()); return useTabsStore() }
/** 一个干净的 store：把持久化那份也一起清掉，否则上一条用例的标签会渗进来 */
const fresh = () => {
  localStorage.removeItem(TABS_KEY)
  return reload()
}
const keys = (s) => s.opened.map((x) => x.key)

// ---- 菜单结构 ----
t('菜单里没有重复的 key（重复会让两个菜单项指向同一页）', () => {
  const names = NAV.flatMap(m => m.items.map(i => i.name))
  assert.equal(new Set(names).size, names.length, `重复：${names.join(',')}`)
})
t('每个菜单项都有标题和图标，否则侧栏会渲染出空行', () => {
  for (const m of NAV) for (const i of m.items) {
    assert.ok(i.title, `${m.key}/${i.name} 缺标题`)
    assert.ok(i.icon, `${m.key}/${i.name} 缺图标`)
  }
})
t('模块也有标题和图标，顶栏少了就渲染出一个空按钮', () => {
  for (const m of NAV) { assert.ok(m.title && m.icon && m.items.length, m.key) }
})
t('模块图标不跟自己第一个菜单项撞图', () => {
  // 收起成图标带之后，模块图标紧挨着第一个菜单图标。撞图就分不出谁是谁。
  for (const m of NAV) {
    const first = m.items[0]
    assert.notEqual(m.icon, first.icon,
      `${m.title} 和第一个菜单 ${first.title} 用了同一个图标`)
  }
})
t('非管理员看不到「操作日志 / 用户与授权」', () => {
  const n = visibleNav(false)
  const names = n.flatMap(m => m.items.map(i => i.name))
  assert.ok(!names.includes('audit') && !names.includes('users'))
})
t('管理员侧栏是 14 项（整理凭证 / 凭证导入 标了 hidden 不进侧栏）', () => {
  const names = visibleNav(true).flatMap(m => m.items.map(i => i.name))
  assert.equal(names.length, 14, `实际 ${names.length}`)
  assert.ok(!names.includes('voucher-organize'), '整理凭证又冒进侧栏了')
  assert.ok(!names.includes('voucher-import'), '凭证导入又冒进侧栏了')
})
t('hidden 的项仍留在 NAV 里，标签页标题才认得出', () => {
  // 侧栏看不见 ≠ 页面没了：直接敲地址要能进，标签页也得叫得出名字
  const hidden = NAV.flatMap(m => m.items).filter(i => i.hidden)
  assert.deepEqual(hidden.map(i => i.name).sort(), ['voucher-import', 'voucher-organize'])
  for (const i of hidden) assert.ok(i.title, `${i.name} 缺标题`)
})

// ---- 模块落地页 ----
t('每个模块的落地页都在自己的可见菜单里', () => {
  for (const m of visibleNav(true)) {
    const land = landingOf(m)
    assert.ok(m.items.some(i => i.name === land), `${m.key} 的落地页 ${land} 不在菜单里`)
  }
})
t('点顶部模块落到指定页：凭证→凭证、账簿报表→明细账、基础资料→账套管理', () => {
  const find = (key) => visibleNav(true).find(m => m.key === key)
  assert.equal(landingOf(find('voucher')), 'vouchers')
  assert.equal(landingOf(find('ledger')), 'detail-ledger')
  assert.equal(landingOf(find('master')), 'books')
})
t('没写 landing 的模块取第一项（系统管理 → 操作日志）', () => {
  const sys = visibleNav(true).find(m => m.key === 'system')
  assert.equal(landingOf(sys), 'audit')
})

// ---- 落地页在侧栏里的位置 ----
t('每个模块的落地页就是它侧栏的第一项', () => {
  // 点顶部模块落到的那一页，必须一眼能在侧栏里找到 —— 排第三就得先扫一遍。
  // 顺序由 visibleNav 保证，不靠手工把 NAV 的数组重排一次：以后再往落地页
  // 前面插一项，顺序会漂回去，而这里会红。
  for (const m of visibleNav(true)) {
    assert.equal(m.items[0].name, landingOf(m),
      `${m.key}（${m.title}）的落地页 ${landingOf(m)} 排在第 ${m.items.findIndex(i => i.name === landingOf(m)) + 1} 位，侧栏顺序：${m.items.map(i => i.title).join(' / ')}`)
  }
})
t('顶到前面只动落地页，其余项的相对顺序不变', () => {
  const raw = NAV.find(m => m.key === 'ledger')
  const shown = visibleNav(true).find(m => m.key === 'ledger')
  const rest = shown.items.slice(1).map(i => i.name)
  assert.equal(shown.items[0].name, 'detail-ledger', '明细账是账簿报表的落地页')
  assert.deepEqual(rest, raw.items.filter(i => i.name !== 'detail-ledger').map(i => i.name),
    '科目余额表 / 总账 / 报表 的先后不该被顶栏调整打乱')
})
t('落地页被过滤掉时退到第一项，且那一项就在第一位', () => {
  // landing 指向的项若因权限/隐藏不在菜单里，不能顶起一个不存在的东西
  const shown = visibleNav(true).find(m => m.key === 'master')
  assert.equal(shown.items[0].name, 'books', '账套管理是基础资料的落地页')
  assert.ok(shown.items.some(i => i.name === landingOf(shown)))
})

// ---- 路由归属 ----
t('凭证列表 / 新增 / 详情共用「凭证」这一个入口', () => {
  assert.equal(menuKeyOf('vouchers'), 'vouchers')
  assert.equal(menuKeyOf('voucher-new'), 'vouchers')
  assert.equal(menuKeyOf('voucher-edit'), 'vouchers')
})
t('报表详情归到「报表」', () => {
  assert.equal(menuKeyOf('report-view'), 'reports')
})
t('没有映射关系的路由原样返回（别把未知路由吞成 undefined）', () => {
  assert.equal(menuKeyOf('balance'), 'balance')
})

// ---- 标签页 ----
t('切页面会开一条新标签', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.open(R('subjects', '/subjects'))
  assert.deepEqual(keys(s), ['balance', 'subjects'])
})
t('标题取自菜单，不是路由 path', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  assert.equal(s.opened[0].title, '科目余额表')
})
t('重复切同一个页面只激活，不新增', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.open(R('subjects', '/subjects'))
  s.open(R('balance', '/balance'))
  assert.deepEqual(keys(s), ['balance', 'subjects'])
})
t('在凭证详情之间来回翻，标签数不变', () => {
  const s = fresh()
  s.open(R('vouchers', '/vouchers'))
  s.open(R('voucher-edit', '/vouchers/1582'))
  s.open(R('voucher-edit', '/vouchers/1581'))
  s.open(R('vouchers', '/vouchers'))
  assert.deepEqual(keys(s), ['vouchers'])
  // 路径要跟着最后打开的那张走，否则点标签会回到旧凭证
  assert.equal(s.opened[0].path, '/vouchers')
})

t('关掉的不是当前标签：不跳转，也不误伤', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.open(R('subjects', '/subjects'))
  s.open(R('vouchers', '/vouchers'))
  const r = s.close('subjects', 'vouchers')     // 当前在 vouchers，关的是 subjects
  assert.equal(r.active, false)
  assert.equal(r.next, null, '不该返回要去的地方')
  assert.deepEqual(keys(s), ['balance', 'vouchers'])
})
t('关掉当前标签：后面顶上', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.open(R('subjects', '/subjects'))
  s.open(R('vouchers', '/vouchers'))
  const r = s.close('subjects', 'subjects')
  assert.equal(r.active, true)
  assert.equal(r.next, '/vouchers', '应就近接管后面那条')
  assert.deepEqual(keys(s), ['balance', 'vouchers'])
})
t('关掉当前标签：后面没有就退回前一条', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.open(R('subjects', '/subjects'))
  const r = s.close('subjects', 'subjects')
  assert.equal(r.next, '/balance')
  assert.deepEqual(keys(s), ['balance'])
})
t('关到只剩零条：交给调用方回首页', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  const r = s.close('balance', 'balance')
  assert.equal(r.active, true)
  assert.equal(r.next, '/')
  assert.equal(s.opened.length, 0)
})
t('关一条不存在的标签：不报错也不跳', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  const r = s.close('nope', 'balance')
  assert.deepEqual(r, { active: false, next: null })
})
t('关闭其他：当前这条留着', () => {
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.open(R('subjects', '/subjects'))
  s.open(R('vouchers', '/vouchers'))
  s.closeOthers('subjects')
  assert.deepEqual(keys(s), ['subjects'])
})
t('关闭全部：清空', () => {
  const s = fresh()
  s.open(R('balance')); s.open(R('subjects'))
  s.closeAll()
  assert.equal(s.opened.length, 0)
})
t('登出/重登之间 reset：下一个用户不该看见上一个用户开着的页面', () => {
  const s = fresh()
  s.open(R('balance')); s.open(R('subjects'))
  s.reset()
  assert.equal(s.opened.length, 0)
})
t('收侧栏状态是可切换的', () => {
  const s = fresh()
  assert.equal(s.sidebarCollapsed, false)
  s.toggleSidebar(); assert.equal(s.sidebarCollapsed, true)
  s.toggleSidebar(); assert.equal(s.sidebarCollapsed, false)
})
t('只有一条标签时不算「有上一页可关」', () => {
  const s = fresh()
  s.open(R('balance'))
  assert.equal(s.canClosePrev, false)
  s.open(R('subjects'))
  assert.equal(s.canClosePrev, true)
})


console.log('')
console.log('== P3-10 标签 path 跟住最后一页 + 持久化 ==')

t('自动保存把地址换了：标签记的必须是新地址（关掉再点回来才不是另一张空凭证）', () => {
  const s = fresh()
  s.open(R('voucher-new', '/vouchers/new'))
  // VoucherEdit 保存完只改地址栏、不走 router，所以它显式调一次 open
  s.open({ name: 'voucher-new', path: '/vouchers/1582' })
  assert.equal(s.opened.length, 1, '不该多出一条标签')
  assert.equal(s.opened[0].path, '/vouchers/1582')
})
t('path 不对的一律不认（拼错的、空的、协议外地址都不进标签栏）', () => {
  const s = fresh()
  s.open({ name: 'balance', path: '' })
  s.open({ name: 'balance', path: 'http://evil.example/balance' })
  s.open({ name: 'balance' })
  assert.equal(s.opened.length, 0)
})
t('关掉浏览器再打开：标签原样回来', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.open(R('voucher-edit', '/vouchers/1582'))
  const again = reload()                // 模拟重新载入页面：store 重新读一次存储
  assert.deepEqual(again.opened.map((x) => [x.key, x.path]),
    [['balance', '/balance'], ['vouchers', '/vouchers/1582']])
})
t('关标签 / 关闭其他 / 关闭全部 都同步到存储（不用等刷新才生效）', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.open(R('subjects', '/subjects'))
  s.closeOthers('subjects')
  assert.deepEqual(JSON.parse(localStorage.getItem(TABS_KEY)).opened.map((x) => x.key), ['subjects'])
  s.closeAll()
  assert.deepEqual(JSON.parse(localStorage.getItem(TABS_KEY)).opened, [])
})
t('登出 reset：存储里那份也一起清掉（下一个用户不该看见上一个开着的页面）', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  const s = fresh()
  s.open(R('balance', '/balance'))
  s.reset()
  assert.equal(s.opened.length, 0)
  assert.deepEqual(JSON.parse(localStorage.getItem(TABS_KEY)).opened, [], '存储里还留着上一个用户的标签')
  assert.equal(reload().opened.length, 0, '重新载入时标签又回来了')
})
t('换了个人登录：上一位存下来的标签一份都不恢复', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  const s = fresh()
  s.open(R('balance', '/balance'))
  setUser(9)                              // 同一个浏览器，换了个人登录
  assert.equal(reload().opened.length, 0)
})
t('**持久化里一个令牌都不能有**：只有 key/title/path 三个字段', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  localStorage.setItem('cwq_token', 'secret-token')
  const s = fresh()
  s.open(R('voucher-edit', '/vouchers/1582'))
  const blob = localStorage.getItem(TABS_KEY)
  assert.ok(!blob.includes('secret-token'), '令牌被带进标签结构了：' + blob)
  const parsed = JSON.parse(blob)
  for (const tab of parsed.opened) {
    assert.deepEqual(Object.keys(tab).sort(), ['key', 'path', 'title'],
      `一条标签里多带了字段：${Object.keys(tab).join(',')}`)
  }
})
t('存的是路径，不含 query（query 里可能夹着期间之类的上下文）', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  const s = fresh()
  s.open({ name: 'vouchers', path: '/vouchers?period_id=3', fullPath: '/vouchers?period_id=3&token=x' })
  assert.equal(s.opened[0].path, '/vouchers?period_id=3')
  assert.ok(!localStorage.getItem(TABS_KEY).includes('token=x'), 'query 被一起存下来了')
})
t('存了坏数据（不是 JSON / 版本不对 / 认不出的 key）：就地丢掉重开，不带崩界面', () => {
  localStorage.removeItem(TABS_KEY)
  setUser(7)
  localStorage.setItem(TABS_KEY, '{ 这不是 json')
  assert.equal(reload().opened.length, 0)
  localStorage.setItem(TABS_KEY, JSON.stringify({ version: 99, opened: [{ key: 'balance', title: 'x', path: '/balance' }] }))
  assert.equal(reload().opened.length, 0, '结构版本对不上就该重开，别去猜')
  localStorage.setItem(TABS_KEY, JSON.stringify({ version: 1, user_id: 7, opened: [{ key: 'no-such-menu', title: 'x', path: '/x' }] }))
  assert.equal(reload().opened.length, 0, '菜单里没有的 key 不该摆上标签栏')
})
t('存储整个不可用（隐私模式）：标签照常开，界面不带崩', () => {
  setActivePinia(createPinia())
  const real = globalThis.localStorage
  globalThis.localStorage = {
    getItem() { throw new Error('denied') },
    setItem() { throw new Error('denied') },
    removeItem() { throw new Error('denied') }
  }
  try {
    const s = useTabsStore()
    s.open(R('balance', '/balance'))
    s.open(R('subjects', '/subjects'))
    assert.deepEqual(keys(s), ['balance', 'subjects'])
    s.close('balance', 'subjects')
    assert.deepEqual(keys(s), ['subjects'])
  } finally {
    globalThis.localStorage = real
  }
})

console.log(`\n通过 ${pass} / 失败 ${fail}`)
console.log('')
console.log('== P3-10b MainLayout 的 watcher：同 name 换 path 也要通知标签 ==')

/**
 * 这一段盯的不是 tabs.open（它本来就一直认 path），而是**谁去调它**。
 *
 * 原来 MainLayout 里只 watch 了 `route.name`：凭证页的「上一张/下一张」
 * 走的是**同一个 route name、不同 path**（/vouchers/12 → /vouchers/13），
 * name 一个字没变，watcher 根本不触发，标签记的 path 就停在第一张 ——
 * 关掉标签再点回来会跳到**另一张凭证**，看着像丢数据。
 *
 * 照旧不写等价物：从**真实组件源码**里把「标签栏」那一节下面的那条 watch(...)
 * 连括号配平一起原样抠出来，配真实的 vue watch 和真实的 tabs store 跑。
 * 被测的就是源码里那条通知本身，所以这条用例红了只可能是真出问题了。
 */
const LAYOUT_SRC = fs.readFileSync(
  new URL('../src/layouts/MainLayout.vue', import.meta.url), 'utf8')

/**
 * 抠出 MainLayout 里那条「切路由就通知标签」的 watch(...)，并自检。
 *
 * 故意**不靠章节注释定位**：文件里「标签栏」这三个字出现了三次（模板、脚本、样式），
 * 按注释抠很容易抠到别处去，而抠错了下面那些断言照样会过 —— 空转比红更坏。
 * 所以这里扫出所有 watch(...)，挑那条真的在调 tabs.open 的，
 * 并且要求**有且只有一条**，多出来一条就说明通知的口子不止一处。
 */
function tabsWatcherFromSource() {
  const found = []
  for (let i = LAYOUT_SRC.indexOf('watch('); i !== -1; i = LAYOUT_SRC.indexOf('watch(', i + 1)) {
    let depth = 0
    let j = LAYOUT_SRC.indexOf('(', i)
    for (; j < LAYOUT_SRC.length; j++) {
      if (LAYOUT_SRC[j] === '(') depth++
      else if (LAYOUT_SRC[j] === ')') { depth--; if (depth === 0) break }
    }
    const src = LAYOUT_SRC.slice(i, j + 1)
    if (src.includes('tabs.open(')) found.push(src)
  }
  assert.equal(found.length, 1,
    `期望正好一条通知标签的 watch(...)，实际 ${found.length} 条：\n${found.join('\n')}`)
  return found[0]
}
const WATCHER_SRC = tabsWatcherFromSource()

/** 让真实 watcher 挂到一个可变的 route 上，返回 { route, tabs }。 */
function mountWatcher(initial) {
  const s = fresh()
  const route = reactive({ name: initial.name, path: initial.path, query: {} })
  // 源码里那条 watch(...) 就是被测对象：route / tabs / watch 全由外面喂进去
  new Function('watch', 'route', 'tabs', WATCHER_SRC)(watch, route, s)
  return { route, tabs: s }
}

await ta('翻到下一张凭证：标签记的 path 跟着换（原来停在第一张）', async () => {
  const { route, tabs } = mountWatcher({ name: 'voucher-edit', path: '/vouchers/12' })
  // immediate：进页面就该记下这一张
  assert.equal(tabs.opened.length, 1, '进凭证页没有开出标签')
  assert.equal(tabs.opened[0].path, '/vouchers/12')
  // 点「下一张」：route name 一个字没变，只有 path 变 —— 这正是原来漏掉的情形
  route.path = '/vouchers/13'
  await nextTick()
  assert.equal(tabs.opened.length, 1, '翻页不该多开一条标签')
  assert.equal(tabs.opened[0].path, '/vouchers/13',
    `标签记的还是 ${tabs.opened[0].path}，关掉再点回来会跳到另一张凭证`)
})

await ta('连着翻好几张：标签 path 始终是最后打开的那一张', async () => {
  const { route, tabs } = mountWatcher({ name: 'voucher-edit', path: '/vouchers/1581' })
  for (const id of [1582, 1583, 1581]) {
    route.path = `/vouchers/${id}`
    await nextTick()
    assert.equal(tabs.opened[0].path, `/vouchers/${id}`)
  }
  assert.equal(tabs.opened.length, 1)
})

await ta('换到别的页面：照旧新开一条标签（watcher 没被改瞎）', async () => {
  const { route, tabs } = mountWatcher({ name: 'voucher-edit', path: '/vouchers/12' })
  route.name = 'balance'; route.path = '/balance'
  await nextTick()
  assert.deepEqual(keys(tabs), ['vouchers', 'balance'])
})

await ta('只改 query 不动标签（标签存的是 path，query 不进存储）', async () => {
  const { route, tabs } = mountWatcher({ name: 'voucher-edit', path: '/vouchers/12' })
  const before = JSON.stringify(tabs.opened)
  route.query = { period_id: '3' }        // query 变了，path 没变
  await nextTick()
  assert.equal(JSON.stringify(tabs.opened), before, 'query 变化不该惊动标签')
  assert.equal(tabs.opened[0].path, '/vouchers/12')
})

await ta('重复通知同一个页面：不新增标签、也不重写存储', async () => {
  localStorage.setItem('cwq_user', JSON.stringify({ id: 3, username: 'u3' }))
  const { route, tabs } = mountWatcher({ name: 'voucher-edit', path: '/vouchers/12' })
  let writes = 0
  const real = localStorage.setItem.bind(localStorage)
  localStorage.setItem = (k, v) => { writes++; return real(k, v) }
  try {
    route.name = 'voucher-edit'; route.path = '/vouchers/12'   // 同 name 同 path
    await nextTick()
    assert.equal(tabs.opened.length, 1, '不该多出一条标签')
    assert.equal(writes, 0, `地址没变还写了 ${writes} 次存储 —— open() 不幂等，被高频调用会白刷`)
    // 但换个同 name 的 path 就必须写：这条才是真正要记下的变化
    route.path = '/vouchers/13'
    await nextTick()
    assert.equal(writes, 1, '地址真变了却没同步到存储')
  } finally {
    localStorage.setItem = real
  }
})

process.exit(fail ? 1 : 0)
