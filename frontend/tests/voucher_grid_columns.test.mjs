/**
 * 凭证分录格的列宽预算（src/views/voucher/VoucherEdit.vue + src/styles/main.css）。
 *
 * 这里钉的是「**最窄的一档也塞得下**：五列的下限加上列间的缝，不许超过
 * 容器实得的宽度」。
 *
 * 事故（2026-10-31）：生产上贷方金额格的右框线整条消失。根因是列下限
 * 合计 876px + 4px 缝 = 880px，而容器实得 894px − 经典纵向滚动条 22px = 872px，
 * 差 8px —— grid 把多出来的部分放到**格子外面**，而贷方金额格是不透明的，
 * 它那块白底正好盖住 `.vgrid` 那 1px 的右边框线。macOS 是覆盖式滚动条
 * （不占宽），所以只在 Windows / Linux 上犯。
 *
 * 跟账套磁贴那处是同一个坑（`book_grid_columns.test.mjs` 钉着）：
 * minmax 的下限是硬底线，超出容器宽度就会静默换一种排法。
 *
 * 算式全部从源码里取（外壳宽度、侧栏宽度、页边距、卡片内边距、滚动区留白、
 * 表格边框、滚动条占位），不写死数字：以后谁改了外壳宽度或留白，这条断言
 * 会跟着重新算，而不是抱着一组过期常量假装通过。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VOUCHER = fs.readFileSync(new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')
const MAIN = fs.readFileSync(new URL('../src/styles/main.css', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

const px = (text, re, what) => {
  const m = text.match(re)
  assert.ok(m, '源码里没找到 ' + (what || re))
  return parseFloat(m[1])
}
const str = (text, re, what) => {
  const m = text.match(re)
  assert.ok(m, '源码里没找到 ' + (what || re))
  return m[1]
}
const block = (text, selector) => {
  const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}')
  const m = text.match(re)
  assert.ok(m, '源码里没找到 ' + selector + ' 这段样式')
  return m[1]
}

/* ── 容器实得宽度：从外往里一层层扣 ────────────────────────────── */

// 外壳最窄时的宽度：.shell { width: max(1280px, 74vw) } 里那个下限
const shellMin = px(MAIN, /width:\s*max\(\s*(\d+(?:\.\d+)?)px/, '外壳宽度')
// 侧栏占掉的宽度：取所有 --sb-w 里最大的那条（收起态 60px 更窄，
// 打印样式里那条是 0px !important，都不是最宽的形态）
const sidebar = Math.max(...[...MAIN.matchAll(/--sb-w:\s*(\d+(?:\.\d+)?)px/g)].map(m => parseFloat(m[1])))
// 页面页边距、卡片内边距与边框
const pagePad = px(MAIN, /\.page\s*\{[^}]*padding:\s*(\d+(?:\.\d+)?)px/, '页面页边距')
const cardPad = px(MAIN, /\.card\s*\{[^}]*padding:\s*(\d+(?:\.\d+)?)px/, '卡片内边距')
const cardBorder = px(MAIN, /\.card\s*\{[^}]*border:\s*(\d+(?:\.\d+)?)px/, '卡片边框')
// 分录区：entry-wrap 的边框、滚动区左右留白（给 ⊕/＋ 与 ⊗ 两条带子）、表格自己的边框
const wrapBorder = px(VOUCHER, /\.entry-wrap\s*\{[^}]*border:\s*(\d+(?:\.\d+)?)px/, '分录区边框')
const scrollPad = px(VOUCHER, /\.entry-scroll\s*\{[^}]*padding:\s*0\s+(\d+(?:\.\d+)?)px/, '滚动区右留白')
const scrollPadL = px(VOUCHER, /\.entry-scroll\s*\{[^}]*padding:\s*0\s+\d+(?:\.\d+)?px\s+0\s+(\d+(?:\.\d+)?)px/, '滚动区左留白')
const gridBorder = px(VOUCHER, /\.vgrid\s*\{[^}]*border-right:\s*(\d+(?:\.\d+)?)px/, '表格边框')

/* ── 经典纵向滚动条的占位 ──────────────────────────────────────────
   macOS 的覆盖式滚动条不占宽，Windows / Linux 的经典滚动条占 15~22px
   （还要随显示缩放放大）。取最宽的一档当底线。 */
const SCROLLBAR = 22

// 表格内容盒（行所在的那一格）实得宽度
const rowWidth = shellMin - sidebar - pagePad * 2
  - (cardBorder * 2 + cardPad * 2)
  - (wrapBorder * 2)
  - (scrollPadL + scrollPad)
  - (gridBorder * 2)
  - SCROLLBAR

/* ── 五列的下限 ────────────────────────────────────────────────── */
const cols = str(VOUCHER, /--vg-cols:\s*([^;]+);/, '列宽定义')
const tracks = cols.trim().split(/\s+(?![^(]*\))/).map(s => s.trim())
const gap = px(block(VOUCHER, '.vg-row'), /gap:\s*0\s+(\d+(?:\.\d+)?)px/, '列间缝隙')

// 每列的下限：minmax(a, b) 取 a；固定宽度取它自己；minmax(0, …) 的下限是 0
const floorOf = (track) => {
  const mm = track.match(/^minmax\(\s*(\d+(?:\.\d+)?)px\s*,/)
  if (mm) return parseFloat(mm[1])
  const fixed = track.match(/^(\d+(?:\.\d+)?)px$/)
  if (fixed) return parseFloat(fixed[1])
  const zero = track.match(/^minmax\(\s*0\s*,/)
  if (zero) return 0
  throw new Error('看不懂的列宽写法：' + track)
}
const floors = tracks.map(floorOf)
const minTotal = floors.reduce((a, b) => a + b, 0)
const need = minTotal + gap * (tracks.length - 1)

/* ── 金额列那 210px 不能动 ──────────────────────────────────────── */
const AMOUNT_COL_MIN = 210
const amountFloors = floors.slice(3)

console.log('== 凭证分录格：列下限必须塞得进最窄的容器 ==')
console.log('   列宽定义：' + cols.trim())
console.log('   算式：外壳 ' + shellMin + ' - 侧栏 ' + sidebar + ' - 页边距 ' + (pagePad * 2) +
  ' - 卡片 ' + (cardBorder * 2 + cardPad * 2) + ' - 分录区边框 ' + (wrapBorder * 2) +
  ' - 滚动区留白 ' + (scrollPadL + scrollPad) + ' - 表格边框 ' + (gridBorder * 2) +
  ' - 滚动条 ' + SCROLLBAR + ' = ' + rowWidth + 'px')

t('算式取自源码，行宽算出了正数', () => {
  assert.ok(rowWidth > 0, '外壳/侧栏/页边距算出了非正的宽度，源码是不是改了结构')
})

t('五列下限 ' + minTotal + 'px + ' + gap + 'px 缝 = ' + need + 'px ≤ 行宽 ' + rowWidth + 'px', () => {
  assert.ok(need <= rowWidth,
    '列下限合计 ' + need + 'px 超过最窄时实得的 ' + rowWidth + 'px：' +
    '多出来的 ' + (need - rowWidth) + 'px 会跑到格子外面，' +
    '贷方金额格那块不透明的白底会盖住右边框线（整条框线消失）')
})

console.log('')
console.log('== 金额格那 11 个数位槽的宽度不许被挤 ==')

t('借/贷两列的下限仍是 ' + AMOUNT_COL_MIN + 'px（' + amountFloors.join(' / ') + '）', () => {
  assert.deepEqual(amountFloors, [AMOUNT_COL_MIN, AMOUNT_COL_MIN],
    '金额列的下限被改了：那 11 个数位槽的宽度是记账格式的一部分，不能让位')
})

t('让位的是文本列（下限 0），不是金额列', () => {
  assert.deepEqual(floors.slice(1, 3), [0, 0],
    '摘要/科目两列的下限不是 0：空间不够时列会溢出格子，' +
    '溢出的一格会把表格右边的框线盖掉')
})

t('序号列仍是固定 56px', () => {
  assert.equal(floors[0], 56, '序号列的宽度被改了')
})

console.log('')
console.log('通过 ' + pass + ' / 失败 ' + fail)
if (fail) process.exit(1)
