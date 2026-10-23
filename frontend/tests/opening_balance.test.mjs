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
import { moneyText as moneyTextOf } from '../src/utils/format.js'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const { amountToNumber, sanitizeAmount, moneyCents, absMoneyText } = await createServer({
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
  assert.ok(/moneyCents/.test(SRC), '行金额没有走定点分（moneyCents）')
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
console.log('== P0-1 读回来的金额不许被改小 ==')
//
// 这一节以前**一条断言都没有** —— 缺口就在这儿：
// 页面里的净化函数测得很细（「脏串当空格子」那些），但**没人测「谁在什么时候调用它」**，
// 于是读库路径（load()）过了一遍净化这件事谁也没发现：
// 打开期初页 → 点保存 → 库里那个 10 位金额被改小，界面全程显示「借贷平衡」。
//
// 下面几条从**页面源码**里抠出金额那一段来跑：测的是页面上真在用的那份代码。

const START = '// ---- 金额：一律整数分'
const END = '// ---- 金额段结束'

/** 抠出金额那一段，并自检抠到的是它（抠空了就让断言失败，别让测试空转）。 */
function amountBlock() {
  const a = SRC.indexOf(START)
  const b = SRC.indexOf(END)
  assert.ok(a !== -1, `源码里找不到标记 ${START}`)
  assert.ok(b > a, `源码里找不到结束标记 ${END}`)
  const block = SRC.slice(a, b)
  for (const fn of ['centsOfRow', 'signedCents', 'signedText', 'totalsCents', 'moneyText']) {
    assert.ok(block.includes(`function ${fn}(`), `抠出来的段里没有 ${fn}() —— 标记挪位置了？`)
  }
  return block
}
const BLOCK = amountBlock()

/**
 * 用真实的金额实现搭一套页面状态。
 * amounts / sides 就是页面里那两个 reactive 对象（读回路径写进去的那两个）。
 *
 * moneyTextOf 是 utils/format.js 的那份统一格式规则（整数分 → 文本）：
 * 页面里的 moneyText 现在只是它的委托，所以这里要把那一份注入进来。
 */
function pageState(amounts, sides) {
  const factory = new Function('amounts', 'sides', 'moneyCents', 'absMoneyText', 'moneyTextOf', `${BLOCK}
    return { centsOfRow, signedCents, signedText, totalsCents, moneyText }`)
  return factory(amounts, sides, moneyCents, absMoneyText, moneyTextOf)
}

t('load() 不用净化：读回来的数直接用后端给的字符串', () => {
  // 这一行以前是 `sanitizeAmount(s.amount)` —— 10 位以上的合法金额被静默截成 9 位。
  assert.ok(SRC.includes("amounts[s.id] = s.is_leaf ? String(s.amount"),
    '读回路径又改回净化了 —— 读回来的数会被改小')
  // load() 那一段里不许再出现 sanitizeAmount
  const a = SRC.indexOf('async function load()')
  const b = SRC.indexOf('async function save()')
  assert.ok(a !== -1 && b > a, '源码里找不到 load()')
  // 只看**代码**（注释里可以写「以前这里过了一遍 sanitizeAmount」）
  const code = SRC.slice(a, b).split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n')
  assert.ok(!code.includes('sanitizeAmount'), 'load() 里还有净化 —— 读回来的数会被改小')
})

t('端到端（同构模拟）：载入 10 位金额 → 保存回去，一个字符都不变', () => {
  // 后端 GET /openings 返回 "1241610200.12" → 页面 → 点保存 → 发出去的还是它。
  const amounts = { 1: '1241610200.12' }
  const sides = { 1: '借' }
  const p = pageState(amounts, sides)
  const row = { id: 1, is_leaf: true }
  assert.equal(p.signedText(row), '1241610200.12', '往返一次把余额改小了')
  assert.equal(p.centsOfRow(row), 124161020012n, '定点也不许掉精度')
})

t('模型上限（18 位整数）往返也不变', () => {
  const amounts = { 1: '999999999999999999.99' }
  const sides = { 1: '贷' }
  const p = pageState(amounts, sides)
  const row = { id: 1, is_leaf: true }
  assert.equal(p.signedText(row), '-999999999999999999.99', '往返一次把余额改小了')
  assert.equal(p.centsOfRow(row), 99999999999999999999n)
  // 浮点会把它改小 —— 这正是 P0-1 的另一种形态
  assert.notEqual(String(Number('999999999999999999.99')), '999999999999999999.99')
})

t('判平口径与凭证页一致：整数分严格相等（浮点残差那一组必须判平）', () => {
  const p = pageState(
    { 1: '100.10', 2: '200.20', 3: '300.30' },
    { 1: '借', 2: '借', 3: '贷' }
  )
  const subjects = [{ id: 1, is_leaf: true }, { id: 2, is_leaf: true }, { id: 3, is_leaf: true }]
  const t = p.totalsCents(subjects)
  assert.equal(t.dr, 30030n)
  assert.equal(t.cr, 30030n)
  assert.equal(t.dr === t.cr, true, '100.10+200.20 对 300.30 必须判平（浮点残差那一组）')
})

t('差一分钱就是不平（不再有 0.005 的浮点容差）', () => {
  const p = pageState({ 1: '100.10', 2: '200.21' }, { 1: '借', 2: '贷' })
  const subjects = [{ id: 1, is_leaf: true }, { id: 2, is_leaf: true }]
  const t = p.totalsCents(subjects)
  assert.equal(t.dr === t.cr, false, '差 0.11 还判成平了')
  assert.equal(t.dr, 10010n)
  assert.equal(t.cr, 20021n)
})

t('拉数失败时不留上一份数据（不能出现「顶栏 2027、内容 2026」）', () => {
  const a = SRC.indexOf('async function load()')
  const b = SRC.indexOf('async function save()')
  const load = SRC.slice(a, b)
  assert.ok(load.includes('try {'), 'load() 没有 try/catch —— 失败后留着上一份数据')
  assert.ok(load.includes('clearData()'), 'load() 失败时没有清空这一页')
  assert.ok(SRC.includes('loadError'), '没有把失败这件事说出来')
})

t('年份没定下来就不去问后端（问回来的是启用期间那一份）', () => {
  const a = SRC.indexOf('async function load()')
  const b = SRC.indexOf('async function save()')
  const load = SRC.slice(a, b)
  assert.ok(load.includes('store.currentYear === undefined'), 'currentYear 没定也去问后端')
})

console.log('')
console.log('== 期初是**年度**口径（2026-10-20 改） ==')

t('页面按**年度**取数，不按期间', () => {
  assert.ok(/store\.currentYear/.test(SRC), '期初页没跟着顶栏的年度走')
  assert.ok(!/getOpenings\(store\.bookId,\s*store\.periodId\)/.test(SRC),
    '还在按期间取数 —— 切一个月换一份期初，这条老口径已经废了')
})
t('标题写的是「年度期初余额」', () => {
  assert.ok(/年度期初余额/.test(SRC), '标题没写年度')
  assert.ok(!/data\.period\}\s*期初余额/.test(SRC), '标题还在按期间写')
})
t('保存与导出也按年度', () => {
  assert.ok(/setOpenings\(store\.bookId,\s*rows,\s*store\.currentYear\)/.test(SRC),
    '保存还在按期间写')
  assert.ok(/year:\s*store\.currentYear/.test(SRC), '导出还在按期间导')
})
t('换月份不重挂这一页（否则刚录了一半的数就没了）', () => {
  const LAYOUT = fs.readFileSync(
    new URL('../src/layouts/MainLayout.vue', import.meta.url), 'utf8')
  assert.ok(/route\.name === 'openings'/.test(LAYOUT),
    '期初页的 viewKey 没有按年度走 —— 切一下月份整页重挂，用户录的数直接没了')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)