/**
 * 「忽略这张」点了就生效，而且**不会**被下一次重算抹掉。
 *
 * 两个毛病：
 *
 *  ① 点「忽略这张」，状态那一列还是「可生成」/「有问题」—— 得再点一次
 *     「重新计算预览」才看得出改没改成。忽略是用户**已经做好**的决定，
 *     不该逼着人再点一次才能看到结果。
 *
 *  ② 从历史里重开批次、点「重新计算预览」，早就点名忽略的发票被重新算
 *     成「可生成」—— 上一次的决定被一次重算悄悄抹掉。
 *
 * 前端这一侧盯三件事：
 *  - 点「忽略这张」会**调接口**，不是只翻一个本地标记；
 *  - 调完把**这一行**就地换掉（整批换掉会把用户展开的行全收起来）；
 *  - 落库失败要把本地意图收回来，界面不能和库里对不上。
 *
 * ② 的另一半（服务端认得住落库的「已忽略」）由后端用例盯着：
 * backend/tests/test_invoice_ignore_state.py。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/InvoiceImport.vue', import.meta.url), 'utf8')
const API = fs.readFileSync(
  new URL('../src/api/invoice.js', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

/** 取出某个函数的源码（连前面的 async 一起）。 */
function fnSource(name) {
  const start = VIEW.indexOf(`function ${name}`)
  assert.notEqual(start, -1, `找不到 ${name}`)
  const async = VIEW.slice(Math.max(0, start - 6), start) === 'async '
  const from = async ? start - 6 : start
  const rest = VIEW.slice(from)
  const end = rest.indexOf('\n}\n')
  return rest.slice(0, end + 2)
}

console.log('== 点「忽略这张」要调接口，不能只翻本地标记 ==')

t('接口客户端有 setRecordIgnored', () => {
  assert.match(API, /export const setRecordIgnored/)
  assert.match(API, /records\/\$\{recordId\}\/ignore/)
})

t('toggleIgnored 会调 setRecordIgnored', () => {
  const src = fnSource('toggleIgnored')
  assert.match(src, /await invoiceApi\.setRecordIgnored\(/)
})

t('toggleIgnored 是异步的 —— 不然等不到落库', () => {
  const src = fnSource('toggleIgnored')
  assert.match(src, /async function toggleIgnored/)
})

t('传的 ignored 是「点完之后」该有的状态，不是原状态', () => {
  const src = fnSource('toggleIgnored')
  assert.match(src, /const next = !isIgnored\(row\)/)
  assert.match(src, /row\.id, next,/)
})

t('连当前的人工覆盖项一起发过去', () => {
  const src = fnSource('toggleIgnored')
  assert.match(src, /overridePayload\(\)/)
})

console.log('== 调完要把这一行换掉 ==')

t('有 mergeBatchPayload', () => {
  assert.match(VIEW, /function mergeBatchPayload/)
})

t('就地合并：不是整批换掉 batch.value', () => {
  const src = fnSource('mergeBatchPayload')
  // 整批换掉会让 el-table 的 data 变成新数组，default-expand-all 把几十张
  // 票全摊开 —— 用户点忽略想看的那张说明，反而给收起来了。
  assert.doesNotMatch(src, /batch\.value = payload\s*;?\s*\}\s*$/m,
    '不能整批替换 batch.value')
  assert.match(src, /Object\.assign\(current, rest\)/)
  assert.match(src, /Object\.assign\(row, updated\)/)
})

t('toggleIgnored 调的是 mergeBatchPayload', () => {
  const src = fnSource('toggleIgnored')
  assert.match(src, /mergeBatchPayload\(payload\)/)
})

console.log('== 落库失败要把本地意图收回来 ==')

t('失败时回滚 overrides', () => {
  const src = fnSource('toggleIgnored')
  assert.match(src, /catch \(err\)/)
  assert.match(src, /delete overrides\[row\.id\]\.ignored/)
  assert.match(src, /overrides\[row\.id\]\.ignored = before/)
})

t('失败要有话讲给用户听', () => {
  const src = fnSource('toggleIgnored')
  assert.match(src, /ElMessage\.error\(/)
})

console.log('== 按钮上要有进行中的状态 ==')

t('有 ignoringId', () => {
  assert.match(VIEW, /const ignoringId = ref\(null\)/)
})

t('「忽略这张」按钮带 loading', () => {
  assert.match(VIEW, /:loading="ignoringId === row\.id"[^\n]*忽略这张/)
})

t('「取消忽略」按钮带 loading', () => {
  assert.match(VIEW, /:loading="ignoringId === row\.id"[^\n]*取消忽略/)
})

console.log(`\n${pass} 通过，${fail} 失败`)
if (fail) process.exit(1)
