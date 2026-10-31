/**
 * 「发票打印」开关（用户 2026-10-31 批注）。
 *
 * 发票导入生成凭证时，原件就是那张凭证的附件（attachments=1）。这个开关
 * 让打印时把发票原件接在凭证后面印出来，剪下来就能订在一起。
 *
 * 盯的是三件事：
 *   1. 开关**只在一页两张**的模板上出现（一页一张时不露出这个设置）；
 *   2. 打开时请求里带上 print_invoices，后端才知道要印发票页；
 *   3. 开关一动就重出预览 —— 预览里必须看得见发票页。
 *
 * 照应的是后端 `app/api/printing.py::print_vouchers` 的同名参数与
 * `app/invoice/print_pages.py`。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const read = (rel) => fs.readFileSync(new URL(rel, import.meta.url), 'utf8')
const DIALOG = read('../src/components/PrintDialog.vue')
const BACKEND_API = read('../../backend/app/api/printing.py')
const BACKEND_PAGES = read('../../backend/app/invoice/print_pages.py')

/** 去掉注释再断言「代码里没有某个写法」—— 解释这个坑的注释本身会命中。 */
const stripComments = (src) => src
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/^\s*\/\/.*$/gm, '')

const DIALOG_CODE = stripComments(DIALOG)
const BACKEND_API_CODE = stripComments(BACKEND_API)

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 弹窗上的「发票打印」开关 ==')

t('弹窗里有这个开关', () => {
  assert.ok(/v-model="invoiceOn"/.test(DIALOG_CODE), '弹窗里没有「发票打印」的开关')
  assert.ok(/label="发票打印"/.test(DIALOG_CODE), '这个开关没有标「发票打印」')
})

t('只在一页两张的凭证模板上出现', () => {
  assert.ok(/v-if="isVoucher && isTwoUp"[^>]*label="发票打印"/.test(DIALOG_CODE),
    '「发票打印」没有限定在一页两张')
})

t('开关一动就重出预览', () => {
  assert.ok(/v-model="invoiceOn"[^>]*@change="refreshPreview"/.test(DIALOG_CODE),
    '开关没有接 refreshPreview —— 预览里看不到发票页')
})

t('请求里带上 print_invoices', () => {
  assert.ok(/p\.print_invoices\s*=/.test(DIALOG_CODE), '请求里没有带 print_invoices')
})

console.log('== 口径与后端一致 ==')

t('后端认得 print_invoices 这个参数', () => {
  assert.ok(/print_invoices: bool = Query\(False\)/.test(BACKEND_API_CODE),
    '后端接口没有 print_invoices 参数')
})

t('发票页由发票模块渲染（不是打印模板里硬写的）', () => {
  assert.ok(BACKEND_PAGES.includes('def invoice_pages_html'),
    '发票页的渲染函数不在发票模块里')
  assert.ok(/app\.invoice\.print_pages/.test(BACKEND_API_CODE),
    '打印接口没有用发票模块')
  assert.ok(/invoice_pages_html/.test(BACKEND_API_CODE),
    '打印接口没有调用发票页的渲染函数')
})

const { errorText } = await import('data:text/javascript;base64,' +
  Buffer.from(read('../src/utils/errorText.js')).toString('base64'))
const makeLoader = new Function('http', 'ElMessage', 'fontsReady', 'errorText',
  read('../src/api/print.js').replace(/^import .*$/gm, '').replace(/\bexport /g, '') +
  '\nreturn loadPrintHtml')
const detail = '发票原件无法打印（批次 1，第 1 页），请检查原件'
for (const [data, expected] of [
  [JSON.stringify({ detail }), detail],
  [{ detail }, detail],
  ['<html>服务不可用</html>', '打印内容生成失败'],
]) {
  const messages = []
  const load = makeLoader({ get: async () => { throw { response: { data } } } },
    { error: message => messages.push(message) }, null, errorText)
  const result = await load('/print/vouchers.html', { print_invoices: true })
  t('原件读取失败的原因能显示给用户', () => {
    assert.equal(result, null)
    assert.deepEqual(messages, [expected])
  })
}

console.log('\n通过 ' + pass + ' / 失败 ' + fail)
process.exit(fail ? 1 : 0)
