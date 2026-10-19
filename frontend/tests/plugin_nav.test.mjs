import test from 'node:test'
import assert from 'node:assert/strict'
import { source, metadata } from './plugin_test_support.mjs'
const { NAV, visibleNav, landingOf } = await source('/src/router/nav.js')
const { validatePlugin } = await source('/src/plugins/manifest.js')
test('动态导航只从已授权 manifest 派生，静态 NAV 不发生变更', () => {
  const before = JSON.stringify(NAV)
  assert.ok(!visibleNav(true).some(x => x.key === 'extensions'))
  const p = validatePlugin(metadata())
  const nav = visibleNav(false, [p])
  assert.equal(nav.find(x => x.key === 'extensions').items[0].name, p.menu.name)
  assert.equal(landingOf(nav.find(x => x.key === 'extensions')), p.menu.name)
  assert.equal(JSON.stringify(NAV), before)
  assert.ok(!nav.some(x => x.key === 'system'))
})
