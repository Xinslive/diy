/**
 * 导航外壳的三块纯逻辑：路由归属映射（menuKeyOf）、菜单过滤（visibleNav）与
 * 模块落地页（landingOf），加上标签页开关（tabs store）。
 *
 * 关标签的「关完之后该去哪」是最容易写错的地方 —— 关的不是当前标签时不能乱跳，
 * 关的是当前标签时又必须有人接盘，否则页面停在一条已经不存在的标签上。
 * 这里把这几种情形都钉死。
 *
 * 用 vite 的 ssrLoadModule 加载**线上那份源码**，不复制逻辑（零新依赖）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createPinia, setActivePinia } from 'pinia'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const server = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
})
const { landingOf, menuKeyOf, visibleNav, NAV } = await server.ssrLoadModule('/src/router/nav.js')
const { useTabsStore } = await server.ssrLoadModule('/src/stores/tabs.js')

let pass = 0, fail = 0
const t = (name, fn) => { try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }

const R = (name, path = '/x', title = name) => ({ name, path, meta: { title } })
const fresh = () => { setActivePinia(createPinia()); return useTabsStore() }
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
t('管理员侧栏是 12 项（整理凭证 / 凭证导入 标了 hidden 不进侧栏）', () => {
  const names = visibleNav(true).flatMap(m => m.items.map(i => i.name))
  assert.equal(names.length, 12, `实际 ${names.length}`)
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

console.log(`\n通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
