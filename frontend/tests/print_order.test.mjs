/**
 * 打印顺序：一页两张的错位配对 + 整叠倒着印（用户 2026-10-30 批注）。
 *
 * 盯的是三件事：
 *   1. **两个开关**：「打印顺序优化」管怎么配对，「倒序打印」管出纸的先后 ——
 *      这是两件不同的事，合成一个开关说不清那一项在两种模板上分别是什么意思；
 *   2. 「打印顺序优化」只在**一页两张**时出现（一页一张没有配对这回事），
 *      「倒序打印」两种模板都有；
 *   3. 默认值：错位配对默认开、倒序打印默认关；送出去的两个口径与后端
 *      一一对应 —— 前端写一个后端不认的值，开关看着打开了、纸上的
 *      顺序却一点没变。
 *
 * 照应的是后端 `app/printing/template.py::optimize_print_order` 与
 * `app/api/printing.py::_print_order_arg`。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const U = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/printOrder.js'))

const read = (rel) => fs.readFileSync(new URL(rel, import.meta.url), 'utf8')
const DIALOG = read('../src/components/PrintDialog.vue')
const BACKEND = read('../../backend/app/printing/template.py')
const BACKEND_API = read('../../backend/app/api/printing.py')

/** 去掉注释再断言「代码里没有某个写法」—— 解释这个坑的注释本身会命中。 */
const stripComments = (src) => src
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/^\s*\/\/.*$/gm, '')

const DIALOG_CODE = stripComments(DIALOG)
const BACKEND_CODE = stripComments(BACKEND)
const BACKEND_API_CODE = stripComments(BACKEND_API)

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 两个开关：配对与先后各管各的 ==')

t('一页两张时错位配对走 optimized', () => {
  assert.equal(U.printOrderParam(2, { interleave: true }), 'optimized')
})

t('关掉错位配对回到连续配对', () => {
  assert.equal(U.printOrderParam(2, { interleave: false }), 'normal')
})

t('一页一张没有配对这回事', () => {
  // 每张纸只有一张凭证，配不配对都是原样。
  assert.equal(U.printOrderParam(1, { interleave: true }), 'normal')
})

t('两个开关的默认值：错位配对开、倒序打印关', () => {
  assert.equal(U.PRINT_ORDER_DEFAULTS.interleave, true)
  assert.equal(U.PRINT_ORDER_DEFAULTS.reverse, false)
})

console.log('== 口径取值与后端一致 ==')

t('前端只有两种配对口径', () => {
  assert.deepEqual(U.PRINT_ORDERS, ['normal', 'optimized'])
})

t('后端认得这两种口径', () => {
  for (const name of U.PRINT_ORDERS) {
    assert.ok(BACKEND_CODE.includes(`"${name}"`), `后端没有 "${name}" 这个口径`)
  }
  assert.ok(/PRINT_ORDERS\s*=\s*\(PRINT_ORDER_NORMAL, PRINT_ORDER_OPTIMIZED\)/.test(BACKEND_CODE),
    '后端的 PRINT_ORDERS 与前端不是同一组')
})

t('后端的倒序是整页翻（不是把整条序列反过来）', () => {
  // 整页翻：同一页里上下两格的先后不变（跟着翻就把 3+6 印成了 6+3）。
  assert.ok(/sheets\s*=\s*list\(reversed\(sheets\)\)/.test(BACKEND_API_CODE),
    '后端不是按整页翻的')
  assert.ok(!/pieces\s*=\s*list\(reversed\(pieces\)\)/.test(BACKEND_API_CODE),
    '后端把整条序列翻过来了（同一页里两格会跟着翻）')
})

console.log('== 弹窗把两个开关接到请求上 ==')

t('弹窗里有这两个开关', () => {
  assert.ok(/v-model="interleaveOn"/.test(DIALOG_CODE), '弹窗里没有「打印顺序优化」的开关')
  assert.ok(/v-model="reverseOn"/.test(DIALOG_CODE), '弹窗里没有「倒序打印」的开关')
})

t('「打印顺序优化」只在一页两张时出现', () => {
  assert.ok(/v-if="isVoucher && isTwoUp"[^>]*label="打印顺序优化"/.test(DIALOG_CODE),
    '「打印顺序优化」没有限定在一页两张')
})

t('「倒序打印」两种模板都有', () => {
  assert.ok(/v-if="isVoucher"[^>]*label="倒序打印"/.test(DIALOG_CODE),
    '「倒序打印」没有限定在凭证范围')
})

t('请求里带上 print_order 与 reverse', () => {
  assert.ok(/p\.print_order\s*=/.test(DIALOG_CODE), '请求里没有带 print_order')
  assert.ok(/p\.reverse\s*=/.test(DIALOG_CODE), '请求里没有带 reverse')
})

t('开关一动就重出预览', () => {
  // 三个：错位配对、倒序打印、发票打印（最后一个见 print_invoice.test.mjs）。
  const switches = DIALOG_CODE.match(/el-switch[^>]*@change="refreshPreview"/g) || []
  assert.equal(switches.length, 3, '不是三个开关都接了 refreshPreview')
})

console.log('\n通过 ' + pass + ' / 失败 ' + fail)
process.exit(fail ? 1 : 0)
