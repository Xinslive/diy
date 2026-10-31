/**
 * 发票导入的「可能重复记账」提示。
 *
 * **这件事要解决什么。**
 *
 * 有些发票是**手工先记了账**，后来才把票传上来导入 —— 再生成一张凭证，
 * 同一笔钱就在账上记了两遍。系统拦不住（发票号码才是唯一身份，金额相同
 * 不代表是同一笔），所以导入时要**提示**：这张发票的金额，在它开票那个月
 * 的账上，已经有对应的分录了。
 *
 * 提示要**看得见**：这一批几十张票摊开之后，藏在展开行里的提示等于没有
 * 提示 —— 所以「状态」那一列上也要挂一个标签，概要那一行也要报数，
 * 顶上还要有一条整批的提醒。
 *
 * 判定口径在 ``backend/app/invoice/duplicates.py``：同月 + 同额 + 同向 +
 * 同结算科目。这组用例盯**前端这一侧**：提示长什么样、摆在哪、有几句。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

import {
  DUPLICATE_WARNING_LABEL,
  duplicateWarningCount,
  duplicateWarningMatches,
  duplicateWarningText,
} from '../src/views/voucher/invoice_upload.js'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/InvoiceImport.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

const hit = {
  voucher_no: '记-0001',
  voucher_date: '2026-10-05',
  voucher_status: 'POSTED',
  subject_code: '1002',
  subject_name: '银行存款',
  direction: 'CREDIT',
  amount: '100.00',
  summary: '付油款',
}
const row = {
  id: 'r1',
  duplicate_warning: {
    period: '2026-10',
    amount: '100.00',
    direction: 'CREDIT',
    matches: [hit],
  },
}

console.log('== 提示文案要把「是哪一笔」说清楚 ==')

t('有命中就有一句提示', () => {
  assert.ok(duplicateWarningText(row), '有命中却没有提示文案')
})

t('文案里点名期间、凭证号、日期、方向、科目、金额', () => {
  const text = duplicateWarningText(row)
  assert.match(text, /2026-10/)
  assert.match(text, /记-0001/)
  assert.match(text, /2026-10-05/)
  assert.match(text, /贷/)
  assert.match(text, /1002/)
  assert.match(text, /银行存款/)
  assert.match(text, /100\.00/)
})

t('摘要也带上（用户靠它认出是哪一笔）', () => {
  assert.match(duplicateWarningText(row), /付油款/)
})

t('方向认借方', () => {
  const debit = { duplicate_warning: { period: '2026-10', matches: [{ ...hit, direction: 'DEBIT' }] } }
  assert.match(duplicateWarningText(debit), /借/)
})

t('没有命中就没有提示（空串，不是「undefined」）', () => {
  assert.equal(duplicateWarningText({}), '')
  assert.equal(duplicateWarningText({ duplicate_warning: null }), '')
  assert.equal(duplicateWarningText({ duplicate_warning: { period: '2026-10', matches: [] } }), '')
  assert.equal(duplicateWarningText(undefined), '')
})

t('多条命中都说出来，不只说第一条', () => {
  const two = { duplicate_warning: { period: '2026-10', matches: [hit, { ...hit, voucher_no: '记-0002' }] } }
  const text = duplicateWarningText(two)
  assert.match(text, /记-0001/)
  assert.match(text, /记-0002/)
})

console.log('== 概要上要数得出来 ==')

t('有几张要提示', () => {
  assert.equal(duplicateWarningCount([row, {}, row]), 2)
})

t('一张都没有时是 0（不是 undefined）', () => {
  assert.equal(duplicateWarningCount([]), 0)
  assert.equal(duplicateWarningCount(undefined), 0)
})

t('命中清单取出来是数组', () => {
  assert.equal(duplicateWarningMatches(row).length, 1)
  assert.deepEqual(duplicateWarningMatches({}), [])
})

console.log('== 界面上摆在哪 ==')

t('「状态」那一列上挂一个提示标签', () => {
  // 「状态」这一列有**两处**（处理历史里那张表也有一列状态），
  // 所以按标签区本身定位，别去匹配第一处「状态」列。
  const m = VIEW.match(/<div class="status-cell">[\s\S]*?<\/div>/)
  assert.ok(m, '找不到状态列里的标签区')
  assert.match(m[0], /duplicateWarningMatches\(row\)\.length/)
  assert.match(m[0], /DUPLICATE_WARNING_LABEL/)
})

t('展开行里也有一块提示（说清楚是哪张凭证）', () => {
  assert.match(VIEW, /class="duplicate-warning"/)
  assert.match(VIEW, /duplicateWarningText\(row\)/)
})

t('提示摆得比错误红字靠前 —— 错误是「这张有问题」，提示是「你确认一下」', () => {
  const dupAt = VIEW.indexOf('class="duplicate-warning"')
  const errAt = VIEW.indexOf('class="invoice-errors"')
  assert.ok(dupAt > 0 && errAt > 0, '提示块或错误块没找到')
  assert.ok(dupAt < errAt, '提示块被排到错误块后面了')
})

t('概要那一行报出「N 张可能重复记账」', () => {
  assert.match(VIEW, /张可能重复记账/)
  assert.match(VIEW, /duplicateCount/)
})

t('顶上有一条整批的提醒', () => {
  assert.match(VIEW, /<el-alert v-if="duplicateCount" type="warning"/)
})

t('用的图标是警示那个，不是信息那个', () => {
  assert.match(VIEW, /<el-icon><WarningFilled \/><\/el-icon>/)
})

console.log(`\n${pass} 通过，${fail} 失败`)
if (fail) process.exit(1)
