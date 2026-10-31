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
const makeLoader = new Function('http', 'ElMessage', 'fontsReady', 'errorText', 'isCanceled',
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
    { error: message => messages.push(message) }, null, errorText, () => false)
  const result = await load('/print/vouchers.html', { print_invoices: true })
  t('原件读取失败的原因能显示给用户', () => {
    assert.equal(result, null)
    assert.deepEqual(messages, [expected])
  })
}

const canceledMessages = []
const canceledLoad = makeLoader({ get: async () => { throw { code: 'ERR_CANCELED' } } },
  { error: message => canceledMessages.push(message) }, null, errorText, e => e.code === 'ERR_CANCELED')
assert.equal(await canceledLoad('/print/vouchers.html'), null)
t('取消过期预览不报错', () => assert.deepEqual(canceledMessages, []))

const controllerSource = DIALOG_CODE.slice(DIALOG_CODE.indexOf('let token = 0'),
  DIALOG_CODE.indexOf('watch(() => props.modelValue'))
const makeController = new Function('props', 'loading', 'printing', 'html', 'error', 'visible',
  'buildParams', 'loadPrintHtml', 'openPrintWindow', 'printWindow',
  controllerSource + '\nreturn { refreshPreview, doPrint, stopPreview }')
const loading = { value: false }, printing = { value: false }, html = { value: '' }
const error = { value: '' }, visible = { value: true }, requests = [], printed = []
const controller = makeController({ path: '/print/vouchers.html' }, loading, printing, html,
  error, visible, () => ({ print_invoices: true }),
  (path, params, options) => new Promise(resolve => requests.push({ options, resolve })),
  doc => ({ doc }), w => printed.push(w.doc))
const oldRequest = controller.refreshPreview()
const newRequest = controller.refreshPreview()
t('换设置时取消旧预览请求', () => assert.equal(requests[0].options.signal.aborted, true))
requests[1].resolve('<html>new preview</html>')
await newRequest
requests[0].resolve('<html>old preview</html>')
await oldRequest
controller.doPrint()
t('打印复用最新预览而不再次下载', () => {
  assert.equal(requests.length, 2)
  assert.deepEqual(printed, ['<html>new preview</html>'])
  assert.equal(visible.value, false)
})
visible.value = true
const pendingRequest = controller.refreshPreview()
controller.doPrint()
controller.stopPreview()
requests[2].resolve(null)
await pendingRequest
t('预览未完成不能打印旧结果，关闭后取消加载', () => {
  assert.equal(printed.length, 1)
  assert.equal(requests[2].options.signal.aborted, true)
  assert.equal(loading.value, false)
})

const makePrintWindow = new Function('http', 'ElMessage', 'fontsReady', 'errorText', 'isCanceled', 'setTimeout',
  read('../src/api/print.js').replace(/^import .*$/gm, '').replace(/\bexport /g, '') + '\nreturn printWindow')
let decodeImage, printCount = 0
const imageReady = new Promise(resolve => { decodeImage = resolve })
const printLoaded = makePrintWindow(null, { error: () => assert.fail('图片加载应成功') },
  () => Promise.resolve(), errorText, () => false, fn => fn())
printLoaded({ document: { images: [{ decode: () => imageReady }] }, focus() {}, print() { printCount++ } })
await Promise.resolve()
t('图片尚未解码时不唤起打印', () => assert.equal(printCount, 0))
decodeImage()
await imageReady
await Promise.resolve()
await Promise.resolve()
t('图片解码完成后才打印', () => assert.equal(printCount, 1))
const imageErrors = []
const printBroken = makePrintWindow(null, { error: message => imageErrors.push(message) },
  () => Promise.resolve(), errorText, () => false, fn => fn())
printBroken({ document: { images: [{ decode: () => Promise.reject(new Error('broken image')) }] },
  focus() {}, print() { printCount++ } })
await Promise.resolve()
await Promise.resolve()
await Promise.resolve()
t('图片加载失败时阻止打印并说明原因', () => {
  assert.equal(printCount, 1)
  assert.equal(imageErrors.length, 1)
})

console.log('\n通过 ' + pass + ' / 失败 ' + fail)
process.exit(fail ? 1 : 0)
