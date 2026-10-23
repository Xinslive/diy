/**
 * 凭证列表「分录金额」筛选框要放得下完整的金额，又不能把框撑大
 * （src/views/voucher/VoucherList.vue，**线上那份源码**）。
 *
 *  用户报的现象是「金额不能显示全」：填 4421.79，框里只显示「4421.7」。
 *  第一次改法是**把框加宽**到 120px，用户回头说「宽度太大了，这不好看」。
 *
 *  真正的原因是 **el-input-number 的宽度有一截是内边距**：左右各 15px
 *  那是给加减按钮留的，这两个框没有按钮（:controls="false"），
 *  30px 全是浪费。外宽 78px 时内层只剩 48px，而 14px Arial 下一个数字
 *  7.79px —— 「4421.79」要 50.6px，**差 2.6px 就被截掉**。
 *
 *  修法是**收内边距，不是加宽**：内边距收到 8px，92px 的框给出 76px 字宽。
 *  于是这条用例同时钉两件事：
 *
 *  ① 放得下（字宽 ≥ 样本金额的宽度）；
 *  ② **没有靠加宽蒙混**（外宽有上限）—— 只加宽的话过了①却过了②，
 *     框会一天比一天宽。
 *
 *  下面按**实际字宽**算，不拍像素数：字宽是实测值（14px Arial：数字 7.79px、
 *  小数点 3.89px）。字宽一变（换字体、放大字号），这条断言会跟着不够用，
 *  钉的是「放得下」这件事，不是「现在是多少像素」。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/VoucherList.vue', import.meta.url), 'utf8')

//: 14px Arial 实测字宽。
const DIGIT_W = 7.79
const DOT_W = 3.89
//: 收掉之后 el-input-number 的左右内边距（各 8px）。
const PADDING = 8
//: 要放得下的样本：七位整数 + 两位小数。
const SAMPLE = '9999999.99'
//: 外宽上限：框不是靠加宽来解决的。
const MAX_OUTER = 100

const textWidth = (s) => [...s].reduce(
  (w, ch) => w + (ch === '.' ? DOT_W : DIGIT_W), 0)

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

console.log('== 金额筛选框：放得下，但别把框撑大 ==')

t('两个金额筛选框都在（从 / 到）', () => {
  assert.equal(widths().length, 2, '没找到两个金额筛选框')
})

t('宽度够放下「' + SAMPLE + '」', () => {
  const need = textWidth(SAMPLE)
  for (const w of widths()) {
    assert.ok(w - PADDING * 2 >= need,
      `外宽 ${w}px 减去内边距只剩 ${w - PADDING * 2}px 字宽，`
      + `放不下「${SAMPLE}」（要 ${need.toFixed(1)}px）`)
  }
})

t('框没有被撑大（加宽不是修法）', () => {
  for (const w of widths()) {
    assert.ok(w <= MAX_OUTER, `外宽 ${w}px —— 靠加宽解决的，一眼就看得出来`)
  }
})

t('内边距确实收掉了（左右各 8px）', () => {
  assert.match(VIEW, /\.amt-input\s*:deep\(\.el-input__wrapper\)\s*\{[^}]*padding-left:\s*8px/,
    '没有收掉 el-input-number 那 30px 内边距 —— 字宽还是不够')
  assert.match(VIEW, /\.amt-input\s*:deep\(\.el-input__wrapper\)\s*\{[^}]*padding-right:\s*8px/,
    '右边距没收掉')
})

t('两个框都挂了 amt-input 这个类（不然收内边距的那条规则落不到它们身上）', () => {
  const n = (VIEW.match(/class="amt-input"/g) || []).length
  assert.equal(n, 2, '只有 ' + n + ' 个框挂了 amt-input')
})

t('框里写清楚了为什么是这个宽度', () => {
  assert.match(VIEW, /金额框要放得下\*\*完整的金额\*\*/,
    '宽度改了以后没人知道差在哪 —— 注释是这个数的来历')
})

console.log('')
console.log('通过 ' + pass + ' / 失败 ' + fail)
if (fail) process.exit(1)
