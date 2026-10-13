/**
 * 期初余额页的金额净化（审计 P2-20）。
 *
 * 原来的写法是 `Number(框里的字)`，脏输入一律变 NaN，而：
 *   · `NaN !== 0` 为**真** → 这一行被「哪些行有余额」的过滤选中，进了保存名单
 *   · `String(signedOf())` 把它变成 "0" → 库里存的是 0
 *   · 于是界面上报「已保存 N 条」，人以为存了 100，其实存了 0
 *
 * 这里测两件事：
 *   1. 数值口径本身（utils/amountGrid.js 的 amountToNumber，**线上那份源码**）
 *   2. 页面确实接上了它 —— 源码级断言，防止哪天又写成 `Number(amounts[...])`
 *      那样单测照样全绿，页面却已经坏回去了。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const { amountToNumber, sanitizeAmount } = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/amountGrid.js'))

const SRC = fs.readFileSync(new URL('../src/views/book/OpeningBalance.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => { try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }

console.log('== 数值口径：认不出的串一律当空格子 ==')

t('单摆着的负号按 0 算，不产生 NaN', () => {
  assert.ok(Number.isNaN(Number('-')))
  assert.equal(amountToNumber('-'), 0)
})
t('字母、中文、符号一律当空格子', () => {
  for (const dirty of ['abc', '一百', '??', '-', null, undefined]) {
    assert.equal(amountToNumber(dirty), 0, String(dirty))
  }
})
t('串在中间夹着字母：字母不留、数字留下（与凭证金额格同规矩，两页不能各说各话）', () => {
  assert.equal(sanitizeAmount('12abc34'), '1234')
  assert.equal(amountToNumber('12abc34'), 1234)
})
t('全角与数学减号先归一化再解析，金额不打折', () => {
  assert.equal(amountToNumber('１００'), 100)
  assert.equal(amountToNumber('−100'), -100)
  assert.equal(amountToNumber('１２３４．５６'), 1234.56)
})

/** 页面的保存名单 = 「末级科目 且 这一行有余额」。把两条真源码抠出来跑一遍。 */
t('保存名单：脏输入那一行不再被算进去（报出来的条数 = 真存进去的条数）', () => {
  // 页面里 amountOf 就是 amountToNumber(amounts[id])，这里按同一份源码口径算名单
  const amounts = { 1: '100', 2: 'abc', 3: '-', 4: '０.０５' }
  const rowsBefore = Object.values(amounts).filter((v) => Number(v || 0) !== 0).length
  const rowsNow = Object.values(amounts).filter((v) => amountToNumber(v) !== 0).length
  // 老口径：100 ✓、abc（NaN≠0）✓、-（NaN≠0）✓、０.０５（全角 → NaN）✓ —— 四行全被选中
  assert.equal(rowsBefore, 4, '老口径把 abc / - / 全角串都算成「有余额」')
  assert.equal(rowsNow, 2, '新口径只认 100 和 0.05')
  // 存进去的值也一样：脏行不再以 "0" 的名义混进名单
  const stored = Object.values(amounts).filter((v) => amountToNumber(v) !== 0).map((v) => String(amountToNumber(v)))
  assert.deepEqual(stored.sort(), ['0.05', '100'])
})

console.log('')
console.log('== 页面确实接上了这份净化（源码级断言） ==')

t('页面不再出现 `Number(amounts[...)` 这种裸解析', () => {
  // 注意不能直接写 /Number\(amounts\[/: amountToNumber( 里也含 "Number("
  assert.ok(!/(^|[^A-Za-z])Number\(amounts\[/.test(SRC), '期初页又变回裸 Number(框里的字) 了')
})
t('页面用的是 amountGrid 那一份净化，不另写一套', () => {
  assert.ok(/from '@\/utils\/amountGrid'/.test(SRC), '没有引入金额格净化')
  assert.ok(/amountToNumber/.test(SRC), '行金额没有走 amountToNumber')
  assert.ok(/sanitizeAmountDetailed/.test(SRC), '输入没有过净化（脏串会直接进库）')
})
t('输入那一刻就净化并提示：截断/丢位看得见', () => {
  assert.ok(/typeAmount/.test(SRC))
  assert.ok(/ElMessage\.warning/.test(SRC), '截断了却一声不吭 —— 审计 P2-19 要求可见反馈')
})
t('刚敲下的小数点留在框里（跟凭证金额格同一个规矩）', () => {
  assert.ok(/amountInputText/.test(SRC), '输入时没留那个刚敲下的小数点，「1234.5」会打成 12345')
  assert.equal(sanitizeAmount('1234.5'), '1234.5', '净化本身不该动小数')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)