/**
 * 发票批次的两件事（src/views/voucher/InvoiceImport.vue）：
 *
 *  ① 「只显示未入账发票」的开关；
 *  ② 已确认过的批次要能**补生成**剩下的发票，所以「作废批次」在那种批次上
 *     必须是禁用的 —— 作废会删记录、删原件，却撤不下已经在账上的凭证。
 *
 * 第 ① 条的口径是「这张发票有没有凭证」，不是「它是什么状态」：
 * 被点了忽略的发票也没入账，它得留在列表里 —— 用户就是靠这个列表
 * 把它取消忽略、补生成凭证的。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/InvoiceImport.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 只显示未入账发票 ==')

t('批次头有这个开关', () => {
  assert.match(VIEW, /只显示未入账发票/)
})

t('开关接的是一个 ref', () => {
  assert.match(VIEW, /const onlyUnbooked = ref\(false\)/)
})

t('表格的数据源是过滤后的那份', () => {
  assert.match(VIEW, /:data="visibleInvoices"/)
  assert.match(VIEW, /const visibleInvoices = computed/)
})

t('过滤口径是「没有凭证」，不是状态', () => {
  const m = VIEW.match(/const visibleInvoices = computed\(\(\) => \{[\s\S]*?\}\)\n/)
  assert.ok(m, '找不到 visibleInvoices')
  assert.match(m[0], /!row\.voucher_id/)
  assert.doesNotMatch(m[0], /row\.status/)
})

t('开关开着时按钮变成「显示全部发票」', () => {
  assert.match(VIEW, /onlyUnbooked \? '显示全部发票' : '只显示未入账发票'/)
})

console.log('== 已生成过的批次不能作废 ==')

t('作废批次在 COMMITTED 批次上是禁用的', () => {
  const m = VIEW.match(/<el-button[^>]*@click="discard"[^>]*>/)
  assert.ok(m, '找不到「作废批次」按钮')
  assert.match(m[0], /batch\.status === 'COMMITTED'/)
})

console.log('== 概要要讲清已入账的张数 ==')

t('概要里有「已入账」', () => {
  assert.match(VIEW, /张已入账/)
  assert.match(VIEW, /committed_count/)
})

console.log(`\n${pass} 通过，${fail} 失败`)
if (fail) process.exit(1)
