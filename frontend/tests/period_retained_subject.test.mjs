/**
 * 期末结账页的「未分配利润科目」这一行（2026-10-26 用户要加的）。
 *
 * 年度结转的**目的科目**是「未分配利润」：本年利润的余额冲到它上面，
 * 变成留存收益。它原先**只能靠编码猜**（310415 / 3104 / 4104），
 * 识别不出来时年度结转就结不了，而界面上没有地方能改 ——
 * 用户只能看着一句报错，无路可走。
 *
 * 现在它跟「本年利润科目」一样是**一个就地可选的字段**：
 * 值本身就是控件，选中即生效（没有「设置」按钮、也没有弹窗），
 * 下拉第一项是「自动识别」。
 *
 * 这些断言读的是**真实组件源码** —— 页面行为（选定即生效、失败回拨）
 * 由浏览器探针守着，这里守住的是「结构别被改没了」。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const PANEL = fs.readFileSync(
  new URL('../src/views/period/PeriodPanel.vue', import.meta.url), 'utf8')
const API = fs.readFileSync(
  new URL('../src/api/book.js', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

/** 年度结转那一块（从「年度结转」标题到块结束）。 */
const yearTile = PANEL.slice(PANEL.indexOf('年度结转'), PANEL.indexOf('整年进度'))

console.log('== 年度结转那一块里有一行「未分配利润」 ==')

t('年度结转卡片里摆着「未分配利润」这一行', () => {
  assert.ok(/未分配利润/.test(yearTile), '年度结转卡片里没有「未分配利润」这一行')
  assert.ok(/tile-profit-label">未分配利润</.test(yearTile), '「未分配利润」不是就地可选的字段那一行')
})

t('这一行是一个下拉（值本身就是控件，选中即生效）', () => {
  assert.ok(/v-model="retainedFormCode"/.test(yearTile), '下拉没有绑到 retainedFormCode')
  assert.ok(/@change="onRetainedChange"/.test(yearTile), '下拉没有走 onRetainedChange')
  assert.ok(!/保存|确定/.test(yearTile), '不该有「保存」按钮 —— 选中即生效')
})

t('下拉第一项是「自动识别」（选它 = 回到系统的自动口径）', () => {
  assert.ok(/const AUTO = '__auto__'/.test(PANEL), '自动识别那一项的哨兵值没了')
  assert.ok(/retainedFormCode\.value = r\.setting_code \|\| AUTO/.test(PANEL),
    '下拉的当前值没有跟着「手工设定值 / 自动识别」走')
  assert.ok(/retainedOptions[\s\S]{0,900}name: r\?\.subject \? `自动识别/.test(PANEL),
    '「自动识别」那一项的文案没了')
})

t('找不到未分配利润科目时整行转琥珀色，按钮禁用并说清要先设', () => {
  assert.ok(/:class="\{ warn: retainedMissing \}"/.test(yearTile), '缺科目时这一行没有转成警示色')
  assert.ok(/retainedMissing \? '先设未分配利润科目'/.test(yearTile), '按钮没有说清「先设未分配利润科目」')
  assert.ok(/:disabled="!store\.canWrite \|\| retainedMissing"/.test(yearTile),
    '缺科目时按钮没禁用 —— 点下去只会撞一句后端报错')
})

t('「没问过」不算「没有科目」', () => {
  assert.ok(/retainedMissing = computed\(\(\) => retainedAsked\.value && !!retained\.value && !retained\.value\.subject\)/.test(PANEL),
    '缺科目的判定没有区分「还没问过」与「问过了没有」')
})

t('后端状态接口分开返回「设的是什么」与「实际用的是什么」', () => {
  assert.ok(/setting_code/.test(PANEL), '页面没有读 setting_code')
  assert.ok(/stale/.test(PANEL), '页面没有读 stale（设定值失效的提示）')
})

t('失败时把下拉拨回原值（不留一个没存上的选择）', () => {
  const fn = PANEL.slice(PANEL.indexOf('async function onRetainedChange'))
    .slice(0, PANEL.indexOf('\n}', PANEL.indexOf('async function onRetainedChange')))
  assert.ok(/retainedFormCode\.value = before \|\| AUTO/.test(fn),
    '设定失败后没有把下拉拨回原值 —— 用户会以为已经改了')
  assert.ok(/ElMessage\.error/.test(fn), '设定失败没有提示')
})

t('api 层有一对「读 / 设」的接口，路径与后端一致', () => {
  assert.ok(/getRetainedSubject = \(id, year\) =>\s*http\.get\(`\/books\/\$\{id\}\/years\/\$\{year\}\/retained-subject`\)/.test(API),
    '没有 getRetainedSubject，或路径与后端对不上')
  assert.ok(/setRetainedSubject = \(id, year, code\) =>\s*http\.put\(`\/books\/\$\{id\}\/years\/\$\{year\}\/retained-subject`, \{ code: code \|\| null \}\)/.test(API),
    '没有 setRetainedSubject，或路径与后端对不上')
})

t('年度结转卡片里的下拉要拉这一年的末级科目（凭证只记末级）', () => {
  assert.ok(/subjectApi\.subjectFlat\(store\.bookId, \{ year \}\)/.test(PANEL),
    '下拉没有拉这一年的科目表')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
