/**
 * 凭证录入的键盘锚点只做源码结构验收。
 *
 * keepActiveRowAnchored() 依赖真实 DOM 的滚动盒、行高和 getBoundingClientRect，
 * 不适合像金额/余额那样从源码抠成纯函数；这里钉住模板分层和两个键盘出口，
 * 真正的 y 坐标与滚动条由 tools/uitest/ui_voucher.mjs 验收。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')
const TEMPLATE = SRC.slice(0, SRC.indexOf('<script setup>'))

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

function matchingDivClose(source, openAt) {
  const token = /<\/?div\b[^>]*>/g
  token.lastIndex = openAt
  let depth = 0
  let seenOpen = false
  for (const m of source.matchAll(token)) {
    const isClose = m[0].startsWith('</')
    if (!isClose) {
      depth++
      seenOpen = true
    } else if (seenOpen) {
      depth--
      if (depth === 0) return m.index
    }
  }
  return -1
}

const scrollOpen = TEMPLATE.indexOf('<div class="entry-scroll"')
const scrollClose = matchingDivClose(TEMPLATE, scrollOpen)
const head = TEMPLATE.indexOf('<div class="vg-row vg-head">')
const total = TEMPLATE.indexOf('<div class="vg-row vg-total">')

console.log('== 凭证分录区：固定表头/合计 + 独立滚动 ==')

t('存在唯一的 entry-scroll 滚动容器', () => {
  assert.ok(scrollOpen >= 0, '模板缺少 .entry-scroll')
  assert.equal((TEMPLATE.match(/class="entry-scroll"/g) || []).length, 1)
  assert.match(SRC, /\.entry-scroll\s*\{[\s\S]*?overflow-y:\s*auto/)
})

t('表头在 entry-scroll 之前，已经移出滚动区', () => {
  assert.ok(head >= 0 && head < scrollOpen, '表头仍在 entry-scroll 内')
})

t('合计在 entry-scroll 的闭合标签之后，已经移出滚动区', () => {
  assert.ok(scrollClose >= 0, '找不到 entry-scroll 的闭合标签')
  assert.ok(total > scrollClose, '合计仍在 entry-scroll 内')
})

t('行、表头、合计共用同一份列宽变量', () => {
  assert.match(SRC, /--vg-cols:\s*56px/)
  assert.match(SRC, /\.vg-head\s*\{[\s\S]*?grid-template-rows:/)
  assert.match(SRC, /\.vg-total\s*\{[\s\S]*?width:\s*var\(--vg-grid-width\)/)
  assert.match(SRC, /\.vg-row\s*\{[\s\S]*?grid-template-columns:\s*var\(--vg-cols\)/)
})

t('锚点常量为第 4 行下方的位置', () => {
  assert.match(SRC, /const ANCHOR_ROW\s*=\s*4\b/)
  assert.match(SRC, /const anchorTop\s*=\s*ANCHOR_ROW\s*\*\s*rowHeight\(\)/)
})

t('keepActiveRowAnchored 使用瞬时 scrollTop，且会跳过 1px 内的小偏差', () => {
  const start = SRC.indexOf('function keepActiveRowAnchored()')
  const end = SRC.indexOf('\n}', start)
  const fn = SRC.slice(start, end)
  assert.match(fn, /box\.scrollTop\s*=\s*to/)
  assert.match(fn, /Math\.abs\(box\.scrollTop\s*-\s*to\)\s*<\s*1/)
  assert.doesNotMatch(fn, /scrollTo\(\{[^}]*behavior:\s*['"]smooth['"]/) 
})

t('moveNext 的已有下一行出口调用锚点定位', () => {
  const start = SRC.indexOf('function moveNext(')
  const end = SRC.indexOf('\n}', start)
  const fn = SRC.slice(start, end)
  assert.match(fn, /focusSum\(i\s*\+\s*1\)/)
  assert.match(fn, /nextTick\(keepActiveRowAnchored\)/)
})

t('addRow 的新增行出口调用锚点定位', () => {
  const start = SRC.indexOf('function addRow(')
  const end = SRC.indexOf('\n}', start)
  const fn = SRC.slice(start, end)
  assert.match(fn, /focusSum\(form\.entries\.length\s*-\s*1\)/)
  assert.match(fn, /nextTick\(keepActiveRowAnchored\)/)
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
