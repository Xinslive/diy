/**
 * 报表表头的版式（src/views/report/ReportView.vue）。
 *
 * 用户在利润表上批注「为什么文字没有上下居中」：表头去掉口径角标之后，
 * 列名却吊在格子上沿，下面空出一大片。
 *
 * 根因是 `thead th` 固定 46px 高，而 `th.c-head` 上的
 * `vertical-align: top` 是给**设计模式**准备的 —— 那边表头要堆
 * ⊕✕ 按钮、列名输入框、列字母、口径下拉，只能从上往下排。查看模式
 * 表头里只有一行 16px 的列名，top 就把它顶到上边去了。
 *
 * 这里守住两件事：
 *   · 查看模式（非 .is-designing）表头必须 middle；
 *   · 设计模式必须仍然是 top —— 改成 middle 不会让设计模式「看起来
 *     更整齐」，而是把那一摞控件挤到格子中间，下边的列字母和口径下拉
 *     会跟基线错开。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/report/ReportView.vue', import.meta.url), 'utf8')

/** 样式块里某个选择器的声明体 */
const css = (selector) => {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = SRC.match(new RegExp(`([^}]*${esc}[^}]*)\\{([^}]*)\\}`))
  assert.ok(m, `样式里找不到 ${selector}`)
  return m[2]
}

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 表头：查看模式上下居中，设计模式仍然顶格 ==')

t('查看模式的表头是 middle', () => {
  const body = css('.report-page:not(.is-designing) .report-table th.c-head')
  assert.match(body, /vertical-align:\s*middle/,
    '查看模式表头不是垂直居中 —— 列名会吊在格子上沿')
})

t('设计模式仍是 top（表头要堆按钮、输入框、列字母、口径下拉）', () => {
  const body = css('.report-table th.c-head')
  assert.match(body, /vertical-align:\s*top/,
    '设计模式表头被改成居中，那一摞控件会挤到格子中间')
})

t('选择器限定在「非设计模式」，不是把设计模式一起改掉', () => {
  assert.match(SRC, /\.report-page:not\(\.is-designing\)\s+\.report-table th\.c-head/,
    '居中规则没有限定在查看模式')
})

t('thead 固定了行高，所以必须靠 vertical-align 才能居中', () => {
  // 46px 是给表头留的固定高度；一旦有人把它改小或删掉，居中与否都看不出来，
  // 这条断言提醒：改高度的同时看一眼 vertical-align 还对不对。
  assert.match(SRC, /\.report-table thead th \{[^}]*height:\s*46px/,
    'thead 行高规则变了 —— 表头高度与 vertical-align 要一起看')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
