/**
 * 全站唯一的金额格式化器（src/utils/format.js）—— 审查 P2-4。
 *
 * `fmt` 被 12 处调用，却**零测试覆盖**：
 * 唯一碰到它的 voucher_balance.test.mjs 是把它当**桩注入**的
 * （`new Function(..., 'fmt', ...)`）—— 那是拿它当依赖用，不是测它。
 * 一份零覆盖的格式化器，改坏了要等到某张凭证上显示出一个奇怪的数才会被发现。
 *
 * 这里测的是**线上那份源码**（vite 的 ssrLoadModule），不是抄一份：
 * 抄一份只能证明抄的那份是对的。
 *
 * 同一份文件里还有 `moneyText`（整数分 → 文本，见 L-29）：
 * 前端内部的金额口径是整数分（BigInt），凡是已经拿着整数分的地方
 * 都不该退回去走浮点 —— `fmt` 走 Number，18 位整数在浮点里已经不是原来那个数了。
 */
import { createServer } from 'vite'
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const server = await createServer({
  root: ROOT, logLevel: 'error',
  server: { middlewareMode: true },
  optimizeDeps: { noDiscovery: true, include: [] },
})
const { fmt, moneyText } = await server.ssrLoadModule('/src/utils/format.js')
// vite 的 dev server 攥着端口与文件监听，不关掉的话 node:test 永远等不到进程退出
after(() => server.close())

console.log('== fmt：空与认不出来的输入 ==')

test('null / undefined / 空串 → 空串（页面上不许冒出 "NaN"）', () => {
  assert.equal(fmt(null), '')
  assert.equal(fmt(undefined), '')
  assert.equal(fmt(''), '')
})

test('认不出来的串原样返回（别悄悄变成 0）', () => {
  // 变成 "0.00" 是**骗人**：那会让一个错值看起来像一个正常的 0
  assert.equal(fmt('abc'), 'abc')
  assert.equal(fmt('12abc34'), '12abc34', '半截数字认不出来，就该原样摆着让人看见')
  assert.equal(fmt('1,234.50'), '1,234.50', '带千分位的串认不出来，但原样返回也不是错')
})

test('0 → "0.00"（不是空串）', () => {
  // 空串是「没有」，"0.00" 是「有，但是一分没有」—— 两者不能混
  assert.equal(fmt(0), '0.00')
  assert.equal(fmt('0'), '0.00')
  assert.equal(fmt(0.0), '0.00')
})

console.log('')
console.log('== fmt：负数与千分位 ==')

test('负数带负号，金额大小照旧', () => {
  assert.equal(fmt(-1234.567), '-1,234.57')
  assert.equal(fmt(-0.5), '-0.50')
  assert.equal(fmt(1234.5), '1,234.50')
})

test('千分位按三位一组', () => {
  assert.equal(fmt(1234567.891), '1,234,567.89')
  assert.equal(fmt(1000), '1,000.00')
  assert.equal(fmt(999), '999.00')
  assert.equal(fmt(100000), '100,000.00')
})

console.log('')
console.log('== fmt：maximumFractionDigits 的截断行为 ==')

test('小数位按 digits 取（默认 2 位）', () => {
  assert.equal(fmt(1234.567), '1,234.57')
  assert.equal(fmt(0.1), '0.10')
  assert.equal(fmt('300.30'), '300.30')
})

test('digits 可以改：0 位没有小数、4 位补到 4 位', () => {
  assert.equal(fmt(1234.567, 0), '1,235')
  assert.equal(fmt(1234.567, 1), '1,234.6')
  assert.equal(fmt(1234.5678, 4), '1,234.5678')
})

test('**超出**的小数位是四舍五入，不是截断 —— 这就是 fmt 与整数分口径的分歧', () => {
  // fmt 是浮点的：它只能四舍五入，因为它拿到手时那个小数已经不准了。
  // 金额那一族（凭证页、期初页）走的是整数分 moneyText：定点截到分，一分不差。
  // 两边口径不同，各用各的，别拿 fmt 的结果去判平。
  assert.equal(fmt(1.005), '1.01')
  assert.equal(fmt(2.675), '2.68')
})

console.log('')
console.log('== moneyText：整数分 → 文本（前端内部的金额口径） ==')

test('整数分落成两位小数', () => {
  assert.equal(moneyText(0n), '0.00')
  assert.equal(moneyText(1n), '0.01')
  assert.equal(moneyText(100n), '1.00')
  assert.equal(moneyText(12345n), '123.45')
})

test('负数带负号（红字）', () => {
  assert.equal(moneyText(-5n), '-0.05')
  assert.equal(moneyText(-12345n), '-123.45')
})

test('带千分位', () => {
  assert.equal(moneyText(123456789n), '1,234,567.89')
  assert.equal(moneyText(-123456789n), '-1,234,567.89')
})

test('18 位整数一分不差（浮点在这里已经不是原来那个数了）', () => {
  assert.equal(moneyText(99999999999999999999n), '999,999,999,999,999,999.99')
  // 同一个数走 fmt（浮点）会被改小 —— 这正是两套口径必须分家的原因
  assert.notEqual(fmt('999999999999999999.99'), '999,999,999,999,999,999.99')
})
