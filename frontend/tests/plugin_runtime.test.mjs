import test from 'node:test'
import assert from 'node:assert/strict'
import { createRouter, createMemoryHistory } from 'vue-router'
import { source, metadata } from './plugin_test_support.mjs'
const { createPluginRuntime } = await source('/src/plugins/runtime.js')
const component = { render: () => null }
function setup() {
  let payload = { plugins: [metadata()] }, loads = [], links = [], removed = []
  let hosts = []
  const session = { token: 'user-token', user: { id: 1 }, bookId: 1, books: [
    { id: 1, name: '现金账', company_name: '甲公司', status: 'ACTIVE', role: 'ACCOUNTANT' },
    { id: 2, name: '只读账', company_name: '乙公司', status: 'ACTIVE', role: 'VIEWER' },
  ] }
  const router = createRouter({ history: createMemoryHistory(), routes: [{ name: 'workspace', path: '/', component,
    children: [{ name: 'books', path: 'books', component }, { name: 'placeholder', path: 'plugin/:id', component }] }] })
  const entries = { value: [] }, tabs = { setPluginEntries: value => removed.push(value) }
  const document = { createElement: () => ({ remove() { this.removed = true } }), head: { appendChild: link => links.push(link) } }
  const runtime = createPluginRuntime({ router, session, tabs, entries, document,
    fetch: async () => { if (payload instanceof Error) throw payload; return payload },
    importer: async url => {
      loads.push(url)
      return { default: host => { hosts.push(host); return { render: () => host.h('div', 'loaded') } } }
    } })
  return { runtime, router, session, entries, loads, links, hosts, setPayload: p => payload = p }
}
test('授权路由可直接进入，module factory 加载同源资源；撤权移除路由、样式和入口', async () => {
  const s = setup(); await s.runtime.refresh()
  assert.equal(s.entries.value.length, 1)
  const route = s.router.getRoutes().find(r => r.name === 'plugin:com.test.demo:main')
  await route.components.default.__asyncLoader()
  assert.equal(s.loads.length, 1); assert.equal(s.links.length, 1)
  assert.deepEqual(s.hosts[0].books(), [
    { id: 1, name: '现金账', companyName: '甲公司', status: 'ACTIVE', role: 'ACCOUNTANT', canWrite: true },
    { id: 2, name: '只读账', companyName: '乙公司', status: 'ACTIVE', role: 'VIEWER', canWrite: false },
  ])
  assert.equal(await s.runtime.routeGuard({ path: '/plugin/com.test.demo', name: route.name }), true)
  s.setPayload({ plugins: [] }); await s.runtime.refresh()
  assert.equal(s.entries.value.length, 0); assert.ok(!s.router.hasRoute(route.name)); assert.ok(s.links[0].removed)
  assert.deepEqual(await s.runtime.routeGuard({ path: '/plugin/com.test.demo' }), { name: 'books' })
})
test('manifest 失败和重复 id 均关闭入口，静态宿主路由仍存在', async () => {
  const s = setup(); s.setPayload({ plugins: [metadata(), metadata()] }); await s.runtime.refresh(); assert.equal(s.entries.value.length, 0)
  s.setPayload({ plugins: [metadata()] }); await s.runtime.refresh(); s.setPayload(new Error('network')); await s.runtime.refresh()
  assert.equal(s.entries.value.length, 0); assert.ok(s.router.hasRoute('books'))
})
test('退出/切换用户后的过期 manifest 不能重新注册路由', async () => {
  const session = { token: 'old', user: { id: 1 } }, entries = { value: [] }
  const router = createRouter({ history: createMemoryHistory(), routes: [] })
  let resolve
  const runtime = createPluginRuntime({ session, router, entries, tabs: { setPluginEntries() {} }, fetch: () => new Promise(r => resolve = r) })
  const pending = runtime.refresh(); session.token = ''; runtime.clear(); resolve({ plugins: [metadata()] }); await pending
  assert.equal(entries.value.length, 0); assert.equal(router.getRoutes().length, 0)
})
test('服务器实时拒绝时清理当前插件，其他插件不受影响', async () => {
  const s = setup(); await s.runtime.refresh(); s.runtime.rejectPlugin('com.test.demo')
  assert.equal(s.entries.value.length, 0); assert.ok(s.router.hasRoute('books'))
})
