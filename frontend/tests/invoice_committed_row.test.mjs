/**
 * 状态「已生成」（已入账）的发票**不摆「忽略这张」**。
 *
 * 为什么：服务端对已入账的记录**直接跳过重算**
 * （`service._build_preview` 里「已入账的不重算」那条），所以对一张
 * 已经生成过凭证的发票点「忽略这张」、改科目、改类型 —— **点了什么也
 * 不会发生**。摆一排按了没反应的控件，用户只会以为系统坏了。
 *
 * 顺带盯两件事：
 *  ① 「入账科目」那一列对已入账的行改成**只读文字** —— 既然改不了，
 *     就不该摆个下拉框装样子。
 *  ② 模板里那个 v-else 认的是**紧挨着的那一个元素**：上一版在「已忽略」
 *     的原生表格后面插了个东西，v-else 就认错了，被忽略的行反而摊出了
 *     整排科目下拉。所以这里统一用显式的 v-if。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

import { isCommittedRow } from '../src/views/voucher/invoice_upload.js'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/InvoiceImport.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 已入账的行怎么判 ==')

t('状态是 COMMITTED 就算已入账', () => {
  assert.equal(isCommittedRow({ status: 'COMMITTED' }), true)
})

t('别的状态不算', () => {
  for (const status of ['READY', 'REJECTED', 'IGNORED', 'DISCARDED', '']) {
    assert.equal(isCommittedRow({ status }), false, status)
  }
})

t('没有行也不炸', () => {
  assert.equal(isCommittedRow(undefined), false)
  assert.equal(isCommittedRow(null), false)
})

console.log('== 界面上不摆那些按了没反应的控件 ==')

t('工具条整个不摊给已入账的行', () => {
  assert.match(VIEW, /<div v-if="!isCommitted\(row\)" class="detail-toolbar">/)
})

t('「忽略这张」确实在工具条里面（所以跟着一起被挡住）', () => {
  const m = VIEW.match(/<div v-if="!isCommitted\(row\)" class="detail-toolbar">[\s\S]*?<div v-if="row\.errors\?\.length" class="invoice-errors">/)
  assert.ok(m, '找不到工具条那一段')
  assert.ok(m[0].includes('忽略这张'), '工具条里没有「忽略这张」')
})

t('「重新解析」也在工具条里，跟着一起被挡住', () => {
  const m = VIEW.match(/<div v-if="!isCommitted\(row\)" class="detail-toolbar">[\s\S]*?<div v-if="row\.errors\?\.length" class="invoice-errors">/)
  assert.ok(m, '找不到工具条那一段')
  assert.ok(m[0].includes('重新解析'), '「重新解析」不在工具条里')
})

console.log('== 已入账的行：入账科目只读 ==')

t('入账科目那一列对已入账的行给的是文字，不是下拉', () => {
  const m = VIEW.match(/<el-table-column label="入账科目"[\s\S]*?<\/el-table-column>/)
  assert.ok(m, '找不到「入账科目」这一列')
  assert.match(m[0], /v-if="isCommitted\(row\)" class="subject-text"/)
  assert.match(m[0], /<el-select v-else/)
})

t('只读文字显示的是「编码 名称」', () => {
  assert.match(VIEW, /function lineSubjectText\(row, index\)/)
  assert.match(VIEW, /item\.subject_code\) return `\$\{item\.subject_code\} \$\{item\.subject_name/)
})

console.log('== v-else 认的是相邻元素，别再插东西 ==')

t('被忽略的那一段用显式 v-if，不用 v-else', () => {
  assert.match(VIEW, /<template v-if="!isIgnored\(row\)">/)
  assert.doesNotMatch(VIEW, /<template v-else>/)
})

console.log('== 凭证预览里那串科目名不能是空的 ==')

t('预览用兜底的取名方式', () => {
  assert.match(VIEW, /function entrySubjectText\(entry\)/)
  assert.match(VIEW, /entrySubjectText\(entry\)/)
})

console.log(`\n${pass} 通过，${fail} 失败`)
if (fail) process.exit(1)
