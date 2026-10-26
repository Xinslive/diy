/**
 * 路由代码块预取（utils/routePrefetch.js）。
 *
 * 这件事的收益全在「点了不用等」上，所以要盯的就两件：
 *   1. 悬停真的把那一页的代码块拉下来了（不是调了个寂寞）；
 *   2. 只拉一次，而且只拉「还没加载过」的那些。
 *
 * 另外还钉死一条**前提**：路由组件全是 SFC（`vite build` 出来是对象）。
 * 预取靠「components[name] 是函数 ⇒ 它是加载函数」这个判据，
 * 哪天有人写了函数式组件当路由组件，这个判据就会把组件当成加载函数去调。
 * 所以下面那条「所有路由都是惰性 import」不是装饰，是这条机制的地基。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
import { prefetchRouteChunk } from '../src/utils/routePrefetch.js'

const SRC = fileURLToPath(new URL('../src/', import.meta.url))
const ROUTER_SRC = fs.readFileSync(new URL('../src/router/index.js', import.meta.url), 'utf8')
const LAYOUT_SRC = fs.readFileSync(new URL('../src/layouts/MainLayout.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => { try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }
const ta = async (name, fn) => {
  try { await fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

/** 造一个只有一条惰性路由的假 router。 */
const fakeRouter = (loader, name = 'vouchers') => ({
  getRoutes: () => [{ name, components: { default: loader } }]
})

// ---- 预取本身 ----
await ta('悬停一次就把那一页的代码块拉下来', async () => {
  let called = 0
  const router = fakeRouter(() => { called++; return Promise.resolve({}) }, 'vouchers')
  await prefetchRouteChunk(router, 'vouchers')
  assert.equal(called, 1, `加载函数被调了 ${called} 次`)
})

await ta('重复悬停不会重复拉（同一个页面只下一次）', async () => {
  let called = 0
  const router = fakeRouter(() => { called++; return Promise.resolve({}) }, 'balance')
  await prefetchRouteChunk(router, 'balance')
  await prefetchRouteChunk(router, 'balance')
  await prefetchRouteChunk(router, 'balance')
  assert.equal(called, 1, `加载函数被调了 ${called} 次`)
})

await ta('已经加载过的页面不再拉（components 里是组件对象，不是函数）', async () => {
  let called = 0
  const router = { getRoutes: () => [{ name: 'subjects', components: { default: { name: 'V' } } }] }
  const r = prefetchRouteChunk(router, 'subjects')
  assert.equal(r, null, '已经加载过的页面不该再返回预取任务')
  assert.equal(called, 0)
})

await ta('路由不存在时安静返回 null，不抛错', async () => {
  const router = { getRoutes: () => [{ name: 'books', components: { default: () => Promise.resolve({}) } }] }
  assert.equal(prefetchRouteChunk(router, 'no-such-route'), null)
})

await ta('预取失败不会炸到界面，而且下次悬停还能重试', async () => {
  let called = 0
  const router = fakeRouter(() => { called++; return Promise.reject(new Error('网络断了')) }, 'reports')
  await prefetchRouteChunk(router, 'reports')   // 不该 reject
  assert.equal(called, 1)
  await prefetchRouteChunk(router, 'reports')   // 失败过 → 缓存里删掉了 → 重试
  assert.equal(called, 2, `失败后没有重试（调了 ${called} 次）`)
})

await ta('没有 router / 没有路由名时不做事', async () => {
  assert.equal(prefetchRouteChunk(null, 'vouchers'), null)
  assert.equal(prefetchRouteChunk(fakeRouter(() => Promise.resolve({}), 'periods'), ''), null)
})

// ---- 前提：路由组件全是惰性 import ----
t('所有路由组件都是惰性 import（预取靠「函数 ⇒ 加载函数」这个判据）', () => {
  const comps = [...ROUTER_SRC.matchAll(/component:\s*(\S.*)$/gm)].map((m) => m[1].trim())
  assert.ok(comps.length > 10, `只找到 ${comps.length} 条路由，解析可能没生效`)
  const notLazy = comps.filter((c) => !/^\(\)\s*=>\s*import\(/.test(c))
  assert.deepEqual(notLazy, [], `有路由不是惰性 import：${notLazy.join(' | ')}`)
})

// ---- 接线：模板上真的挂了预取 ----
t('侧栏菜单项挂了悬停/聚焦预取', () => {
  assert.ok(LAYOUT_SRC.includes('@mouseenter="prefetch(it.name)"'),
    '侧栏菜单项没有挂 @mouseenter 预取 —— 机制还在，但没人触发')
  assert.ok(LAYOUT_SRC.includes('@focus="prefetch(it.name)"'),
    '侧栏菜单项没有挂 @focus 预取 —— 键盘用户享受不到')
})

t('顶部模块按钮挂了悬停/聚焦预取', () => {
  assert.ok(LAYOUT_SRC.includes('@mouseenter="prefetch(landingOf(m))"'),
    '顶部模块按钮没有挂预取')
  assert.ok(LAYOUT_SRC.includes('@focus="prefetch(landingOf(m))"'),
    '顶部模块按钮没有挂键盘聚焦预取')
})

t('prefetch() 走的是 utils/routePrefetch 里的实现', () => {
  assert.ok(LAYOUT_SRC.includes("import { prefetchRouteChunk } from '@/utils/routePrefetch'"),
    'MainLayout 没引 routePrefetch')
  assert.ok(/function prefetch\(name\)\s*\{\s*prefetchRouteChunk\(router, name\)\s*\}/.test(LAYOUT_SRC),
    'prefetch() 没有调用 prefetchRouteChunk')
})

process.exit(fail ? 1 : 0)
