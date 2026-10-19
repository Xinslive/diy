import test from 'node:test'
import assert from 'node:assert/strict'
import { createPinia } from 'pinia'
import { source, metadata, storage } from './plugin_test_support.mjs'
storage()
const { useTabsStore } = await source('/src/stores/tabs.js')
const { validatePlugin } = await source('/src/plugins/manifest.js')
const p = validatePlugin(metadata())
const route = { name: p.menu.name, path: p.menu.route, meta: { title: p.menu.title }, query: { book_id: 9 } }
test('插件 tab 通过运行时授权注册，保存字段保持 key/title/path', () => {
  localStorage.setItem('cwq_user', JSON.stringify({ id: 1 }))
  const tabs = useTabsStore(createPinia())
  tabs.open(route); assert.equal(tabs.opened.length, 0)
  tabs.setPluginEntries([p]); tabs.open(route)
  const saved = JSON.parse(localStorage.getItem('cwq_tabs'))
  assert.deepEqual(Object.keys(saved.opened[0]).sort(), ['key', 'path', 'title'])
  const restored = useTabsStore(createPinia())
  assert.equal(restored.opened.length, 0)
  restored.setPluginEntries([p]); assert.equal(restored.opened[0].key, p.menu.name)
  restored.setPluginEntries([]); assert.equal(restored.opened.length, 0)
  assert.equal(JSON.parse(localStorage.getItem('cwq_tabs')).opened.length, 0)
})
test('切换用户不能恢复前一个用户的插件 tab', () => {
  localStorage.setItem('cwq_user', JSON.stringify({ id: 1 }))
  const tabs = useTabsStore(createPinia()); tabs.setPluginEntries([p]); tabs.open(route)
  localStorage.setItem('cwq_user', JSON.stringify({ id: 2 }))
  const other = useTabsStore(createPinia()); other.setPluginEntries([p]); assert.equal(other.opened.length, 0)
})
