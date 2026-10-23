/**
 * 审查 L-30 ~ L-34：一批小的修复。
 *
 * 这一组多数是**注释与口径**的修复 —— 修的不是「算错了」，是
 * 「下一个人会误以为是这样」或者「两份写法迟早对不上」。
 * 所以这里的断言也相应地守着**那件事本身**（注释在不在、规则是不是同一份、
 * 脚本是不是真的能跑），而不是抄一份逻辑重算一遍。
 *
 * 逐条：
 *   L-30 凭证页同屏两种金额写法（合计格逐位落格 / 页脚是文本）—— 写清，别强行统一；
 *   L-31 插件与宿主同页同权限 —— 信任边界要写在代码里（架构选择，不改架构）；
 *   L-32 登录地址两处各写一份 —— 抽一个常量，并让两边对得上；
 *   L-33 前端 displayBalance 与后端 display_side 约定相反 —— 文件顶上要有一句警告；
 *   L-34 仓库无 lint —— 加一条**零依赖**的语法自检（不加新依赖）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const read = (rel) => fs.readFileSync(new URL(rel, import.meta.url), 'utf8')

const VOUCHER = read('../src/views/voucher/VoucherEdit.vue')
const PLUGIN = read('../src/plugins/runtime.js')
const CLIENT = read('../src/api/client.js')
const ROUTER = read('../src/router/index.js')
const BALANCE = read('../src/utils/balanceSign.js')
const PKG = JSON.parse(read('../package.json'))

console.log('')
console.log('== L-30 凭证页同屏两种金额写法：写清，别强行统一 ==')

test('页脚差额那一段写清了「合计格是金额格、页脚是文本」', () => {
  // 合计格逐位落格是为了对齐打印纸（刻意设计），页脚是带千分位的一句人话。
  // 不写清楚的话，下一个人会以为这是 bug，去"统一"它 —— 统一了就有一边失去理由。
  assert.match(VOUCHER, /合计格（AmountCells）是\*\*逐位落格\*\*/,
    '没有写明合计格是逐位落格的')
  assert.match(VOUCHER, /页脚是文本，合计格是金额格/,
    '没有写明页脚是文本、合计格是金额格')
  assert.match(VOUCHER, /打印纸/, '没有提「对齐打印纸」这个理由')
})

test('页脚金额走整数分的那一份（moneyText），不是浮点的 fmt', () => {
  const m = /const diffTag = computed\([\s\S]*?\n\}\)/.exec(VOUCHER)
  assert.ok(m, '源码里找不到 diffTag')
  assert.match(m[0], /moneyText\(/, '页脚差额没有走 moneyText')
  assert.doesNotMatch(m[0], /fmt\(/, '页脚差额又走回浮点的 fmt 了')
})

console.log('')
console.log('== L-31 插件与宿主同页同权限：信任边界写在代码里 ==')

test('runtime.js 的文件头写明了「插件与宿主同权限、令牌对插件可见」', () => {
  const head = PLUGIN.slice(0, 2000)
  assert.match(head, /信任边界/, 'runtime.js 顶上没有写信任边界')
  assert.match(head, /同页、同权限|同页同权限/, '没有写明插件与宿主同页同权限')
  assert.match(head, /cwq_token/, '没有写明令牌对插件可见')
  assert.match(head, /只安装可信插件|可信插件/, '没有写明信任前提')
})

test('没有改成别的架构（host 仍然是冻结的、插件仍然是同页执行）', () => {
  assert.match(PLUGIN, /Object\.freeze\(\{ h: Vue\.h/, 'host 不再是冻结的了 —— 架构被改动了')
  assert.match(PLUGIN, /module\.default\(host\)/, '插件不再拿到 host —— 架构被改动了')
})

console.log('')
console.log('== L-32 登录地址：一份常量，两边对得上 ==')

test('client.js 抽出 LOGIN_HASH 常量，并在 401 那条路上用它', () => {
  assert.match(CLIENT, /export const LOGIN_HASH = '#\/login'/, 'client.js 没有 LOGIN_HASH 常量')
  const handler = CLIENT.slice(CLIENT.indexOf('status === 401'))
  assert.match(handler, /location\.hash = LOGIN_HASH/, '401 那条路没有用 LOGIN_HASH')
  assert.doesNotMatch(handler, /location\.hash = '#\/login'/, '401 那条路还写着一份字面量')
})

test('常量与路由表里注册的路径对得上（改一处就要改另一处）', () => {
  const m = /path: '(\/login)'/.exec(ROUTER)
  assert.ok(m, '路由表里找不到登录页的 path')
  // hash 路由：浏览器地址是 '#/login'，路由表里注册的是路径段 '/login'
  assert.equal(CLIENT.includes(`LOGIN_HASH = '#${m[1]}'`), true,
    `LOGIN_HASH 与路由表的 ${m[1]} 对不上 —— 令牌一失效就会跳到不存在的地址`)
})

test('router/index.js 里也留了互相指路的注释', () => {
  assert.match(ROUTER, /LOGIN_HASH/, 'router/index.js 没有指向 client.js 的 LOGIN_HASH')
})

console.log('')
console.log('== L-33 前端 displayBalance 与后端 display_side 约定相反 ==')

test('balanceSign.js 顶上有一句醒目的警告', () => {
  const head = BALANCE.slice(0, 1200)
  assert.match(head, /与后端.*display_side.*约定相反/, '顶上没有那句「约定相反」的警告')
  assert.match(head, /勿混用/, '警告里没有「勿混用」')
})

test('**行为**没变：displayBalance 的正负号规则一字未改', () => {
  // 这条只守「别顺手把函数改了」—— 约定相反是要**说清楚**，不是要「修」。
  const m = /export function displayBalance\(direction, signed\) \{[\s\S]*?\n\}/.exec(BALANCE)
  assert.ok(m, '源码里找不到 displayBalance')
  assert.match(m[0], /direction === 'CREDIT' \? -n : n/, 'displayBalance 的行为被改了')
  assert.match(m[0], /Number\.isFinite\(n\)/, 'displayBalance 的脏输入处理被改了')
})

console.log('')
console.log('== L-34 零依赖的语法自检 ==')

test('package.json 里多了一条 check 脚本', () => {
  assert.ok(PKG.scripts?.check, 'package.json 里没有 check 脚本')
  assert.equal(PKG.scripts.check, 'node tests/check_syntax.mjs',
    `check 脚本变了：${PKG.scripts?.check}`)
})

test('没有引入新的 lint 依赖（会破坏 pnpm install --frozen-lockfile）', () => {
  const deps = { ...(PKG.dependencies || {}), ...(PKG.devDependencies || {}) }
  for (const name of Object.keys(deps)) {
    assert.doesNotMatch(name, /eslint|prettier|standard|oxlint|biome/, `多了 lint 依赖：${name}`)
  }
})

test('**行为**：那条自检真的跑得起来，而且当前代码是干净的', () => {
  // 用 fileURLToPath（不是 .pathname）：仓库路径里有中文，.pathname 是百分号编码的
  const script = fileURLToPath(new URL('../tests/check_syntax.mjs', import.meta.url))
  // 只查 src/utils 这一小块 —— 全量查要 spawn 一百多个 node 进程，
  // 放在单元测试里会把整套测试拖慢十几秒（`npm run check` 才是全量的那一次）。
  const out = execFileSync(process.execPath, [script, 'src/utils'], { cwd: ROOT, encoding: 'utf8' })
  assert.match(out, /语法自检通过/, `自检没有给出通过结论：${out}`)
})

test('自检的作用范围写在脚本的头注释里（别让人以为它管全站）', () => {
  const src = read('../tests/check_syntax.mjs')
  assert.match(src, /作用范围/, '脚本头上没有写作用范围')
  assert.match(src, /不做/, '脚本头上没有写「不做什么」')
})
