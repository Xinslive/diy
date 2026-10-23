/**
 * 总账 / 明细账的字号下限 12pt（src/utils/paper.js 的 minFontSize() +
 * src/views/print/PrintTemplate.vue + src/components/print/TemplateInspector.vue）。
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
 *
 *  「总账 / 明细账的下限是 12」这条规则**住在 utils/paper.js 里**，
 *  这里**直接调用它**来断言（线上那份源码，vite 的 ssrLoadModule 加载）。
 * 以前这条测试断言的是「源码里存在字面量 `? 12 : 6`」——
 * 那种断言守不住「12 是不是真的是下限」，只守得住「那行字面量还在不在」；
 * 规则挪个地方写（比如换成 `Math.max(6, 12)`），测试照样绿。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const server = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
})
const U = await server.ssrLoadModule('/src/utils/paper.js')

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

t('**行为**：账簿类页面的字号下限是 12（直接调用线上那份规则）', () => {
  // 总账
  assert.equal(U.minFontSize('LEDGER'), 12, '总账的正文字号下限不是 12')
  // 明细账
  assert.equal(U.minFontSize('DETAIL_LEDGER'), 12, '明细账的正文字号下限不是 12')
})

t('别的范围仍是 6（各自的版式另有约定）', () => {
  for (const scope of ['VOUCHER', 'BALANCE', 'REPORT', '', undefined, null, 'OTHER']) {
    assert.equal(U.minFontSize(scope), 6, `${scope} 的下限不该被抬到 12`)
  }
})

t('设计器把当前范围交给那份规则（范围判断住在规则里，不在页面里）', () => {
  // 规则搬到 utils/paper.js 之后，页面这一侧只负责「把当前 scope 递过去」——
  // 范围认错了还是认对了，全由那一份说了算（两边不再各认一遍）。
  assert.match(PAGE, /minFontSizeOf\(scope\.value\)/,
    '设计器没有把当前范围交给 utils/paper.js 的 minFontSize')
  assert.match(PAGE, /const scope = /, '设计器没有当前范围可用')
})

t('设计器用的是那份规则，不是自己又写了一遍', () => {
  assert.match(PAGE, /minFontSizeOf\(scope\.value\)/,
    '设计器没有调用 utils/paper.js 的 minFontSize —— 规则又分家了')
  assert.match(PAGE, /from '@\/utils\/paper'/, '设计器没有引入 utils/paper')
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
// vite 的 dev server 攥着端口与文件监听，不关掉的话进程永远退不出去
await server.close()
process.exit(fail ? 1 : 0)
