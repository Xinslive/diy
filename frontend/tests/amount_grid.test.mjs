/**
 * 金额格的输入口径（utils/amountGrid.js）。
 *
 * 用户点名的一条：**负号是格子的属性，不是插在数字中间的一个字符**。
 * 光标在哪一位数上都一样，按一下整格翻负、再按一下翻回来；
 * 一个键只管一件事，不必先把光标挪到最前头。
 *
 * 跟 balance_sign 一样：用 vite 的 ssrLoadModule 加载**线上那份源码**，不复制逻辑。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const { toggleAmountSign, sanitizeAmount, amountInputText } = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/amountGrid.js'))

let pass = 0, fail = 0
const t = (name, fn) => { try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }

t('正数按一下「-」：整格翻负（不是插在光标那儿）', () => {
  assert.equal(toggleAmountSign('1234.56'), '-1234.56')
  assert.equal(toggleAmountSign('5'), '-5')
})
t('已经是负数了再按一下：翻回正', () => {
  assert.equal(toggleAmountSign('-1234.56'), '1234.56')
  assert.equal(toggleAmountSign('-5'), '5')
})
t('空格子按一下「-」：先摆好方向，接着敲的数直接成负数', () => {
  assert.equal(toggleAmountSign(''), '-')
  assert.equal(sanitizeAmount(toggleAmountSign('') + '500'), '-500')
})
t('只有一个负号时按一下：清空（空格 ↔ 负号 来回翻）', () => {
  assert.equal(toggleAmountSign('-'), '')
  assert.equal(toggleAmountSign(toggleAmountSign('')), '')
})
t('零没有方向可谈，按空格子算，不摆「-0.00」这种没人写的数', () => {
  assert.equal(toggleAmountSign('0'), '-')
  assert.equal(toggleAmountSign('0.00'), '-')
  assert.equal(toggleAmountSign('-0.00'), '')
})
t('翻完数位不丢：角分、小数都原样带着', () => {
  for (const v of ['1', '1234.56', '0.05', '303000']) {
    assert.equal(toggleAmountSign(toggleAmountSign(v)), v, v)
  }
})
t('刚敲下、还没跟数字的小数点要留在框里', () => {
  assert.equal(amountInputText('2332.', '2332'), '2332.')
  assert.equal(amountInputText('2332.', '2332.5'), '2332.5')
  assert.equal(amountInputText('2332.5', '2332.5'), '2332.5')
  assert.equal(amountInputText('2332', '2332'), '2332')
})
t('没有负号的输入原样过（这一条不能被新逻辑改坏）', () => {
  assert.equal(sanitizeAmount(''), '')
  assert.equal(sanitizeAmount('1234'), '1234')
  assert.equal(sanitizeAmount('1234.567'), '1234.56', '小数只留两位')
  assert.equal(sanitizeAmount('abc'), '', '字母不留')
  assert.equal(sanitizeAmount('-1'), '-1', '红字冲销的负号一路留到底')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
