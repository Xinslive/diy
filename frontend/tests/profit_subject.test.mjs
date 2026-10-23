/**
 * 期末结账页的「本年利润科目」（**线上那份源码**）。
 *
 * 钉住用户点名的那件事：**本年利润科目是一个就地可选的字段** ——
 * 不是一个「设置」按钮 + 一个弹窗。理由：它不是表单里一项待提交的输入，
 * 它就是「结转冲到哪里」这个事实本身；值本身就是控件，选中即生效。
 * 多摆一层「设置」+「保存」，只会让人怀疑刚才那一下到底算不算数。
 *
 * 只看源码就下断言：它说的都是「界面上有没有什么东西」，
 * 真跑起来点一遍只能证明点得动，证明不了它没被换成一个别的控件。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const PERIOD = fs.readFileSync(
  new URL('../src/views/period/PeriodPanel.vue', import.meta.url), 'utf8')
const API = fs.readFileSync(
  new URL('../src/api/book.js', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 本年利润科目：就地可选的字段 ==')

t('结转损益那块有一个绑定 profitFormCode 的下拉', () => {
  const tile = PERIOD.slice(
    PERIOD.indexOf('<!-- 结转损益 -->'), PERIOD.indexOf('<!-- 结账 -->'))
  assert.match(tile, /<el-select[^>]*v-model="profitFormCode"/)
  assert.match(tile, /class="tile-profit-select"/)
})

t('没有「设置」按钮，也没有为它开的弹窗', () => {
  // 这两个曾经都在：一条「设置」链接 + 一个带两段说明文字的 el-dialog。
  // 现在值本身就是控件，多这两样只是让人多按一次。
  assert.doesNotMatch(PERIOD, /openProfitDialog/)
  assert.doesNotMatch(PERIOD, /profitDlgOpen/)
  assert.doesNotMatch(PERIOD, /设置本年利润科目/)
})

t('下拉里第一项是「自动识别」，而且它是一个真选项', () => {
  // 「自动识别」必须能被**选中并显示出来**，不能退化成占位文字 ——
  // el-select 的值是空串时走的是「没选」那条路，界面上摆出来的是占位符，
  // 于是用户看到的是「请选择」，而不是「自动识别」。
  assert.match(PERIOD, /const AUTO = '__auto__'/)
  assert.match(PERIOD, /key: 'auto',\s*\n\s*code: AUTO,/)
  // 选中「自动识别」= 清掉手工设定（存 null，不是空串）
  assert.match(PERIOD, /code === AUTO \? null : code/)
})

t('选中即生效：改值直接调接口，没有「保存」按钮', () => {
  assert.match(PERIOD, /@change="onProfitChange"/)
  assert.match(PERIOD, /async function onProfitChange\(code\)/)
  assert.match(PERIOD, /api\.setProfitSubject\(store\.bookId, cur\.year,/)
  // 失败时把下拉拨回原值：界面上留着一个没存上的选择，
  // 用户会以为已经改了（然后结转时冲错了科目）。
  assert.match(PERIOD, /profitFormCode\.value = before \|\| AUTO/)
})

t('找不到本年利润科目时，结转按钮禁用并说明要做什么', () => {
  assert.match(PERIOD, /profitMissing/)
  assert.match(PERIOD, /profitMissing \? '先设本年利润科目'/)
  // 找不到 ≠ 还没问过：网络故障不能被说成「你这本账缺科目」
  assert.match(PERIOD, /profitAsked\.value && !!profit\.value && !profit\.value\.subject/)
})

t('接口层：路径与「清空 = 自动识别」的语义', () => {
  assert.match(API, /getProfitSubject = \(id, year\) =>\s*\n\s*http\.get\(`\/books\/\$\{id\}\/years\/\$\{year\}\/profit-subject`\)/)
  assert.match(API, /setProfitSubject = \(id, year, code\) =>[\s\S]{0,160}code: code \|\| null/)
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
