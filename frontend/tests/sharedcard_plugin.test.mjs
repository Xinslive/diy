/**
 * 共享卡对账插件前端：匹配月份范围的判定逻辑。
 *
 * 插件源码在 ``plugins/`` 下，而 ``plugins/`` 是 gitignore 的：干净 checkout
 * 里没有这份源码。所以源码缺失时**跳过**，不是失败 —— 在本机全绿、在 CI 必红
 * 的用例等于没有用例。
 */
import { existsSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const PLUGIN_ENTRY = new URL('../../plugins/com.cwq.sharedcard/frontend/index.js', import.meta.url)
const plugin = existsSync(PLUGIN_ENTRY) ? await import(PLUGIN_ENTRY.href) : null

function skip(t) {
  // t.skip() 只是**标记**跳过，不会中断函数体；所以这里必须自己返回 true，
  // 让调用方 `if (skip(t)) return` 真的停住 —— 否则源码缺失时用例会继续
  // 往下跑，在 null 上取值而报错。
  if (!plugin) {
    t.skip('本机没有共享卡对账插件源码')
    return true
  }
  return false
}

test('月份标签：年补四位、月补两位', t => {
  if (skip(t)) return
  const { monthLabel } = plugin
  assert.equal(monthLabel(2026, 8), '2026-08')
  assert.equal(monthLabel(2026, 12), '2026-12')
  // **前补零**：不补的话「2026-1」与「2026-01」是两种写法，排序与比较都会错。
  assert.equal(monthLabel(2026, 1), '2026-01')
  assert.equal(monthLabel(999, 3), '0999-03')
})

test('候选月份：前后各三个月，跨年也对得上', t => {
  if (skip(t)) return
  const { monthChoices } = plugin
  assert.deepEqual(monthChoices('2026-09', 3), [
    '2026-06', '2026-07', '2026-08', '2026-09', '2026-10', '2026-11', '2026-12',
  ])
  // **跨年**：12 月的前三个月是 9/10/11 月，次年的 1/2/3 月。
  assert.deepEqual(monthChoices('2026-12', 3), [
    '2026-09', '2026-10', '2026-11', '2026-12', '2027-01', '2027-02', '2027-03',
  ])
  // **跨年往前**：1 月的前三个月是上一年的 10/11/12 月。
  assert.deepEqual(monthChoices('2026-01', 3), [
    '2025-10', '2025-11', '2025-12', '2026-01', '2026-02', '2026-03', '2026-04',
  ])
  // 认不出来就别硬凑月份出来。
  assert.deepEqual(monthChoices('', 3), [])
})

test('勾选月份：勾上就加上，勾掉就减去', t => {
  if (skip(t)) return
  const { toggleMonth } = plugin
  // 「9 月的对账单去对 8 月的账」—— 勾上 8 月。
  assert.deepEqual(toggleMonth(['2026-09'], '2026-08'), ['2026-08', '2026-09'])
  // 勾掉 9 月 → 只剩 8 月。
  assert.deepEqual(toggleMonth(['2026-08', '2026-09'], '2026-09'), ['2026-08'])
  // **减到一个不剩 = 回到默认**（跟着对账单自己的月份走），
  // 而不是「什么都不匹配」—— 用户要的是「对 8 月的账」，不是「谁都不对」。
  assert.deepEqual(toggleMonth(['2026-09'], '2026-09'), [])
})

test('勾选月份：结果始终按时间排序', t => {
  if (skip(t)) return
  const { toggleMonth } = plugin
  // 「2026-10」排到「2026-09」前面 —— 字符串排序与时间排序在这里一致，
  // 但顺序乱了会让「现在按哪几个月对账」那句话读起来像乱码。
  assert.deepEqual(toggleMonth(['2026-10'], '2026-09'), ['2026-09', '2026-10'])
  assert.deepEqual(toggleMonth(['2026-08'], '2026-11'), ['2026-08', '2026-11'])
})

test('金额：千分位与差额符号', t => {
  if (skip(t)) return
  const { money, diffClass } = plugin
  assert.equal(money('100007401.00'), '100,007,401.00')
  assert.equal(money('-1234.5'), '-1,234.5')
  assert.equal(money(''), '—')
  assert.equal(diffClass('-1.00'), 'sc-danger')
  assert.equal(diffClass('0.00'), 'sc-ok')
  assert.equal(diffClass('12.00'), 'sc-warn')
})
