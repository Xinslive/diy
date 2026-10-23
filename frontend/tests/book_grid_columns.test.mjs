/**
 * 账套磁贴的列数（src/views/book/BookManager.vue + src/styles/main.css，**线上那份源码**）。
 *
 *  这里钉的是「窗口最窄时，一行必须放得下三个账套」。
 *  以前 minmax 的下限写成 330px，而外壳最窄 1280px 时网格实得 1012px ——
 *  三列要 3×330 + 2×12 = 1014px，**差 2px 挤不下**，auto-fit 就悄悄退回两列。
 *  差这一点像素，页面上却从「三个账套一屏」变成「两个 + 第三个掉到第二行」。
 *
 *  算式全部从源码里取（外壳宽度、侧栏宽度、页边距、缝隙），不写死数字：
 *  以后谁改了外壳宽度或页边距，这条断言会跟着重新算，
 *  而不是抱着一组过期常量假装通过。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const BOOKS = fs.readFileSync(new URL('../src/views/book/BookManager.vue', import.meta.url), 'utf8')
const MAIN = fs.readFileSync(new URL('../src/styles/main.css', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

/* 取一段规则块里的第一个数字（px） */
const px = (text, re) => {
  const m = text.match(re)
  assert.ok(m, '源码里没找到 ' + re)
  return parseFloat(m[1])
}
const block = (text, selector) => {
  const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}')
  const m = text.match(re)
  assert.ok(m, '源码里没找到 ' + selector + ' 这段样式')
  return m[1]
}

// 外壳最窄时的宽度：.shell { width: max(1280px, 74vw) } 里那个下限
const shellMin = px(MAIN, /width:\s*max\(\s*(\d+(?:\.\d+)?)px/)
// 侧栏占掉的宽度：取所有 --sb-w 里最大的那条（收起态 60px 更窄，
// 打印样式里那条是 0px !important，都不是最宽的形态）
const sidebar = Math.max(...[...MAIN.matchAll(/--sb-w:\s*(\d+(?:\.\d+)?)px/g)].map(m => parseFloat(m[1])))
// 账套页自己的页边距
const pagePad = px(block(BOOKS, '.book-page'), /padding:\s*(\d+(?:\.\d+)?)px/)
// 网格列宽下限与列间缝隙
const gridBlock = block(BOOKS, '.book-grid')
const colMin = px(gridBlock, /minmax\(min\(100%,\s*(\d+(?:\.\d+)?)px\)/)
const gap = px(gridBlock, /gap:\s*(\d+(?:\.\d+)?)px/)

// 外壳最窄时网格实得的宽度
const gridWidth = shellMin - sidebar - pagePad * 2
// 能排下的列数
const columns = Math.floor((gridWidth + gap) / (colMin + gap))

console.log('== 窗口最窄时也要一行放得下三个账套 ==')

t('算式取自源码：外壳 ' + shellMin + ' - 侧栏 ' + sidebar + ' - 页边距 ' + pagePad * 2 + ' = ' + gridWidth + 'px', () => {
  assert.ok(gridWidth > 0, '外壳/侧栏/页边距算出了非正的宽度，源码是不是改了结构')
})

t('最窄宽度下能排下 3 列（当前 ' + columns + ' 列）', () => {
  assert.ok(columns >= 3,
    '最窄宽度只排得下 ' + columns + ' 列：' + colMin + 'px 的下限 + ' + gap +
    'px 的缝，三列需要 ' + (colMin * 3 + gap * 2) + 'px，实得 ' + gridWidth + 'px')
})

console.log('')
console.log('== 但列数不能写死 ==')

t('网格仍按 auto-fit + minmax 自适应（窗口再窄时自己收成一列）', () => {
  assert.match(gridBlock, /repeat\(auto-fit,\s*minmax\(/,
    '网格不再用 auto-fit/minmax，窗口变窄时磁贴会被硬挤成多列')
})

console.log('')
console.log('== 卡片变窄，里面的东西不许被挤变形 ==')

t('年度下拉框不许被 flex 压瘦', () => {
  assert.match(BOOKS, /\.book-year-select\s*\{[^}]*flex:\s*none/, '下拉框会跟着卡片一起被压扁')
})

t('「已结账 12 / 12 期」不换行（窄卡片里折成两行就难读了）', () => {
  assert.match(BOOKS, /\.book-fact strong\s*\{[^}]*white-space:\s*nowrap/, '期数会被折行')
})

console.log('')
console.log('通过 ' + pass + ' / 失败 ' + fail)
if (fail) process.exit(1)
