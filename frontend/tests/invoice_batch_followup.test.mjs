/**
 * 发票批次的两件事（src/views/voucher/InvoiceImport.vue）：
 *
 *  ① 「只显示未入账发票」的开关，**默认是开着的**（用户 2026-10-26 要求）；
 *  ② 已确认过的批次要能**补生成**剩下的发票，所以「作废批次」在那种批次上
 *     必须是禁用的 —— 作废会删记录、删原件，却撤不下已经在账上的凭证。
 *
 * 第 ① 条的口径是「这张发票有没有凭证」，不是「它是什么状态」：
 * 被点了忽略的发票也没入账，它得留在列表里 —— 用户就是靠这个列表
 * 把它取消忽略、补生成凭证的。
 *
 * 默认开着还有两条配套：藏起来的张数要讲出来（不然看着像丢了发票），
 * 整批都已入账时空表要把话讲全（不然看着像这批没东西）。
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
  assert.match(VIEW, /const onlyUnbooked = ref\((true|false)\)/)
})

t('默认只显示未入账发票（默认开着）', () => {
  assert.match(VIEW, /const onlyUnbooked = ref\(true\)/)
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

t('藏起来的张数不再用灰字提示（用户 2026-10-29 批注去掉）', () => {
  // 那句「当前显示未入账 N 张，已隐藏 M 张已入账发票」去掉了 ——
  // 张数在上面的概要里已经写着（「共 N 张发票，N 张可生成，N 张已入账」），
  // 再摆一段灰字只是占掉主操作的位置。
  // 断言的是**模板里不再渲染它**，不是「这句话在文件里不出现」——
  // 注释里提一句「这句去掉了」是正常的。
  assert.doesNotMatch(VIEW, /\{\{\s*filterHint\s*\}\}/)
  assert.doesNotMatch(VIEW, /const filterHint = computed/)
  // 计数本身还在（概要里那一句）。
  assert.match(VIEW, /const bookedCount = computed/)
  assert.match(VIEW, /const batchSummaryTitle = computed/)
  assert.match(VIEW, /张已入账/)
})

t('开关不抢主按钮的样式', () => {
  // 这一排里只有「确认生成」该是主按钮；开关染成主色就分不清谁是正事。
  assert.doesNotMatch(VIEW, /onlyUnbooked \? 'primary'/)
})

t('整批都已入账时，空表把话讲全', () => {
  assert.match(VIEW, /<template #empty>/)
  assert.match(VIEW, /const emptyHint = computed/)
  assert.match(VIEW, /都已入账 —— 点「显示全部发票」看全部记录。/)
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
