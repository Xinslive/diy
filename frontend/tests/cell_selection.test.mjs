/**
 * 表格矩形选区（src/utils/cellRange.js，**线上那份源码**）。
 *
 *  这个特性最怕的不是「框不方正」，是**框大了**：原生选区从 C3 拖到 C9
 *  会把中间整行连同左右各列一起框进去，用户看到的就是这个。所以这里重点
 *  盯三件事 —— 拖动方向不影响结果、命中判定是闭合矩形、以及导出文本不把
 *  换行和多余空白带进剪贴板。
 *
 *  DOM 那半（定位、绘制、剪贴板）在 composables 里，只能源码级断言；
 *  形状与文本这半是纯函数，逐条跑。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const {
  inRect, normRect, rectSize, rectToTsv,
} = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/cellRange.js'))

const APP = fs.readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
const HOOK = fs.readFileSync(new URL('../src/composables/useCellSelection.js', import.meta.url), 'utf8')
const CSS = fs.readFileSync(new URL('../src/styles/main.css', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== normRect：两个角归一，拖动方向不影响结果 ==')

t('左上 → 右下', () => {
  assert.deepEqual(normRect({ r: 2, c: 2 }, { r: 8, c: 2 }), { r1: 2, c1: 2, r2: 8, c2: 2 })
})
t('右下 → 左上（倒着拖，结果必须一样）', () => {
  assert.deepEqual(normRect({ r: 8, c: 2 }, { r: 2, c: 2 }), { r1: 2, c1: 2, r2: 8, c2: 2 })
})
t('同一格 = 单格选区', () => {
  assert.deepEqual(normRect({ r: 3, c: 3 }, { r: 3, c: 3 }), { r1: 3, c1: 3, r2: 3, c2: 3 })
})

console.log('== inRect：只认矩形里那一列，不多吃旁边 ==')

const col = normRect({ r: 2, c: 2 }, { r: 8, c: 2 })
t('C3→C9 这一列七格全部命中', () => {
  assert.deepEqual([2, 3, 4, 5, 6, 7, 8].map(r => inRect(r, 2, col)), Array(7).fill(true))
})
t('同一行的左右邻列都不命中（用户报的就是这个）', () => {
  assert.equal(inRect(5, 1, col), false)
  assert.equal(inRect(5, 3, col), false)
})
t('矩形外的行不命中', () => {
  assert.equal(inRect(1, 2, col), false)
  assert.equal(inRect(9, 2, col), false)
})
t('矩形选区四边闭合：角上的格子也在内', () => {
  const box = normRect({ r: 1, c: 1 }, { r: 3, c: 4 })
  assert.deepEqual([[1, 1], [3, 4], [1, 4], [3, 1]].map(([r, c]) => inRect(r, c, box)), Array(4).fill(true))
  assert.equal(inRect(4, 2, box), false)
  assert.equal(inRect(2, 5, box), false)
})

console.log('== rectSize：用来判断「有没有真的拖过」 ==')

t('单格是 1×1，拖过一格就是 2×1', () => {
  assert.deepEqual(rectSize(normRect({ r: 0, c: 0 }, { r: 0, c: 0 })), { rows: 1, cols: 1 })
  assert.deepEqual(rectSize(normRect({ r: 0, c: 0 }, { r: 1, c: 0 })), { rows: 2, cols: 1 })
})
t('横拖与竖拖分别算行和列', () => {
  assert.deepEqual(rectSize(normRect({ r: 4, c: 1 }, { r: 4, c: 5 })), { rows: 1, cols: 5 })
  assert.deepEqual(rectSize(normRect({ r: 4, c: 1 }, { r: 9, c: 1 })), { rows: 6, cols: 1 })
})

console.log('== rectToTsv：粘进 Excel 要成块 ==')

const GRID = [
  ['资产', '行次', '期末余额'],
  ['货币资金', '1', '1,712,379.87'],
  ['短期投资', '2', '0.00'],
  ['应收账款', '3', '320,492.35'],
]
const at = (r, c) => GRID[r]?.[c]

t('取一列七格 → 每行一个值，换行分隔', () => {
  assert.equal(rectToTsv(at, normRect({ r: 1, c: 2 }, { r: 3, c: 2 })),
    '1,712,379.87\n0.00\n320,492.35')
})
t('取一个矩形块 → 制表符分列', () => {
  assert.equal(rectToTsv(at, normRect({ r: 1, c: 0 }, { r: 2, c: 1 })),
    '货币资金\t1\n短期投资\t2')
})
t('空格与换行压成单格一个值（否则粘进 Excel 会错行）', () => {
  assert.equal(rectToTsv(() => '  备注\n第二行 ', normRect({ r: 0, c: 0 }, { r: 0, c: 0 })), '备注 第二行')
})
t('越界的列补空串，不留 undefined', () => {
  // 第 4 列本来就没有内容，选到它必须是空格子而不是字面量 undefined
  assert.equal(rectToTsv(at, normRect({ r: 2, c: 2 }, { r: 3, c: 3 })), '0.00\t\n320,492.35\t')
})
t('整行都在表外 → 空行，行数不减（粘过去仍是原样的行）', () => {
  assert.equal(rectToTsv(at, normRect({ r: 3, c: 0 }, { r: 4, c: 0 })), '应收账款\n')
})

console.log('== 接入点：全局装一次 + 原生选区确实关掉了 ==')

t('App.vue 装一次，卸载时拆掉', () => {
  assert.match(APP, /installCellSelection\(\)/)
  assert.match(APP, /onBeforeUnmount\(removeSelection\)/)
})
t('监听挂在 document 上（新加的表格不必改代码就生效）', () => {
  assert.match(HOOK, /document\.addEventListener\('mousedown'/)
  assert.match(HOOK, /document\.addEventListener\('mouseup'/)
  assert.match(HOOK, /document\.addEventListener\('mousemove'/)
})
t('重复装不会叠出两层监听（否则一次拖动弹两次提示）', () => {
  assert.match(HOOK, /addEventListener\('mousedown', onMouseDown, true\)[\s\S]*?removeEventListener\('mousedown', onMouseDown, true\)/)
})
t('输入框/按钮/下拉里的拖选不接管', () => {
  assert.match(HOOK, /const SKIP = 'input,textarea,select,button,a,\[contenteditable="true"\][\s\S]*?'/)
  assert.match(HOOK, /cell\.closest\(SKIP\)/)
})
t('单击不抢剪贴板（表格里单击多半是打开详情）', () => {
  assert.match(HOOK, /size\.rows \* size\.cols < 2/)
})
t('Ctrl+C 走 copy 事件，局域网 http 部署也有兜底', () => {
  assert.match(HOOK, /addEventListener\('copy', onCopy\)/)
  assert.match(HOOK, /clipboardData\?\.setData\('text\/plain'/)
  assert.match(HOOK, /execCommand\('copy'\)/)
})
t('表格的原生选区关掉，选中态样式在', () => {
  assert.match(CSS, /table \{ user-select: none/)
  assert.match(CSS, /\.cwq-cell-picked \{/)
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
