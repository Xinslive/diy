/**
 * 明细账页与打印模板设计器的**用友形态**。
 *
 *  总账已经改成用友式总分类账，明细账随之跟上：**同一套版式**，
 *  差别只在「日 / 凭证号数 / 凭证摘要」这三列填的是真值。
 *
 *  这些是**源码级**断言：盯住页面上那几处最容易悄悄退回去的地方。
 *  退回去的样子很具体 —— 打印时又只打屏幕上的那一个科目、页眉页脚又退回
 *  通用那一套、打印日期又变成自动取当天。哪一条都不报错，只是纸不对了。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const DETAIL = fs.readFileSync(
  new URL('../src/views/ledger/DetailLedger.vue', import.meta.url), 'utf8')
const INSPECTOR = fs.readFileSync(
  new URL('../src/components/print/TemplateInspector.vue', import.meta.url), 'utf8')
const DIALOG = fs.readFileSync(
  new URL('../src/components/PrintDialog.vue', import.meta.url), 'utf8')
const DESIGNER = fs.readFileSync(
  new URL('../src/views/print/PrintTemplate.vue', import.meta.url), 'utf8')
const RANGE = fs.readFileSync(
  new URL('../src/utils/ledgerPrint.js', import.meta.url), 'utf8')
/** 只看模板部分：注释里提到「打印范围」不算，那个下拉得真的不在页面上。 */
const DETAIL_TPL = DETAIL.split('<script setup>')[0]

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 打印范围：整册 or 仅当前科目 ==')
//
// 明细账与总账是**同一册结构**：一个一级科目一份（用户 2026-10-22 批注 3），
// 连续打成一册。范围下拉与「不打印无发生额科目」都住在**打印预览弹窗**里
// （批注 1、2），工具栏上那份已经删掉 —— 同一个设置只留一处。

t('工具栏上不再有打印范围下拉', () => {
  assert.ok(!/打印范围/.test(DETAIL_TPL), '工具栏上还有「打印范围」')
  assert.ok(!/ledger-scope-select/.test(DETAIL), '工具栏上还有那个下拉')
})
t('打印范围搬进打印预览弹窗', () => {
  assert.match(DETAIL, /:print-ranges="LEDGER_PRINT_RANGES"/)
  assert.match(DETAIL, /v-model:range="printRange"/)
  assert.match(DETAIL, /v-model:skip-empty="printSkipEmpty"/)
})
t('成册单位改成一级科目', () => {
  // 「本册（全部末级科目）」的说法已经没有了 —— 册页单位是一级科目。
  assert.ok(!/本册（全部末级科目）/.test(DETAIL), '还在说「末级科目」')
  assert.ok(!/本册（全部末级科目）/.test(RANGE), '共享口径里还在说「末级科目」')
  assert.match(RANGE, /本册（全部一级科目）/)
  assert.match(RANGE, /仅当前科目/)
})
t('默认是「仅当前科目」', () => {
  // 明细账是逐笔的账，整册是几百页纸；用户走到这一页要的就是屏幕上那一个。
  assert.match(DETAIL, /ref\(PRINT_RANGE_CURRENT\)/)
})
t('导出与打印用同一个范围口径', () => {
  // 两处各算一遍口径，迟早有一处漏改 —— 于是纸上是一册、文件里是另一个科目。
  assert.match(DETAIL, /ledgerPrintParams\(/)
  assert.match(DETAIL, /\{ \.\.\.printParams\.value \}/, '导出没有复用打印参数')
})
t('起止科目范围只跟着「仅当前科目」走', () => {
  // 整册是全账套的一级科目，起止科目是父科目下的子科目范围 —— 两者不是一回事。
  assert.match(DETAIL, /from_subject_id: isComprehensive\.value/)
  assert.match(DETAIL, /to_subject_id: isComprehensive\.value/)
})

console.log('')
console.log('== 累计行的标签由后端给，屏幕与纸上同一份 ==')
//
// 以前前端硬写「累　　计」，于是整年那一行纸上写「本年累计」、屏幕写「累计」。

t('前端不再硬写累计标签', () => {
  assert.ok(!/YTD_TOTAL\s*\?\s*'累/.test(DETAIL), '前端还在硬写累计标签')
})
t('标签直接取行里的摘要', () => {
  assert.match(DETAIL, /summary:\s*row\.summary\s*\|\|\s*''/)
})

console.log('')
console.log('== 模板设计器左栏：账簿版式 ==')
//
// 用户 2026-10-22 批注 2：页眉页脚那一组（本币名称、制表、页号…）
// 得能在左侧栏里设置。

t('左栏有一节「账簿版式」', () => {
  assert.match(INSPECTOR, /账簿版式/, '左栏没有「账簿版式」这一节')
})
t('账簿版式那一节在模板里有 ledger_book 时才出现', () => {
  assert.match(INSPECTOR, /hasLedgerBook/, '没有按 ledger_book 判断显隐')
})
t('页眉页脚各项都能在左栏里改', () => {
  for (const key of ['currency_name', 'book_label', 'subject_caption',
                     'page_label', 'footer_left', 'footer_center', 'footer_right']) {
    assert.match(INSPECTOR, new RegExp(`lb\\.${key}`), `左栏改不了 ${key}`)
  }
})
t('每页行数与行高能改', () => {
  assert.match(INSPECTOR, /lb\.rows_per_page/)
  assert.match(INSPECTOR, /lb\.row_height_mm/)
})
t('「不打印无发生额科目」不在左栏，在打印弹窗里', () => {
  // 它是**这一次打印**的开关（打印预览弹窗左栏，默认打开）。
  // 模板里再摆一份，同一个设置就有两个地方能改 —— 改了一处、另一处不动。
  // （认的是**绑定** `lb.skip_empty`：注释里提一句这个字段名不算开关。）
  assert.ok(!/lb\.skip_empty/.test(INSPECTOR), '左栏还有 skip_empty 开关')
  assert.match(DIALOG, /不打印无发生额科目/)
  assert.match(DIALOG, /el-switch v-model="skipEmptyValue"/)
})
t('这一节只归左栏管 —— 别处不重复摆同一批控件', () => {
  // 同一个设置出现在两个地方，用户改了一处、另一处不动，纸上出来是旧的。
  const inDialog = /currency_name|book_label|footer_center/.test(DIALOG)
  assert.ok(!inDialog, '打印弹窗里又摆了一份账簿版式的控件')
})

console.log('')
console.log('== 打印预览弹窗：打印日期可指定 ==')
//
// 用户 2026-10-22 批注 3：允许在打印预览页面设置打印日期，不要弄成自动获取。

t('弹窗里有打印日期输入框', () => {
  assert.match(DIALOG, /打印日期/, '打印预览弹窗里没有打印日期')
})
t('打印日期只给账簿范围（总账 / 明细账）', () => {
  assert.match(DIALOG, /isLedgerBook/)
  assert.match(DIALOG, /scope\s*===\s*'LEDGER'\s*\|\|\s*props\.scope\s*===\s*'DETAIL_LEDGER'/)
})
t('填了就作为 print_date 参数发出去', () => {
  assert.match(DIALOG, /p\.print_date\s*=\s*printDate\.value/)
})
t('留空就不发这个参数（后端回落到当天）', () => {
  assert.match(DIALOG, /if\s*\(isLedgerBook\.value\s*&&\s*printDate\.value\)/)
})

console.log('')
console.log('== 打印预览弹窗：方向不再配说明文字 ==')
//
// 用户 2026-10-22 批注：左栏底下那句讲「方向 / 纸张」的解释去掉。方向与
// 纸型在上面的控件里一眼看得见，再补一句只是把左栏压得满满当当。

t('方向说明已经去掉', () => {
  // 注释里可以写「当初那句是什么」—— 判的是**页面上还看不看得到**，
  // 所以先把 HTML 注释去掉再找那句话。
  const shown = DIALOG.replace(/<!--[\s\S]*?-->/g, '')
  assert.ok(!/方向只对本次打印生效/.test(shown), '打印弹窗里还有方向说明')
  assert.ok(!/note-tip/.test(DIALOG), 'note-tip 的标记/样式还留着')
})
t('凭证页要的那句说明还留着', () => {
  assert.match(DIALOG, /一页放几张凭证由所选模板决定/)
  assert.match(DIALOG, /v-if="isVoucher" class="note"/)
})

console.log('')
console.log('== 打印弹窗里的控件顺序 ==')
//
// 用户 2026-10-22 批注 2：打印范围挪到「打印日期」下面，再加一个默认打开的
// 「不打印无发生额科目」开关。顺序错了用户还是得找。

t('打印范围排在打印日期下面', () => {
  const iDate = DIALOG.indexOf('label="打印日期"')
  const iRange = DIALOG.indexOf('label="打印范围"')
  const iSkip = DIALOG.indexOf('label="不打印无发生额科目"')
  assert.ok(iDate > 0 && iRange > 0 && iSkip > 0, '三个控件少了一个')
  assert.ok(iDate < iRange, '打印范围要排在打印日期下面')
  assert.ok(iRange < iSkip, '开关要排在打印范围下面')
})
t('开关默认打开', () => {
  assert.match(RANGE, /SKIP_EMPTY_DEFAULT = true/)
  assert.match(DETAIL, /ref\(SKIP_EMPTY_DEFAULT\)/)
})

console.log('')
console.log('== 模板设计器：明细账也能整册预览 ==')

t('预览科目下拉里有「全部科目（成册）」', () => {
  assert.match(DESIGNER, /全部科目（成册）/, '明细账预览不能选整册')
})
t('打印目标里整册不传 subject_id', () => {
  assert.match(DESIGNER, /subject_id:\s*previewSubjectId\.value\s*\|\|\s*undefined/)
})
t('载入模板时保证有 ledger_book 那一块', () => {
  // 老模板可能是别处写进去的，缺了这一块，左栏会整片空着。
  assert.match(DESIGNER, /ensureLedgerBook/)
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
if (fail) process.exit(1)
