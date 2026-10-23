/**
 * 凭证列表「分录金额」筛选框要放得下完整的金额
 * （src/views/voucher/VoucherList.vue，**线上那份源码**）。
 *
 *  用户报的现象是「金额不能显示全」：填 4421.79，框里只显示「4421.7」。
 *
 *  原因不是输入框没给够宽度，而是 **el-input-number 的宽度有一截是内边距**：
 *  它的内层输入框只拿到「外宽 − 30px」（左右各 15px）。外宽 78px 时内层
 *  只剩 48px，而 14px Arial 下一个数字 7.79px —— 「4421.79」要 50.6px，
 *  **差 2.6px 就被截掉**。差这么一点看着像「金额不能显示全」。
 *
 *  下面按**实际字宽**算需要多宽，而不是拍一个数：字宽是实测值
 *  （14px Arial：数字 7.79px、小数点 3.89px），要放下的是八位整数加两位
 *  小数（99999999.99）。字宽一变（换字体、放大字号），这条断言会跟着不够用，
 *  于是它钉的是「放得下」这件事，不是「现在是多少像素」。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/VoucherList.vue', import.meta.url), 'utf8')

//: 14px Arial 实测字宽。
const DIGIT_W = 7.79
const DOT_W = 3.89
//: el-input-number 的内层输入框拿不到左右各 15px 的内边距。
const INNER_PADDING = 30
//: 要放得下的样本：八位整数 + 两位小数。
const SAMPLE = '99999999.99'

const needed = (s) => [...s].reduce(
  (w, ch) => w + (ch === '.' ? DOT_W : DIGIT_W), 0) + INNER_PADDING

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

/** 两个金额筛选框的外宽（px）。 */
const widths = () => {
  const out = []
  const re = /<el-input-number[^>]*v-model="query\.(?:min|max)_amount"[\s\S]*?style="width:\s*(\d+)px"/g
  let m
  while ((m = re.exec(VIEW))) out.push(Number(m[1]))
  return out
}

console.log('== 金额筛选框要放得下完整的金额 ==')

t('两个金额筛选框都在（从 / 到）', () => {
  assert.equal(widths().length, 2, '没找到两个金额筛选框')
})

t('宽度够放下「' + SAMPLE + '」', () => {
  const need = needed(SAMPLE)
  for (const w of widths()) {
    assert.ok(w >= need,
      `外宽 ${w}px 只剩 ${w - INNER_PADDING}px 字宽，`
      + `放不下「${SAMPLE}」（要 ${need}px）`)
  }
})

t('两个框一样宽（一个能显示、一个不能，就是半吊子）', () => {
  const [a, b] = widths()
  assert.equal(a, b, `两个金额框宽度不一样：${a} / ${b}`)
})

t('框里写清楚了为什么是这个宽度', () => {
  assert.match(VIEW, /金额框要放得下\*\*完整的金额\*\*/,
    '宽度改了以后没人知道差在哪 —— 注释是这个数的来历')
})

console.log('')
console.log('通过 ' + pass + ' / 失败 ' + fail)
if (fail) process.exit(1)
