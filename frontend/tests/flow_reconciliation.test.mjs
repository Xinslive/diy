/**
 * 流水对账：**按钮可用性、状态文案、结算科目来源徽标**。
 *
 * 这些是这个页面的业务核心，藏在 .vue 里就只能靠正则去猜它对不对 ——
 * 所以纯逻辑抽到 ``views/bank/flow_state.js``，这里直接跑那份源码。
 *
 * 三条要盯住的：
 *
 *  ① **待确认科目的行不许生成凭证** —— 按钮的可用性直接由后端给的候选
 *     清单决定（``canCommit``），前端**不自己再判一遍**：两边各判一次的话，
 *     迟早出现「按钮能点、点了没反应」。
 *  ② **结算科目的来源必须标出来** —— 结算科目决定钱记到哪个银行账户，
 *     「这个科目为什么跟我配的不一样」是用户第一个会问的问题。
 *  ③ **方向未知的行要单独标出来** —— 它等的是「定方向」，不是「定科目」。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const STATE = await import('../src/views/bank/flow_state.js')
const {
  canCommit, diffClass, fieldLabel, settlementSourceText, settlementSourceType,
  toColumnMapping, voucherStateText, voucherStateType, FIELD_OPTIONS,
  toCents, planSubjectCents, planTotalMatches, planTotalDiff, planSubjectText,
  reclassifyRowCount, reclassifyCandidateText, unknownTemplateFields,
  isCrossPeriod, reclassifyCrossPeriodText, reclassifyCrossPeriodConfirm,
  SUMMARY_TEMPLATE_FIELDS,
} = STATE

const VIEW = fs.readFileSync(
  new URL('../src/views/bank/FlowReconciliation.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 生成按钮：待确认的行必须先定科目 ==')

t('没有候选时不能点「生成流水凭证」', () => {
  assert.equal(canCommit(null), false)
  assert.equal(canCommit({ candidate_count: 0 }), false)
  assert.equal(canCommit({}), false)
})

t('有候选时可以点', () => {
  assert.equal(canCommit({ candidate_count: 3 }), true)
})

t('按钮的可用性直接用 canCommit，没有在前端另判一遍', () => {
  assert.match(VIEW, /:disabled="!store\.canWrite \|\| !canCommit\(plan\)"/)
  // 前端不该自己数「待确认的行」来决定能不能生成
  assert.doesNotMatch(VIEW, /candidate_count\s*>/)
})

t('「待确认科目」的行在计划里被单独点名', () => {
  // 后端 plan 的 skipped 里带着 reason，前端据此显示「待确认科目 N 条」
  assert.match(VIEW, /plan\.pending_count/)
})

console.log('== 结算科目来源徽标 ==')

t('三种来源各有一个说法', () => {
  assert.equal(settlementSourceText('MANUAL'), '人工指定')
  assert.equal(settlementSourceText('STATEMENT'), '对账单识别')
  assert.equal(settlementSourceText('CONFIG'), '配置科目')
})

t('来源徽标用不同的颜色', () => {
  const types = ['MANUAL', 'STATEMENT', 'CONFIG'].map(settlementSourceType)
  assert.equal(new Set(types).size, 3, '三种来源不该是同一个颜色')
})

t('发票导入页把来源标出来', () => {
  const INVOICE = fs.readFileSync(
    new URL('../src/views/voucher/InvoiceImport.vue', import.meta.url), 'utf8')
  assert.match(INVOICE, /settlement_source/)
  assert.match(INVOICE, /settlement_note/)
  assert.match(INVOICE, /对账单识别/)
})

t('发票导入页讲清楚有没有对账单可匹配', () => {
  const INVOICE = fs.readFileSync(
    new URL('../src/views/voucher/InvoiceImport.vue', import.meta.url), 'utf8')
  assert.match(INVOICE, /statementHint/)
  assert.match(INVOICE, /statementCount/)
})

console.log('== 流水行状态 ==')

t('方向未知与待确认科目是两回事', () => {
  assert.equal(voucherStateText('UNKNOWN'), '方向未知')
  assert.equal(voucherStateText('PENDING'), '待确认科目')
  assert.notEqual(voucherStateText('UNKNOWN'), voucherStateText('PENDING'))
})

t('已被发票占用与已入账都标出来', () => {
  assert.equal(voucherStateText('INVOICE'), '已被发票占用')
  assert.equal(voucherStateText('POSTED'), '已入账')
})

console.log('== 余额差额的着色 ==')

t('对上了是绿的、能解释是黄的、原因待查是红的', () => {
  assert.equal(diffClass({ verdict: 'MATCH' }), 'diff-ok')
  assert.equal(diffClass({ verdict: 'EXPLAINED' }), 'diff-warn')
  assert.equal(diffClass({ verdict: 'UNEXPLAINED' }), 'diff-bad')
  assert.equal(diffClass(null), '')
})

console.log('== 手工指定列 ==')

t('列映射是「字段 → 列号」', () => {
  const mapping = toColumnMapping({ 0: 'DATE', 2: 'IN', 5: '' })
  assert.deepEqual(mapping, { DATE: 0, IN: 2 })
})

t('字段名都有中文说法', () => {
  for (const f of FIELD_OPTIONS) {
    assert.ok(fieldLabel(f.value), `${f.value} 没有中文标签`)
  }
  assert.equal(fieldLabel('DATE'), '日期')
  assert.equal(fieldLabel('BALANCE'), '余额')
})

t('页面提供「记住这个格式」与「按指定重新解析」', () => {
  assert.match(VIEW, /记住这个格式/)
  assert.match(VIEW, /按指定重新解析/)
})

console.log('== 流水明细页：默认只显示解析出来的内容 ==')

t('方向、对方科目、状态都收在「对账信息」栏里（默认收起）', () => {
  // 收起状态 = showExtra 默认 false
  assert.match(VIEW, /const showExtra = ref\(false\)/)
  // 这三列都挂在 showExtra 上
  for (const label of ['方向', '对方科目', '状态']) {
    assert.match(
      VIEW,
      new RegExp(`<el-table-column v-if="showExtra" label="${label}"`),
      `${label} 列没收进「对账信息」栏`)
  }
})

t('默认就显示的那些列是流水解析出来的内容', () => {
  for (const label of ['日期', '摘要', '用途', '对方户名', '收入', '支出', '余额']) {
    assert.match(VIEW, new RegExp(`<el-table-column label="${label}"`))
  }
})

t('表格用紧凑尺寸', () => {
  assert.match(VIEW, /<el-table[\s\S]*?size="small"[\s\S]*?class="flow-rows-table"/)
})

console.log('== 一行流水对应多个对方科目 ==')

t('金额按「分」算，不走浮点', () => {
  assert.equal(toCents('0.1'), 10)
  assert.equal(toCents('1234.56'), 123456)
  assert.equal(toCents(''), 0)
  assert.equal(toCents(null), 0)
  assert.equal(toCents('12'), 1200)
})

t('合计必须等于流水金额，差一分钱都不算', () => {
  const subjects = [
    { subject_id: 1, amount: '617.28' },
    { subject_id: 2, amount: '617.28' },
  ]
  assert.equal(planSubjectCents(subjects), 123456)
  assert.equal(planTotalMatches(subjects, '1234.56'), true)
  assert.equal(planTotalMatches(subjects, '1234.57'), false)
  assert.equal(planTotalDiff(subjects, '1234.56'), 0)
  assert.equal(planTotalDiff(subjects, '1234.00'), 56)
})

t('浮点相加会飘，但这里不会', () => {
  // 0.1 + 0.2 在双精度里是 0.30000000000000004 —— 金额不能这么算
  const subjects = [
    { subject_id: 1, amount: '0.1' },
    { subject_id: 2, amount: '0.2' },
  ]
  assert.equal(planTotalMatches(subjects, '0.30'), true)
})

t('对方科目的显示文本带金额', () => {
  assert.equal(
    planSubjectText({ subject_code: '6602', subject_name: '业务招待费', amount: '800.00' }),
    '6602 业务招待费 800.00')
})

t('页面上能增删多个对方科目', () => {
  assert.match(VIEW, /再加一个科目/)
  assert.match(VIEW, /removePlanItem/)
  assert.match(VIEW, /addPlanItem/)
  // 合计与流水金额不一致时不让保存
  assert.match(VIEW, /:disabled="!planTotalOk"/)
})

console.log('== 改科目：账上已有同一笔分录 ==')

t('数得清有多少行可以改科目', () => {
  assert.equal(reclassifyRowCount([]), 0)
  assert.equal(reclassifyRowCount([{ reclassify_candidates: [] }]), 0)
  assert.equal(reclassifyRowCount([{ reclassify_candidates: [{ entry_id: 1 }] }]), 1)
  assert.equal(reclassifyRowCount([
    { reclassify_candidates: [{ entry_id: 1 }] },
    { reclassify_candidates: [] },
    { reclassify_candidates: [{ entry_id: 2 }] },
  ]), 2)
})

t('页面上有「可改科目」的入口与改科目弹窗', () => {
  assert.match(VIEW, /可改科目/)
  assert.match(VIEW, /openReclassify/)
  assert.match(VIEW, /doReclassify/)
  assert.match(VIEW, /reclassifyRow/)
})

t('候选的显示文本说清楚「记在哪个科目上」', () => {
  const text = reclassifyCandidateText({
    subject_code: '1001', subject_name: '库存现金',
    amount: '5000.00', voucher_no: '记0003',
  })
  assert.match(text, /1001/)
  assert.match(text, /库存现金/)
  assert.match(text, /5000\.00/)
  assert.match(text, /记0003/)
})

t('跨期的候选认得出来', () => {
  assert.equal(isCrossPeriod({ in_window: false, days_diff: 28 }), true)
  assert.equal(isCrossPeriod({ in_window: true, days_diff: 3 }), false)
  // 后端没给这个字段时（老数据、接口出错）宁可不提示
  assert.equal(isCrossPeriod({ days_diff: 28 }), false)
  assert.equal(isCrossPeriod(null), false)
})

t('跨期候选要写出日期差，让人确认是不是同一笔', () => {
  assert.match(reclassifyCrossPeriodText({ in_window: false, days_diff: 28 }), /28/)
  assert.match(reclassifyCrossPeriodText({ in_window: false, days_diff: 28 }), /同一笔/)
  // 窗口内的不写这句
  assert.equal(reclassifyCrossPeriodText({ in_window: true, days_diff: 3 }), '')
})

t('二次确认的话要说清风险', () => {
  const msg = reclassifyCrossPeriodConfirm({ in_window: false, days_diff: 28 })
  assert.match(msg, /28/)
  assert.match(msg, /同一笔/)
})

t('页面上：跨期候选有提示，且点下去要再确认一次', () => {
  assert.match(VIEW, /isCrossPeriod/)
  assert.match(VIEW, /reclassifyCrossPeriodText/)
  assert.match(VIEW, /reclassifyCrossPeriodConfirm/)
  assert.match(VIEW, /ElMessageBox\.confirm/)
})

console.log('== 凭证摘要模板 ==')

t('占位符清单与后端一致', () => {
  assert.deepEqual(SUMMARY_TEMPLATE_FIELDS,
    ['摘要', '用途', '对方户名', '日期', '金额', '方向', '序号'])
})

t('不认识的占位符要能查出来', () => {
  assert.deepEqual(unknownTemplateFields('流水 {用途}'), [])
  assert.deepEqual(unknownTemplateFields(''), [])
  assert.deepEqual(unknownTemplateFields('{摘要}{瞎写的}'), ['瞎写的'])
})

t('页面能设账套级与规则级摘要模板', () => {
  assert.match(VIEW, /凭证摘要模板（账套级）/)
  assert.match(VIEW, /configForm\.summary_template/)
  assert.match(VIEW, /saveRuleTemplate/)
  assert.match(VIEW, /summary_template/)
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
