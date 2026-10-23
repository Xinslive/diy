/**
 * 审查 P1-3：`ownDelta` 用浮点累加，与同一页面的整数分口径不一致。
 *
 * 同一张凭证页里，`centsOf` / `voucherTotals` / `autoBalanceNeed` 全都走
 * **整数分（BigInt）**，唯独「本张给每个科目带来的变动」这一处走 `Number()` 浮点累加。
 * 18 位金额会在这差出 0.03 元 —— 而这一格正是辅助记账时人照着填数的那个数。
 *
 * 这里从**真实组件源码**里抠出 ownDelta 与 balanceText 跑一遍
 * （与 voucher_balance.test.mjs 同一个路子）：测的是页面上真在用的那份代码。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { computed, reactive } from 'vue'
import { moneyText } from '../src/utils/format.js'
import { moneyCents } from '../src/utils/amountGrid.js'
import { displayBalanceCents } from '../src/utils/balanceSign.js'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

/** 抠出两个标记之间那一段（ownDelta 专用）。 */
function between(startMark, endMark) {
  const a = SRC.indexOf(startMark)
  const b = SRC.indexOf(endMark)
  assert.ok(a !== -1, `源码里找不到标记 ${startMark}`)
  assert.ok(b > a, `源码里找不到标记 ${endMark}`)
  const block = SRC.slice(a, b)
  assert.ok(block.includes('const ownDelta'), '抠出来的段里没有 ownDelta —— 标记挪位置了？')
  return block
}

const OWN_DELTA_BLOCK = between(
  '// ---- ownDelta 段开始', '// ---- ownDelta 段结束')

/** 抠出 `const <name> = (…) => { … }` 的整段源码（按大括号配平）。 */
function grabArrow(name) {
  const m = new RegExp(`const\\s+${name}\\s*=\\s*`).exec(SRC)
  assert.ok(m, `源码里找不到 const ${name} =`)
  const start = m.index + m[0].length
  let depth = 0, j = SRC.indexOf('(', start)
  for (; j < SRC.length; j++) {
    if (SRC[j] === '(') depth++
    else if (SRC[j] === ')') { depth--; if (depth === 0) break }
  }
  assert.equal(SRC[j], ')', `${name} 的参数表没配平`)
  const arrow = SRC.indexOf('=>', j)
  assert.ok(arrow > 0 && arrow < j + 40, `${name} 不是箭头函数`)
  const bodyStart = SRC.indexOf('{', arrow)
  assert.ok(bodyStart > 0, `${name} 不是带块体的箭头函数`)
  depth = 0
  for (j = bodyStart; j < SRC.length; j++) {
    if (SRC[j] === '{') depth++
    else if (SRC[j] === '}') { depth--; if (depth === 0) break }
  }
  const out = SRC.slice(m.index, j + 1)
  assert.ok(out.endsWith('}'), `${name} 抠出来的不是一段完整的函数`)
  return out
}

const BALANCE_TEXT_SRC = grabArrow('balanceText')

/** 搭一套真实的 ownDelta + balanceText。 */
function page(entries, balances = {}, subjects = {}) {
  const form = reactive({ entries })
  const factory = new Function('computed', 'moneyText', 'moneyCents', 'displayBalanceCents',
    'form', 'balances', 'subjects', `
    const readonly = { value: false }
    const markingRows = { value: new Set() }
    const subjectOf = (id) => subjects[id] || null
    const centsOf = (v) => moneyCents(v)
    ${OWN_DELTA_BLOCK}
    ${BALANCE_TEXT_SRC}
    return { ownDelta, balanceText }`)
  const own = factory(computed, moneyText, moneyCents, displayBalanceCents,
    form, { value: balances }, subjects)
  return { ...own, form }
}

const D = (amount, subject_id = 1) => ({ direction: 'DEBIT', amount, subject_id })
const C = (amount, subject_id = 2) => ({ direction: 'CREDIT', amount, subject_id })

test('ownDelta 累加出来的是**整数分（BigInt）**，不是浮点', () => {
  const p = page([D('100.10'), C('30.05', 2)])
  assert.equal(typeof p.ownDelta.value[1], 'bigint', 'ownDelta 的结果不是 BigInt —— 还在走浮点')
  assert.equal(p.ownDelta.value[1], 10010n)
  assert.equal(p.ownDelta.value[2], -3005n, '贷方没有取负')
})

test('18 位金额一分不差（浮点在这里会差出 0.03 元）', () => {
  const p = page([D('999999999999999999.99'), D('999999999999999999.99', 1)])
  assert.equal(p.ownDelta.value[1], 199999999999999999998n,
    `ownDelta 算成了 ${p.ownDelta.value[1]} —— 掉精度了`)
  // 浮点那一条路：两笔 999999999999999999.99 加起来是 2e18，
  // 而正确的和是 1999999999999999999.98 —— **差 0.02 元**
  const floatSum = 999999999999999999.99 + 999999999999999999.99
  assert.notEqual(String(floatSum), '1999999999999999999.98',
    '浮点这条路的残差不见了 —— 请复核这个测试还测的是不是同一件事')
  assert.equal(p.ownDelta.value[1], 199999999999999999998n)
})

test('科目格显示的余额也是整数分那一路（不经过浮点）', () => {
  // 账上余额（后端给的 number）+ 本张正在填的（整数分）
  const p = page([D('0.10')], { 1: 36500.5 }, { 1: { direction: 'DEBIT' } })
  assert.equal(p.balanceText({ subject_id: 1 }), '36,500.60',
    '科目格显示的不是精确到分的余额')
})

test('贷属性科目的余额要翻负（借正贷负那套口径没变）', () => {
  const p = page([D('100.00', 2)], { 2: 0 }, { 2: { direction: 'CREDIT' } })
  assert.equal(p.balanceText({ subject_id: 2 }), '-100.00',
    '贷属性科目的正负号翻了 —— 那是显示口径，不该被这一处改掉')
})

test('0 显示成空（一个空科目没有余额可言）', () => {
  const p = page([], { 1: 0 }, { 1: { direction: 'DEBIT' } })
  assert.equal(p.balanceText({ subject_id: 1 }), '')
})

test('没有科目、没有金额的行不算数', () => {
  const p = page([{ direction: 'DEBIT', amount: '100.00', subject_id: null }])
  assert.deepEqual(p.ownDelta.value, {}, '没挂科目的行也进了 ownDelta')
})
