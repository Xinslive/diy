/**
 * 自助注册开关的前端一半（2026-10-19）。
 *
 * 要守住三件事，缺一条都正好对应一种用户看得见的错法：
 *
 *   1. 关掉时**整个注册页签不渲染**（不是只置灰）—— 页签还在而点下去撞 403，
 *      在登录页上尤其难看，那是一个还没进门的人看到的第一个界面。
 *   2. 关掉时如果人正停在「注册」页，要被拉回登录 —— 否则剩下半张没有页签栏的
 *      空白表单，看起来像页面坏了。
 *   3. 接口失败按「关着」处理 —— 少一个入口好过在没确认过状态时把一个匿名
 *      入口亮出来。
 *
 * 判定逻辑放在 utils/registration.js 里就是为这个：它在 LoginView.vue 的
 * 模板里只占一个 `v-if`，改坏了恒绿。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { source } from './plugin_test_support.mjs'

const { registrationView } = await source('/src/utils/registration.js')
const LOGIN = fs.readFileSync(new URL('../src/views/LoginView.vue', import.meta.url), 'utf8')

test('关闭时注册页签不渲染，页签栏也一起去掉', () => {
  const v = registrationView(false, 'login')
  assert.equal(v.showRegister, false)
  assert.equal(v.showTabs, false, '只剩登录一个页签时，页签栏没有信息量')
})

test('关闭时停在注册页的人被拉回登录', () => {
  const v = registrationView(false, 'register')
  assert.equal(v.tab, 'login', '人正停在注册页时关掉开关，会剩一张空白表单')
  assert.equal(v.showRegister, false)
})

test('开启时注册页签照常出现', () => {
  const v = registrationView(true, 'login')
  assert.equal(v.showRegister, true)
  assert.equal(v.showTabs, true)
  assert.equal(v.tab, 'login')
  assert.equal(registrationView(true, 'register').tab, 'register', '开着时别把人踢回登录')
})

test('拿不到状态一律当关闭（失败即隐藏）', () => {
  for (const bad of [undefined, null, 0, '', false, 'false']) {
    assert.equal(registrationView(bad, 'login').showRegister, false,
      `${JSON.stringify(bad)} 应当按「关着」处理`)
  }
})

test('页签初值缺失时退回登录', () => {
  assert.equal(registrationView(true, undefined).tab, 'login')
  assert.equal(registrationView(false, undefined).tab, 'login')
})

test('登录页确实去拉了这个开关，并把它绑在注册页签上', () => {
  // 这三条是「有没有接上」的连线检查：判定函数测得再对，页面没调用它也一样坏。
  assert.match(LOGIN, /authApi\.publicConfig\(\)/, '登录页没有读后端的注册开关')
  assert.match(LOGIN, /v-if="gate\.showRegister"[^>]*label="注册"|label="注册"[^>]*v-if="gate\.showRegister"/,
    '注册页签没有绑在开关上 —— 它会一直显示')
  // 接口失败要保持关闭，不能在 catch 里又打开一次
  assert.doesNotMatch(LOGIN, /catch\s*\{[^}]*registrationEnabled\.value\s*=\s*true/,
    '请求失败时把注册入口点亮了')
})
