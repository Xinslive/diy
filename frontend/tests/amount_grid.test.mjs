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

t('10 位整数照样截到 9 位（格只有 亿…元 九格），但**必须说出来**', () => {
  assert.equal(sanitizeAmount('1234567890'), '123456789')
  const { value, notices } = sanitizeAmountDetailed('1234567890')
  assert.equal(value, '123456789')
  assert.equal(notices.length, 1, '截断了却一声不吭 —— 人按回车才发现少了几位')
  assert.equal(notices[0].code, 'INT_CLIPPED')
  assert.ok(notices[0].text.includes('10'), '提示里要出现被丢的那一位：' + notices[0].text)
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
