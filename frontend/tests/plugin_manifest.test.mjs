import test from 'node:test'
import assert from 'node:assert/strict'
import { source, metadata } from './plugin_test_support.mjs'
const { validatePlugin } = await source('/src/plugins/manifest.js')
test('合法 manifest 只产生受控的扩展路由和同源不可变资产', () => {
  const p = validatePlugin(metadata())
  assert.equal(p.menu.name, 'plugin:com.test.demo:main')
  assert.equal(p.menu.key, p.menu.name)
})
for (const [name, mutate] of [
  ['id', p => p.id = '../evil'], ['digest', p => p.digest = 'wrong'], ['version', p => p.version = 'latest'],
  ['route', p => p.menu.route = '/books'], ['route_prefix', p => p.menu.route = '/plugin/com.test.demoevil'],
  ['menu', p => p.menu.module = 'system'], ['icon', p => p.menu.icon = 'script'], ['route_name', p => p.menu.name = 'books'],
  ['remote', p => p.entry = 'https://evil.example/index.js'], ['query_token', p => p.entry += '?token=leak'],
  ['escape', p => p.entry = p.entry.replace('index.js', '../backend/main.py')], ['styles', p => p.styles = ['https://evil.example/style.css']],
]) test(`不安全 ${name} 被拒绝`, () => { const p = metadata(); mutate(p); assert.throws(() => validatePlugin(p)) })
