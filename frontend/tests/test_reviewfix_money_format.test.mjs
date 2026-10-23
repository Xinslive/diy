/**
 * 审查 L-29：金额格式化有 4 套并行实现 —— 同一个业务对象，两个界面，两个数。
 *
 * 最刺眼的是期初**导入预览弹窗**对 1.005 显示 "1.005"，
 * 而期初**余额页**显示 "1.00"：两个界面讲的是同一笔期初余额。
 *
 * 根因：弹窗用的是 `(Number(v || 0)).toLocaleString('zh-CN', { minimumFractionDigits: 2 })` ——
 * 浮点，而且 `maximumFractionDigits` 没给（默认 3），于是多出来的小数位原样留着。
 *
 * 修法：统一到**整数分**（BigInt）+ utils/format.js 的 moneyText ——
 * 定点到分、两位小数、带千分位。期初余额页与凭证页用的是同一份。
 *
 * 这里从**真实源码**里抠出两处的实现跑一遍，比对它们对同一个数的输出 ——
 * 测的是「两个界面说得一样」，不是「源码里有没有 moneyText 这个词」。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { moneyText } from '../src/utils/format.js'
import { moneyCents } from '../src/utils/amountGrid.js'

const DIALOG = fs.readFileSync(
  new URL('../src/components/OpeningImportDialog.vue', import.meta.url), 'utf8')
const PAGE = fs.readFileSync(
  new URL('../src/views/book/OpeningBalance.vue', import.meta.url), 'utf8')

/** 抠出 `const <name> = ...` 那一行（箭头函数，表达式体）。 */
function grabConst(src, name) {
  const m = new RegExp(`const\\s+${name}\\s*=\\s*`).exec(src)
  assert.ok(m, `源码里找不到 const ${name} =`)
  const start = m.index + m[0].length
  const lineEnd = src.indexOf('\n', start)
  return src.slice(start, lineEnd).trim().replace(/;$/, '')
}

/** 抠出 `function <name>(...) {...}` 的整段源码（按大括号配平）。 */
function grabFn(src, name) {
  const m = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(src)
  assert.ok(m, `源码里找不到 function ${name}(`)
  const i = m.index
  let depth = 0, j = src.indexOf('(', i)
  for (; j < src.length; j++) {
    if (src[j] === '(') depth++
    else if (src[j] === ')') { depth--; if (depth === 0) break }
  }
  assert.equal(src[j], ')', `${name} 的参数表没配平`)
  const bodyStart = src.indexOf('{', j)
  depth = 0
  for (j = bodyStart; j < src.length; j++) {
    if (src[j] === '{') depth++
    else if (src[j] === '}') { depth--; if (depth === 0) break }
  }
  const out = src.slice(i, j + 1)
  assert.ok(out.endsWith('}'), `${name} 抠出来的不是一段完整的函数`)
  return out
}

/** 弹窗里那个 money（箭头函数）：用真的 moneyText / moneyCents 注入。 */
function dialogMoney() {
  const expr = grabConst(DIALOG, 'money')
  assert.match(expr, /^\(.*\)\s*=>/, `抠出来的不是箭头函数：${expr}`)
  const factory = new Function('moneyText', 'moneyCents', `return (${expr})`)
  return factory(moneyText, moneyCents)
}

/** 期初余额页那个 moneyText（本地函数，委托给统一的那一份）。 */
function pageMoneyText() {
  const src = grabFn(PAGE, 'moneyText')
  const factory = new Function('moneyTextOf', `${src}\nreturn moneyText`)
  return factory(moneyText)
}

/** 只看**代码行**（注释里可以写「以前这里是 toLocaleString」）。 */
function codeLines(src) {
  return src.split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('//') && !l.startsWith('*') && !l.startsWith('/*'))
    .join('\n')
}

test('导入预览弹窗不再用浮点的 toLocaleString（多出来的小数位就是这么来的）', () => {
  assert.doesNotMatch(codeLines(DIALOG), /toLocaleString\(/,
    '弹窗又变回浮点格式化了 —— 1.005 会显示成 1.005，跟期初页对不上')
  assert.match(codeLines(DIALOG), /moneyText\(moneyCents\(v\)\)/,
    '弹窗没有走整数分 + 统一格式（moneyText(moneyCents(v))）')
})

test('期初余额页的 moneyText 委托给统一的那一份（别再各写各的）', () => {
  const src = grabFn(PAGE, 'moneyText')
  assert.match(src, /moneyTextOf\(cents\)/,
    '期初页又自己写了一份格式规则 —— 两个界面迟早又对不上')
  assert.match(PAGE, /from '@\/utils\/format'/, '期初页没有引入统一的格式规则')
})

test('**行为**：两个界面对同一个数说得一样', () => {
  const d = dialogMoney()
  const p = pageMoneyText()
  for (const v of ['1.005', '100.10', '0', '-5', '1,234.56', '999999999999999999.99', 1.005, 100.1, 0]) {
    // 弹窗收的是「元」，期初页收的是「分」—— 各自按自己的口径算
    const fromDialog = d(v)
    const fromPage = p(moneyCents(v))
    assert.equal(fromDialog, fromPage,
      `${JSON.stringify(v)}：导入预览显示 ${fromDialog}，期初页显示 ${fromPage} —— 同一笔钱两个数`)
  }
})

test('1.005 两边都是 1.00（以前弹窗显示 1.005）', () => {
  assert.equal(dialogMoney()('1.005'), '1.00')
  assert.equal(pageMoneyText()(moneyCents('1.005')), '1.00')
})

test('两位小数 + 千分位（弹窗收的是「元」，期初页收的是「分」）', () => {
  assert.equal(dialogMoney()('1234567.89'), '1,234,567.89')
  assert.equal(dialogMoney()('0.1'), '0.10')
  assert.equal(dialogMoney()('-0.05'), '-0.05')
  assert.equal(dialogMoney()('-5'), '-5.00')
})

test('18 位整数不被浮点改小', () => {
  // 浮点那条路会把它变成 1,000,000,000,000,000,000.00
  assert.equal(dialogMoney()('999999999999999999.99'), '999,999,999,999,999,999.99')
})
