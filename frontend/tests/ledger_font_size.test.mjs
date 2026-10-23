/**
 * 总账 / 明细账的字号下限 12pt（src/views/print/PrintTemplate.vue +
 * src/components/print/TemplateInspector.vue，**线上那份源码**）。
 *
 *  10pt 的正文在 A4 上看不清，而总账与明细账是要拿给外部看的正式账簿
 *  （用户 2026-10-23）。后端已经把这条线钉死了（app.printing.template
 *  的 LEDGER_MIN_FONT_SIZE，校验 / 打印 / 导出三处都执行）。
 *
 *  这里盯的是**设计器**：它自己也必须知道这条线。填 9pt、存下去、
 *  加载时又被后端抬成 12pt —— 用户只会以为界面在骗人，而「界面上
 *  能填、保存后变样」比「压根不让你填」更让人不信任。
 *
 *  **两边必须一样**：前端的下限与后端的下限是同一条线，各写各的
 *  就会有一处漏 —— 漏在前端是能填小值，漏在后端是印出小字。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const PAGE = fs.readFileSync(
  new URL('../src/views/print/PrintTemplate.vue', import.meta.url), 'utf8')
const INSPECTOR = fs.readFileSync(
  new URL('../src/components/print/TemplateInspector.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 总账 / 明细账的下限是 12pt ==')

t('范围是这两个：总账与明细账', () => {
  assert.match(PAGE, /scope\.value === 'LEDGER' \|\| scope\.value === 'DETAIL_LEDGER'/,
    '设计器没有把总账 / 明细账认出来')
})

t('这两个范围给 12，别的一律 6', () => {
  // 一行里同时写着这两个数：抬到 12 的条件与抬不动的范围
  assert.match(PAGE, /\? 12 : 6/, '设计器的字号下限不是「账簿 12、其余 6」')
})

t('检查器用父组件给的下限，不用写死的 6', () => {
  assert.match(INSPECTOR, /v-model="tpl\.font\.size" :min="minFontSize"/,
    '正文字号输入框的下限还是写死的 —— 填进去的值存下来会被后端抬走')
})

t('标题字号的下限也不低于正文', () => {
  assert.match(INSPECTOR, /v-model="tpl\.title\.size" :min="minTitleSize"/,
    '标题字号没有跟着正文的下限走')
  assert.match(INSPECTOR, /Math\.max\(8, props\.minFontSize\)/,
    '标题字号的下限没有取「正文下限与通用下限的较大者」')
})

t('下限是父组件传进来的，不是组件自己定的', () => {
  assert.match(INSPECTOR, /minFontSize:\s*\{ type: Number, default: 6 \}/,
    '检查器没有声明 minFontSize 这个 prop')
  assert.match(PAGE, /:min-font-size="minFontSize"/,
    '页面没有把下限传给检查器')
})

console.log('')
console.log('通过 ' + pass + ' / 失败 ' + fail)
if (fail) process.exit(1)
