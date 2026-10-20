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
const {
  toggleAmountSign, sanitizeAmount, sanitizeAmountDetailed, amountInputText,
  amountToNumber, caretBoundary, distributeAmount,
  MAX_INT_DIGITS, MAX_MONEY_INT_DIGITS, inspectAmount,
  moneyCents, centsToText, absMoneyText,
} = await createServer({
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
console.log('== P2-19 全角 / 中文符号要归一化，不是清空 ==')

t('全角数字是同一个数：认回来，不是当垃圾剔掉', () => {
  assert.equal(sanitizeAmount('１２３'), '123', '以前这一条返回空串 —— 人看着像金额格坏了')
  assert.equal(sanitizeAmount('１，２３４．５６'), '1234.56')
})
t('中文/全角句点是小数点，不是垃圾（丢掉它金额凭空大十倍）', () => {
  assert.equal(sanitizeAmount('1。5'), '1.5')
  assert.equal(sanitizeAmount('1．5'), '1.5')
})
t('U+2212 数学减号按负号处理：冲销不能变成再记一遍', () => {
  // 这一条就是本文件开头点名「记账里最贵的一种错」那一种：
  // 真减号被当垃圾字符剔掉，-300 变 300，方向反了
  assert.equal(sanitizeAmount('−100'), '-100')
  assert.equal(sanitizeAmount('－100'), '-100', '全角连字符')
  assert.equal(sanitizeAmount('− 1 0 0'), '-100', '带空格的数学减号')
  assert.equal(distributeAmount('−100').join('').trim(), '−100', '落格后仍然是负的那一笔')
})
t('正号不产生负号', () => {
  assert.equal(sanitizeAmount('＋100'), '100')
  assert.equal(sanitizeAmount('100'), '100')
})
t('U+2212 也认得出「整格翻负」：翻一次回正，再翻一次回负', () => {
  assert.equal(toggleAmountSign('−100'), '100')
  assert.equal(toggleAmountSign(toggleAmountSign('−100')), '-100')
})

console.log('')
console.log('== P2-19 超长输入必须给出可见反馈 ==')

t('10 位整数**不再被截短**：值原样保留，只说「这一格放不下」', () => {
  // 这一条以前断言的是「10 位照样截到 9 位」—— 把**截断**当成了规范。
  // 那正是 P0-1 能活到今天的原因：金额格只有 9 格是**画格子**的事，
  // 而「把读回来的数改小」是**改数** —— 期初余额往返一次就把余额改小了。
  // 截断不能是期望行为：值必须原样保留，由界面说清楚放不下。
  const { value, notices } = sanitizeAmountDetailed('1234567890')
  assert.equal(value, '1234567890', '值不许被改小')
  assert.equal(sanitizeAmount('1234567890'), '1234567890')
  assert.equal(notices.length, 1)
  assert.equal(notices[0].code, 'INT_OVERFLOW', '这是「放不下」，不是「丢了几位」')
  assert.ok(notices[0].text.includes('9'), '提示里要说出格子的容量：' + notices[0].text)
})
t('小数超过分位：截断 + 说明', () => {
  const { value, notices } = sanitizeAmountDetailed('1234.5678')
  assert.equal(value, '1234.56')
  assert.equal(notices[0].code, 'DEC_CLIPPED')
})
t('没截断时不啰嗦（提示只对真丢了东西的时候说话）', () => {
  assert.deepEqual(sanitizeAmountDetailed('1234.56').notices, [])
  assert.deepEqual(sanitizeAmountDetailed('１２３').notices, [], '全角归一化没丢东西，不该报警')
  assert.deepEqual(sanitizeAmountDetailed('').notices, [])
  assert.deepEqual(sanitizeAmountDetailed('-').notices, [])
})

console.log('')
console.log('== P0-1 读回来的金额不许被改小（截断不能是期望行为）==')
//
// 这一节以前**一条断言都没有** —— 缺口就在这儿：
// 上面那条「10 位照样截到 9 位」守的是**输入路径的提示**，
// 而**读库路径**（期初页 load()、凭证金额格的显示）从来没被测过，
// 于是「读回来的合法值被改小」一路畅通（审查 P0-1 / P-43）。
//
// 下面每一条都是读库路径的契约：值原样留着，改小一个字符都不行。

t('带小数的 10 位整数原样保留（P0-1 的核心断言）', () => {
  assert.equal(sanitizeAmountDetailed('1234567890.12').value, '1234567890.12')
  assert.equal(sanitizeAmount('1234567890.12'), '1234567890.12')
  assert.equal(sanitizeAmount('1000000000.00'), '1000000000.00')
})

t('模型上限（18 位整数）以内一个字符都不动', () => {
  assert.equal(sanitizeAmountDetailed('999999999999999999.99').value, '999999999999999999.99')
  assert.equal(sanitizeAmount('999999999.99'), '999999999.99', '9 位是安全线')
})

t('超过模型上限（18 位整数）才拒绝：不进值 + MONEY_LIMIT', () => {
  const tooBig = '1' + '0'.repeat(20)
  const { value, notices } = sanitizeAmountDetailed(tooBig)
  assert.notEqual(value, tooBig, '超限的数不许进值')
  assert.equal(value, '', '超限的值一律不进（拒绝，不是截断）')
  assert.ok(notices.some((n) => n.code === 'MONEY_LIMIT'), '必须说清是超了模型上限')
})

t('两个上限各管各的：格容量 9 位、模型上限 18 位', () => {
  assert.equal(MAX_INT_DIGITS, 9, '金额格的物理容量（亿…元 九格）')
  assert.equal(MAX_MONEY_INT_DIGITS, 18, '数据模型上限（与后端 MONEY_LIMIT 对齐）')
  assert.equal(inspectAmount('123456789.12').overGrid, false)
  assert.equal(inspectAmount('1234567890.12').overGrid, true)
  assert.equal(inspectAmount('1234567890.12').overModel, false)
  assert.equal(inspectAmount('999999999999999999.99').overGrid, true)
  assert.equal(inspectAmount('999999999999999999.99').overModel, false)
  assert.equal(inspectAmount('1' + '0'.repeat(20)).overModel, true)
})

t('显示层不许再给出第二个（被改小的）数：放不下就不落格', () => {
  // 落格层以前会把 1234567890.12 摆成另一个数（截小的或截尾的）——
  // 同一屏上分录格与合计行差 10 倍，人只能靠猜哪个对（审查 P1-1）。
  // 现在放不下就**不落格**（由显示层改用「原值 + 角标」摆出来）。
  const cells = distributeAmount('1234567890.12')
  assert.equal(cells.length, 11)
  assert.ok(cells.every((c) => c === ''), '放不下就不落格 —— 宁可不摆，也不摆一个错的数')
  assert.equal(cells[0], '', '第 0 格留空，不显示改小的数')
})

t('定点：一分不差，18 位也不掉精度（浮点会改小）', () => {
  assert.equal(moneyCents('1234567890.12'), 123456789012n)
  assert.equal(moneyCents('1241610200.12'), 124161020012n)
  assert.equal(moneyCents('999999999999999999.99'), 99999999999999999999n)
  assert.equal(moneyCents('0.05'), 5n)
  assert.equal(moneyCents('abc'), 0n)
  assert.equal(moneyCents('-'), 0n)
  assert.equal(centsToText(moneyCents('1234567890.12')), '1234567890.12')
  assert.equal(centsToText(moneyCents('-0.05')), '-0.05')
  // 浮点会把同一个数改小 —— 这正是 P0-1 的另一种形态
  assert.notEqual(String(Number('999999999999999999.99')), '999999999999999999.99')
})

t('绝对值走字符串：原值一个字符都不动', () => {
  assert.equal(absMoneyText('-1241610200.12'), '1241610200.12')
  assert.equal(absMoneyText('1241610200.12'), '1241610200.12')
  assert.equal(absMoneyText('999999999999999999.99'), '999999999999999999.99')
  assert.equal(absMoneyText(''), '')
})

console.log('')
console.log('== 认不出来的串一律当空格子（期初余额页靠这条，审计 P2-20） ==')

t('单摆着的负号是空格子，不是 NaN', () => {
  assert.ok(Number.isNaN(Number('-')), 'Number("-") 确实是 NaN —— 这正是坑')
  assert.equal(amountToNumber('-'), 0)
  assert.equal(amountToNumber(''), 0)
  assert.equal(amountToNumber('abc'), 0)
  assert.equal(amountToNumber(null), 0)
})
t('金额照常解析：全角先归一化，负数是真负数', () => {
  assert.equal(amountToNumber('１２３'), 123)
  assert.equal(amountToNumber('−100'), -100)
  assert.equal(amountToNumber('1234.56'), 1234.56)
})
t('NaN 不会被「哪些行有余额」的过滤条件选中（这就是「报了 N 条却存成 0」）', () => {
  const dirty = 'abc'
  const before = Number(dirty) !== 0          // 老写法：NaN !== 0 → 这行被选中
  assert.equal(before, true, '老过滤条件确实会选中脏行')
  assert.equal(amountToNumber(dirty) !== 0, false, '新写法必须把它当空格子')
})

console.log('')
console.log('== P3-7 只有负号时光标画错一格 ==')

t('caretBoundary("-", 0) = 9（负号不占位，第一个数字仍落在元位）', () => {
  assert.equal(caretBoundary('-', 0), 9, '以前是 8 —— 光标凭空短一格')
  assert.equal(caretBoundary('-', 1), 9, '负号两侧是同一条缝，它不是数位')
})
t('负数光标位置也不受负号影响', () => {
  assert.equal(caretBoundary('-1234', 5), 9, '末尾 = 元之后')
  assert.equal(caretBoundary('-1234', 1), 5, '负号后首位数之前')
  assert.equal(caretBoundary('-1234', 0), 5, '负号之前也是同一条缝')
  assert.equal(caretBoundary('1234', 4), 9, '不带负号时口径不变')
})
t('光标与落格反推对得上：任取一串，边界必落在 0-11 且非递减', () => {
  for (const v of ['', '-', '1234', '-1234', '1234.56', '-1234.56', '0.05']) {
    const s = sanitizeAmount(v)
    let prev = -1
    for (let p = 0; p <= s.length; p += 1) {
      const b = caretBoundary(v, p)
      assert.ok(b >= 0 && b <= 11, `${v}@${p} → ${b}`)
      assert.ok(b >= prev, `${v}@${p} 边界倒退了：${prev} → ${b}`)
      prev = b
    }
  }
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
