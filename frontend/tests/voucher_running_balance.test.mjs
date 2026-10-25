/**
 * 凭证录入页科目格左下角那个余额：**时点**、**滚动**、**敲完才更新**。
 *
 * 三件事，各是一道用户提过的要求（2026-10-25）：
 *
 * 1. **时点**：显示的是「这张凭证记进去之前账上有多少」，不是整期期末。
 *    一张 100、一张 300、一张 500，月中那张该看到 100，不是 900。
 *    （基数由后端按这张在期间里的位置算，前端只管加自己那几行。）
 * 2. **滚动**：同一张凭证里同一个科目出现三次借 100，三行分别显示
 *    100 / 200 / 300 —— 每一行显示的是「到这一行为止还剩多少」，
 *    不是「整张记完还剩多少」（那三行都会显示 300）。
 * 3. **敲完才更新**：敲字的过程中余额一动不动，光标离开那一格才一步到位
 *    （输入 1 余额不变，输入 100 离开这一格，余额从 300 走到 400）。
 *
 * 这里从**真实组件源码**里抠出这一段跑一遍（与 voucher_balance.test.mjs
 * 同一个路子）：测的是页面上真在用的那份代码。
 *
 * 「整数分不掉精度」那几条是从 reviewfix 的老用例里带过来的
 * （原来叫 ownDelta，现在叫 runningDelta）—— 余额那一串从头到尾是 BigInt，
 * 18 位金额差 0.03 元这种事不能再犯。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { computed, reactive, ref } from 'vue'
import { moneyText } from '../src/utils/format.js'
import { moneyCents } from '../src/utils/amountGrid.js'
import { displayBalanceCents } from '../src/utils/balanceSign.js'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

const START = '// ---- 科目余额段开始'
const END = '// ---- 科目余额段结束'

/** 抠出余额那一段，并自检抠到的是它（抠空了就让断言失败，别让测试空转）。 */
function balanceBlock() {
  const a = SRC.indexOf(START)
  const b = SRC.indexOf(END)
  assert.ok(a !== -1, `源码里找不到标记 ${START}`)
  assert.ok(b > a, `源码里找不到结束标记 ${END}`)
  const out = SRC.slice(a, b)
  for (const fn of ['committedOf', 'commitAmount', 'runningDelta', 'balanceText']) {
    assert.ok(out.includes(fn), `抠出来的段里没有 ${fn} —— 标记挪位置了？`)
  }
  return out
}
const BLOCK = balanceBlock()

/**
 * 搭一套真实的余额状态。
 *
 * ``balances`` 是后端给的**基数**（这张凭证之前的账上余额，借正贷负），
 * ``subjects`` 是科目表里那些科目的「余额方向」。
 */
function page(entries, balances = {}, subjects = {}) {
  const form = reactive({ entries })
  // ``centsOf`` 在组件里就是 ``moneyCents`` 的一层壳（见 VoucherEdit.vue）——
  // 这里直接把 moneyCents 绑成它，测的就是同一份定点化。
  const factory = new Function('computed', 'ref', 'moneyText', 'centsOf', 'displayBalanceCents',
    'form', 'balances', 'readonly', 'subjectOf', `${BLOCK}
    return { form, committedAmounts, committedOf, commitAmount, seedCommitted,
             runningDelta, balanceText }`)
  return factory(computed, ref, moneyText, moneyCents, displayBalanceCents,
    form, { value: balances }, { value: false }, (id) => subjects[id] || null)
}

const D = (amount, subject_id = 1) => ({ subject_id, direction: 'DEBIT', amount, summary: '' })
const C = (amount, subject_id = 2) => ({ subject_id, direction: 'CREDIT', amount, summary: '' })

// ------------------------------------------------------------------ 口径一：滚动

test('同一张凭证里同一科目的三行是滚动的 100 / 200 / 300', () => {
  const p = page([D('100.00'), D('100.00'), D('100.00')], { 1: 0 }, { 1: { direction: 'DEBIT' } })
  p.seedCommitted(p.form.entries)
  assert.equal(p.balanceText(0, p.form.entries[0]), '100.00')
  assert.equal(p.balanceText(1, p.form.entries[1]), '200.00')
  assert.equal(p.balanceText(2, p.form.entries[2]), '300.00',
    '第三行不是 300 —— 滚动口径没生效？')
})

test('基数是这张凭证**之前**的账（后端给的时点余额）', () => {
  // 三张凭证各借 100：月中那张的基数是 100（它前面那张的数），不是 300（月底的账）
  const p = page([D('100.00')], { 1: 100 }, { 1: { direction: 'DEBIT' } })
  p.seedCommitted(p.form.entries)
  assert.equal(p.balanceText(0, p.form.entries[0]), '200.00',
    '基数没加上这张自己的分录')
})

test('没挂科目的行不进累计', () => {
  const p = page([
    { subject_id: null, direction: 'DEBIT', amount: '100.00', summary: '' },
    D('100.00'),
  ], { 1: 0 }, { 1: { direction: 'DEBIT' } })
  p.seedCommitted(p.form.entries)
  assert.equal(p.balanceText(1, p.form.entries[1]), '100.00',
    '没挂科目的那一行也进了累计')
})

// ------------------------------------------------------------------ 口径二：敲完才更新

test('敲字的过程中余额一动不动，光标离开这一格才一步到位', () => {
  const p = page([D('')], { 1: 300 }, { 1: { direction: 'DEBIT' } })
  const row = p.form.entries[0]
  // 敲「1」「10」「100」：余额一直是 300 —— 半截数不该牵着人走
  for (const v of ['1', '10', '100']) {
    row.amount = v
    assert.equal(p.balanceText(0, row), '300.00', `敲到「${v}」时余额就跳了`)
  }
  // 光标离开这一格 → 定下来 → 一步到位从 300 到 400
  p.commitAmount(row)
  assert.equal(p.balanceText(0, row), '400.00')
})

test('提交的是「方向 + 金额」，方向翻了余额也跟着翻', () => {
  const p = page([D('100.00')], { 1: 500 }, { 1: { direction: 'DEBIT' } })
  const row = p.form.entries[0]
  p.seedCommitted(p.form.entries)
  assert.equal(p.balanceText(0, row), '600.00')
  // 改成贷方 100 —— 敲的过程中余额不动，离开这一格才定下来
  row.direction = 'CREDIT'
  row.amount = '100.00'
  assert.equal(p.balanceText(0, row), '600.00', '敲的过程中余额不该动')
  p.commitAmount(row)
  assert.equal(p.balanceText(0, row), '400.00')
})

// ------------------------------------------------------------------ 口径三：整数分

test('余额那一串是整数分（BigInt），18 位金额一分不差', () => {
  const p = page([D('999999999999999999.99'), D('999999999999999999.99')],
    { 1: 0 }, { 1: { direction: 'DEBIT' } })
  p.seedCommitted(p.form.entries)
  assert.equal(typeof p.runningDelta.value[1], 'bigint', 'runningDelta 的结果不是 BigInt —— 还在走浮点')
  assert.equal(p.runningDelta.value[1], 199999999999999999998n,
    `算成了 ${p.runningDelta.value[1]} —— 掉精度了`)
  assert.equal(p.balanceText(1, p.form.entries[1]), '1,999,999,999,999,999,999.98')
})

test('贷属性科目的余额要翻负（借正贷负那套显示口径没变）', () => {
  // 贷属性的科目一旦在**借方**挂上钱，就显示负数 —— 一眼看出录反了方向
  const p = page([D('100.00', 2)], { 2: 0 }, { 2: { direction: 'CREDIT' } })
  p.seedCommitted(p.form.entries)
  assert.equal(p.balanceText(0, p.form.entries[0]), '-100.00',
    '贷属性科目的正负号翻了 —— 那是显示口径，不该被这一处改掉')
})

test('0 显示成空（一个空科目没有余额可言）', () => {
  const p = page([], { 1: 0 }, { 1: { direction: 'DEBIT' } })
  assert.equal(p.balanceText(0, { subject_id: 1 }), '')
})
