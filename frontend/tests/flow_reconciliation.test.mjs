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
  canCommit, canEditPlanSubject, canToggleIgnore, diffClass, fieldLabel,
  settlementSourceText, settlementSourceType,
  toColumnMapping, voucherStateText, voucherStateType, FIELD_OPTIONS,
  toCents, planSubjectCents, planTotalMatches, planTotalDiff, planSubjectText,
  reclassifyRowCount, reclassifyCandidateText, unknownTemplateFields,
  isCrossPeriod, reclassifyCrossPeriodText, reclassifyCrossPeriodConfirm,
  renderSummary, summaryTemplateValuesText, SUMMARY_TEMPLATE_EXAMPLES,
  SUMMARY_TEMPLATE_FIELDS, planSubjectSummary, defaultExpandedRowIds,
  suspiciousRows, statementsInPeriod, statementsOtherPeriods,
  bankAccountSubjects,
} = STATE

const VIEW = fs.readFileSync(
  new URL('../src/views/bank/FlowReconciliation.vue', import.meta.url), 'utf8')

/**
 * 抠出**流水明细那张表**的 ``<el-table>`` 开标签（含全部属性）。
 *
 * 为什么不用正则去猜：流水对账这个 .vue 里有三张 el-table（对账单台账、科目规则、
 * 流水明细），「默认展开」这种事必须**钉在流水明细那张**上 —— 钉错了表，
 * 用例照样绿，而默认展开根本没生效。
 */
function rowsTableTag() {
  const at = VIEW.indexOf('class="flow-rows-table"')
  assert.ok(at > 0, '找不到流水明细表')
  const start = VIEW.lastIndexOf('<el-table', at)
  const end = VIEW.indexOf('>', at)
  assert.ok(start > 0 && end > at, '流水明细表的开标签抠不出来')
  return VIEW.slice(start, end + 1)
}

/** 抠出**科目规则那张表**（:data="rules"）的整段。 */
function rulesTableBlock() {
  const at = VIEW.indexOf(':data="rules"')
  assert.ok(at > 0, '找不到科目规则表')
  const end = VIEW.indexOf('</el-table>', at)
  assert.ok(end > at, '科目规则表抠不出整段')
  return VIEW.slice(at, end)
}

/**
 * 去掉注释（**HTML 的与 CSS 的都要去**）。
 *
 * 这些 .vue 里**注释写得很长**（把「为什么这么改」也写在旁边），而注释里
 * 出现某个词（哪怕是「预览」「toggleRowExpansion」「已入账…定死」）并不等于
 * 界面上有它。断言「界面上没有 X」时必须先剥掉注释，否则会被自己写的说明
 * 文字绊倒 —— 而这段代码里连 ``<style>`` 里的说明文字都算注释。
 */
function stripComments(text) {
  return text
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
}

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

console.log('== 流水明细页：对账信息默认摊开（但只摊「还有活要干的行」）==')

t('方向、对方科目、状态收在展开行里（默认展开，且是往下展开）', () => {
  // **往下展开**：用展开行，不是往右加列。
  // 加列会把表格撑宽 → 窄屏上冒出左右滑动条，一边横拖一边看很难受。
  assert.match(VIEW, /type="expand"/)
  assert.match(VIEW, /class="flow-extra"/)
  // 三样都在展开行里
  for (const label of ['方向', '对方科目', '状态']) {
    assert.match(VIEW, new RegExp(`>${label}</span>`), `${label} 不在展开行里`)
  }
  // **不许**再有那三个「往右加」的列
  //
  // 只看**流水明细这张表**（``.flow-rows-table``）—— 同一个 .vue 里还有
  // 对账单台账（「状态」列）与科目规则（「对方科目」列）两张表，
  // 那些列是正经的，别一起禁掉。
  const rowsTableStart = VIEW.indexOf('class="flow-rows-table"')
  assert.ok(rowsTableStart > 0, '找不到流水明细表')
  const rowsTableBlock = VIEW.slice(rowsTableStart, VIEW.indexOf('</el-table>', rowsTableStart))
  for (const label of ['方向', '对方科目', '状态']) {
    assert.doesNotMatch(
      rowsTableBlock,
      new RegExp(`<el-table-column[^>]*label="${label}"`),
      `${label} 还在流水明细表里当列 —— 那就会把表格撑宽、出左右滑动条`)
  }
})

t('默认摊开的是「还有活要干的行」；已入账、被发票占用的收起', () => {
  // 用户要求（2026-10-28）：「已入账的流水明细默认收起」、
  // 「被发票占用的流水……也默认收起」。
  //
  // **判据 = 对方科目还能不能改**：还能改 = 还有活；不能改 = 账上（或发票上）
  // 已经有定论了 —— 那些行的对账信息看一眼就够，全摊开反而把要处理的那
  // 几行淹了。
  const rows = [
    { id: 1, matched_voucher_id: null, matched_invoice_record_id: null },
    { id: 2, matched_voucher_id: 9, matched_invoice_record_id: null },
    { id: 3, matched_voucher_id: null, matched_invoice_record_id: 4 },
    { id: 4, matched_voucher_id: null, matched_invoice_record_id: null },
  ]
  assert.deepEqual(defaultExpandedRowIds(rows), [1, 4])
  assert.deepEqual(defaultExpandedRowIds([]), [])
  assert.deepEqual(defaultExpandedRowIds(null), [])
})

t('按行控制展开要用 row-key + expand-row-keys，不能用 default-expand-all', () => {
  // ``:default-expand-all`` 是「全展或全不展」，没有「哪些行展」这一说 ——
  // 要按行控制就只能用 ``row-key`` + ``:expand-row-keys``。
  //
  // **少 row-key 会当场抛错**：``setExpandRowKeys`` 里第一句就是
  // ``assertRowKey()``（row-key 没配就 throw "[ElTable] prop row-key is required"）。
  const tag = rowsTableTag()
  assert.match(tag, /row-key="id"/, '流水明细表没配 row-key')
  assert.match(tag, /:expand-row-keys="expandedRowIds"/, '没按行控制展开')
  assert.doesNotMatch(tag, /:default-expand-all/, '还在用 default-expand-all')
})

t('用户手动开过/关过的行不会被下一次刷新弹回去', () => {
  // ``:expand-row-keys`` 一变，el-table 把展开状态**整个换成**那批 key
  // （setExpandRowKeys 是赋值，不是合并）—— 而数据每换一次那个数组都会重算。
  // 不把用户的动作记下来，他刚点开的行就被弹回去了。
  const code = stripComments(VIEW)
  assert.match(code, /rowExpandOverrides/)
  assert.match(code, /@expand-change="onExpandChange"/)
  assert.match(code, /function onExpandChange\(row, expandedRows\)/)
})

t('展开箭头一直都在：没有「对账信息」勾选开关', () => {
  // 用户原话：「去掉勾选对账信息的开关」。
  // 曾经过去是「勾上『对账信息』才出现行首箭头」—— 那样「不看对账信息的人」
  // 和「要改对账信息的人」看到的是**两张不同的表**。
  assert.doesNotMatch(VIEW, /showExtra/, '还有 showExtra 这个开关状态')
  assert.doesNotMatch(VIEW, /对账信息<\/el-checkbox>/, '「对账信息」勾选开关还在')
  assert.doesNotMatch(VIEW, /class="flow-toggle"/, '「对账信息」勾选开关还在')
  // 展开列**没有** v-if —— 箭头常在，行默认展开
  assert.match(VIEW, /<el-table-column type="expand"/)
})

console.log('== 忽略 / 恢复：点完忽略还要点得回来 ==')

t('「忽略」之后「恢复」必须还能点', () => {
  // 用户原话：「流水明细点击忽略之后怎么没办法点击恢复？」
  //
  // 曾经条件写成 ``row.status !== 'OPEN'``：忽略之后状态变成 IGNORED，
  // 按钮**自己把自己禁用了** —— 文案写着「恢复」，却永远点不动。
  // 判据必须是「状态在这两者之一」。
  assert.equal(canToggleIgnore({ status: 'OPEN' }), true)
  assert.equal(canToggleIgnore({ status: 'IGNORED' }), true)
  // 已生成凭证的行不许改这个状态（后端也这么挡）
  assert.equal(canToggleIgnore({ status: 'POSTED' }), false)
  assert.equal(canToggleIgnore(null), false)
  // 模板里用的就是这个判据，别在 .vue 里另写一份
  assert.match(VIEW, /:disabled="!store\.canWrite \|\| !canToggleIgnore\(row\)"/)
})

console.log('== 已入账 / 被发票占用的流水：对方科目不由流水这边定 ==')

t('已生成凭证的行不能再指定对方科目', () => {
  // 只改流水行、不改凭证，两边就对不上 —— 而且界面上看不出来。
  // 判据与后端同一条（matched_voucher_id），两边各判一次迟早出「能点却被拒」。
  assert.equal(canEditPlanSubject({ matched_voucher_id: null }), true)
  assert.equal(canEditPlanSubject({ matched_voucher_id: 12 }), false)
  assert.equal(canEditPlanSubject(null), false)
  // 界面必须置灰
  assert.match(VIEW, /:disabled="!canEditPlanSubject\(row\)"/)
})

t('被发票占用的行也不能再指定对方科目', () => {
  // 发票那张凭证的分录是**发票自己**算出来的，流水这边只贡献结算账户 ——
  // 流水行上填的对方科目**根本不会进那张凭证**。让用户去填一个不生效的
  // 东西，是骗人。判据与后端同一条（matched_invoice_record_id）。
  assert.equal(canEditPlanSubject({
    matched_voucher_id: null, matched_invoice_record_id: null,
  }), true)
  assert.equal(canEditPlanSubject({
    matched_voucher_id: null, matched_invoice_record_id: 7,
  }), false)
})

t('「已入账定死」那句话与提示框都不再出现在界面上', () => {
  // 用户要求（2026-10-28）：不要那行红字，也不要那个弹出提示。
  // 一行行都挂着一句警告，看着像出了事，其实只是「这一行已经制过证了」。
  //
  // **只认界面上的东西**：这些 .vue 的注释里满是解释，注释里写着这句话
  // 不等于界面上有它 —— 所以先剥掉注释再断言。
  const shown = stripComments(VIEW)
  assert.doesNotMatch(shown, /已入账，科目由凭证定死/, '那句红字还在')
  assert.doesNotMatch(shown, /对方科目由凭证定死/, '那个提示框还在')
  // 提示框整个拆掉了（原来包着按钮的那层 el-tooltip）
  assert.doesNotMatch(shown, /已入账的对方科目由凭证定死/)
})

t('默认就显示的那些列是流水解析出来的内容', () => {
  for (const label of ['日期', '摘要', '用途', '对方户名', '收入', '支出', '余额']) {
    assert.match(VIEW, new RegExp(`<el-table-column label="${label}"`))
  }
})

t('表格用紧凑尺寸', () => {
  assert.match(VIEW, /<el-table[\s\S]*?size="small"[\s\S]*?class="flow-rows-table"/)
})

console.log('== 指定科目时：每个对方科目各有一句凭证摘要 ==')

t('人工摘要取自对方科目那一项，不是整张凭证', () => {
  assert.equal(planSubjectSummary({ summary_override: '备用金' }), '备用金')
  assert.equal(planSubjectSummary({ summary_override: '' }), '')
  assert.equal(planSubjectSummary({}), '')
  assert.equal(planSubjectSummary(null), '')
})

t('弹窗里每个科目一个摘要框，默认带出规则算出来的那句', () => {
  const view = stripComments(VIEW)
  // 摘要框绑在**每一项**上（v-for 里的 item），不是一个框管全部
  assert.match(view, /v-model="item\.summary"/)
  // 打开弹窗时把「按规则算出来的那句」填进去 —— 人工写过的优先
  assert.match(view, /summary: planSubjectSummary\(item\) \|\| defaultSummary/)
  // 留空 = 交回规则
  assert.match(view, /留空 = 按规则\/模板自动生成/)
  // 「人工」标记看的是**当前值与默认值是否不同** —— 不是「动过没有」。
  // 看「动过没有」的话，**已经存下来的人工摘要重新打开时反而不显示标记**，
  // 用户以为它没生效。
  assert.match(view, /v-if="item\.summary && item\.summary !== item\.defaultSummary"/)
})

t('界面上不再有「整张凭证一句摘要」的那个框', () => {
  const view = stripComments(VIEW)
  // **顶部的那个框已经去掉了** —— 它管的是整张凭证所有分录，
  // 而摘要的粒度是对方科目。
  assert.doesNotMatch(view, /planDialog\.summary/)
  assert.doesNotMatch(view, /row\.summary_override/)
})

t('没动过摘要框就不带 summary —— 那些科目要继续跟着规则走', () => {
  // 不动的科目如果也被存下来，规则改了之后它们就不跟着变了 ——
  // 而用户根本没碰过它们。
  assert.match(VIEW, /summaryTouched/)
  assert.match(VIEW, /if \(item\.summaryTouched\)/)
  assert.match(VIEW, /entry\.summary = \(item\.summary \|\| ''\)\.trim\(\)/)
})

t('流水明细页上不显示「凭证摘要」', () => {
  // 用户要求：摘要只在「对方科目」弹窗里设，页面上不摆。
  // 断言的是**剥掉注释后的模板** —— 注释里提到某个词不等于界面上有它。
  const view = stripComments(VIEW)
  const expanded = view.slice(view.indexOf('type="expand"'), view.indexOf('</el-table>'))
  assert.doesNotMatch(expanded, /凭证摘要/)
})

t('换了科目，摘要框回到默认 —— 显示的与存下来的一致', () => {
  // 摘要**跟着科目走**：后端「同一个科目上那句才跟着走，换了科目就不带走」。
  // 框里若还留着上一个科目的话，界面上显示着、存的时候却不生效 ——
  // 「显示的没生效」比「没显示」更坏。
  assert.match(VIEW, /@change="onPlanSubjectChange\(item\)"/)
  assert.match(VIEW, /function onPlanSubjectChange\(item\) \{/)
  assert.match(VIEW, /item\.summary = item\.defaultSummary/)
  assert.match(VIEW, /item\.summaryTouched = false/)
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

console.log('== 摘要模板：预览的口径必须与后端一致 ==')

// 前端的预览要显示「这条流水最后会长成什么样」—— 预览与真生成的摘要对不上，
// 那预览就等于没有预览。而这两处是两套代码（JS 与 Python），所以**共用一份
// 基准文件**：各自钉一条用例读它，口径一漂移至少有一边红。
const RENDER_CASES = JSON.parse(fs.readFileSync(
  new URL('../../backend/tests/fixtures/summary_template_render_cases.json', import.meta.url),
  'utf8'))

t('渲染口径与后端共用同一份基准', () => {
  assert.ok(RENDER_CASES.cases.length > 0, '基准文件是空的 —— 那这条用例等于没跑')
  for (const c of RENDER_CASES.cases) {
    const out = renderSummary(c.template, c.row)
    assert.equal(out, c.expected, `${c.name}：预览渲染出 ${out}，基准写的是 ${c.expected}`)
  }
})

t('预览拿不到那一列的值时，取空而不是 "undefined"', () => {
  const out = renderSummary('{用途}-{对方户名}', {
    seq: 1, tx_date: '2026-09-14', direction: 'IN', amount: '10.00',
    summary: '转入', purpose: '', counterparty_name: '',
  })
  assert.equal(out, '-', `空值不该渲染成 undefined/null：${out}`)
})

t('不认识的占位符原样留着（正在输入的人要看得出打错了）', () => {
  const out = renderSummary('{用途}-{摘}', {
    seq: 1, tx_date: '2026-09-14', direction: 'IN', amount: '10.00',
    summary: '转入', purpose: '房租', counterparty_name: '',
  })
  assert.match(out, /\{摘\}/, `不认识的占位符该原样留着：${out}`)
})

t('有现成的示例模板可照抄', () => {
  assert.ok(SUMMARY_TEMPLATE_EXAMPLES.length >= 3, '示例太少，用户没得抄')
  for (const ex of SUMMARY_TEMPLATE_EXAMPLES) {
    assert.ok(ex.label && ex.template, `示例要既有名字又有模板：${JSON.stringify(ex)}`)
    assert.deepEqual(unknownTemplateFields(ex.template), [],
      `示例里的占位符必须都是认识的：${ex.template}`)
  }
})

t('预览要摆出那一行的字段值（否则用户会把摘要误当成用途）', () => {
  const text = summaryTemplateValuesText({
    seq: 1, tx_date: '2026-09-28', direction: 'IN', amount: '872100.00',
    summary: '杨育军', purpose: '', counterparty_name: '',
  })
  assert.match(text, /摘要=杨育军/, `字段值没摆出来：${text}`)
  assert.match(text, /用途=（空）/, `空值要标出来：${text}`)
  assert.match(text, /金额=872100\.00/, `金额没摆出来：${text}`)
})

t('页面上有占位符按钮、示例与预览', () => {
  assert.match(VIEW, /insertPlaceholder/)
  assert.match(VIEW, /SUMMARY_TEMPLATE_EXAMPLES/)
  // **只有规则级模板了**（账套级已下线）—— 预览也只有那一个
  assert.match(VIEW, /rulePreviewText/)
  assert.match(VIEW, /renderSummary/)
  assert.doesNotMatch(VIEW, /configPreviewText/, '账套级模板的预览还在')
})

t('已保存的规则不再逐行给预览；新增规则时预览一下就够', () => {
  // 预览是**写模板时**才需要的东西：模板长什么样，得当场看着一行流水才知道。
  // 规则存下来之后用途在凭证上，一行一格铺开预览只会把表格撑得又高又乱。
  //
  // **只认界面上的东西**：这些 .vue 的注释里满是解释（「预览是写模板时才需要…」），
  // 注释里写着「预览」不等于界面上有预览 —— 所以先剥掉注释再断言。
  const block = stripComments(rulesTableBlock())
  assert.doesNotMatch(block, /renderSummary\(/, '规则表里还在逐行渲染预览')
  assert.doesNotMatch(block, /预览/, '规则表里还有预览')
  // 但**新增规则**那块的预览必须还在（rulePreviewText）
  assert.match(VIEW, /rulePreviewText/)
  assert.doesNotMatch(VIEW, /configPreviewText/, '账套级模板的预览还在')
})

console.log('== 页签的名字 ==')

t('页签叫「科目规则」，不是「流水科目规则」', () => {
  // 页签本身就叫「流水对账」，前面再缀一个「流水」是废话。
  assert.match(VIEW, /<el-tab-pane label="科目规则" name="rules">/)
  assert.doesNotMatch(VIEW, /流水科目规则/, '界面里还有「流水科目规则」')
})

console.log('== 存疑凭证：账上有、对账单里找不到对应流水的那些凭证 ==')

t('流水对账有第 4 个页签「存疑凭证」', () => {
  // 这是流水对账的**另一半**：流水明细页那条路是「对账单上有、账上没有」
  // → 生成凭证；这一路是「账上有、对账单上没有」→ 那是错账。
  assert.match(VIEW, /<el-tab-pane label="存疑凭证" name="suspicious">/)
  // **页签顺序**：对账单 → 流水明细 → 科目规则 → 存疑凭证
  const labels = [...VIEW.matchAll(/<el-tab-pane label="([^"]+)"/g)].map((m) => m[1])
  assert.deepEqual(labels, ['对账单', '流水明细', '科目规则', '存疑凭证'])
})

t('存疑凭证按对账单分组，摊平成一张表时带上账户与期间', () => {
  const groups = [{
    account_code: '100202',
    account_name: '农商银行青年支行',
    period_text: '2026-09-01 ~ 2026-09-24',
    vouchers: [
      { voucher_id: 1, voucher_no: '记0015', reason: '金额对不上' },
      { voucher_id: 2, voucher_no: '记0016', reason: '找不到对应流水' },
    ],
  }]
  const rows = suspiciousRows(groups)
  assert.equal(rows.length, 2)
  // **每行都要带上「哪个账户、哪段期间」** —— 离开它就说不清凭什么可疑
  assert.equal(rows[0].account_code, '100202')
  assert.equal(rows[0].period_text, '2026-09-01 ~ 2026-09-24')
  assert.equal(rows[0].voucher_no, '记0015')
  assert.equal(rows[1].voucher_no, '记0016')
})

t('摊平函数对空输入不炸', () => {
  assert.deepEqual(suspiciousRows(null), [])
  assert.deepEqual(suspiciousRows([]), [])
  assert.deepEqual(suspiciousRows([{ vouchers: null }]), [])
})

t('每一行只说结论：说清「对账单里没找到这笔流水」，不摆近似解释的线索', () => {
  // **只说结论**（2026-10-28 用户定的口径）：按金额没匹配到就是存疑。
  // 以前界面写「2026-09-15 前后有一笔流水，金额是 8588.00，对不上」——
  // 那是拿「日期附近有个流水」近似解释一笔根本对不上的账，读起来像
  // 「差一点就匹配上了」，会让人以为只是日期差了几天，去改日期而不是改金额。
  assert.match(VIEW, /\{\{ row\.reason \}\}/)
  // 线索**不上界面**（后端仍然算、仍然进 API，给排查用）
  assert.doesNotMatch(VIEW, /row\.clues/, '界面上还在渲染线索')
  assert.doesNotMatch(VIEW, /流水第 \{\{ clue\.seq \}\} 行/, '界面上还在摆流水行线索')
})

t('存疑凭证表没有「对账单」这一列，账户与期间挪到「为什么可疑」格里', () => {
  // 用户批注：这一列不显示。但「这笔钱记在哪个账户、属于哪份对账单」
  // 是判断存疑的前提 —— 挪进「为什么可疑」格，不占一列也不丢信息。
  const suspiciousPane = VIEW.slice(VIEW.indexOf('name="suspicious"'))
  assert.doesNotMatch(suspiciousPane, /<el-table-column label="对账单"/,
    '存疑凭证表里还有「对账单」列')
  assert.match(suspiciousPane, /row\.account_code \}\} \{\{ row\.account_name \}\}/)
  assert.match(suspiciousPane, /row\.period_text/)
  // **账户一行、期间一行**（用户批注）：挤在一行时这一格会自己折行，
  // 读起来「账户…· 期间」黏在一起。
  assert.doesNotMatch(suspiciousPane, /row\.account_name \}\} · \{\{ row\.period_text \}\}/,
    '账户与期间还挤在同一行')
})

t('页面顶部那段流水对账说明已经去掉', () => {
  // 用户批注：去掉这段提示。
  assert.doesNotMatch(VIEW, /上传网银导出的对账单，把它绑到具体的现金科目/,
    '顶部说明还在')
})

t('顶部统计说清三件事：几张存疑 / 邻期未判定 / 内部转账', () => {
  // 邻期那笔**不判定**，但要报个数 —— 用户看得见「那几个月还有几笔没说法」，
  // 而不是以为它们不存在。
  assert.match(VIEW, /suspiciousTotal/)
  assert.match(VIEW, /suspiciousNearCount/)
  assert.match(VIEW, /suspiciousInternalCount/)
  assert.match(VIEW, /邻期分录未判定/)
  assert.match(VIEW, /内部转账/)
})

t('「修改凭证」跳到那张凭证去改', () => {
  // 这一页只负责**指出问题**，不负责「改」—— 错账的改法各不相同，
  // 写死一种改法只会改错。
  // 按钮文案：2026-10-28 用户批注，「去改这张凭证」缩成「修改凭证」。
  assert.match(VIEW, /修改凭证/)
  assert.doesNotMatch(VIEW, /去改这张凭证/, '按钮文案还是旧的')
  assert.match(VIEW, /openVoucher\(row\.voucher_id\)/)
  assert.match(VIEW, /name: 'voucher-edit'/)
})

t('存疑凭证那一页是懒加载的', () => {
  // 它要扫一遍账上所有现金分录，而用户未必会去看它 —— 每次进来都查一遍
  // 是白查。
  assert.match(VIEW, /@tab-change="onTabChange"/)
  assert.match(VIEW, /if \(name === 'suspicious'\) loadSuspicious\(\)/)
  assert.match(VIEW, /api\.getSuspiciousVouchers/)
})

console.log('== 对账单跟着顶栏的会计期间走 ==')

t('按顶栏选中的期间过滤对账单（交集口径）', () => {
  const list = [
    { id: 1, period_start: '2026-09-01', period_end: '2026-09-24' },  // 9 月内
    { id: 2, period_start: '2026-09-28', period_end: '2026-10-03' },  // 跨月
    { id: 3, period_start: '2026-08-01', period_end: '2026-08-31' },  // 8 月
    { id: 4, period_start: null, period_end: null },                  // 老数据
  ]
  // 9 月：1 号在、3 号（8 月）不在、2 号跨月 → **在**（交集口径）
  assert.deepEqual(statementsInPeriod(list, 2026, 9).map((s) => s.id), [1, 2, 4])
  // 10 月：2 号跨月 → 在
  assert.deepEqual(statementsInPeriod(list, 2026, 10).map((s) => s.id), [2, 4])
  // 8 月
  assert.deepEqual(statementsInPeriod(list, 2026, 8).map((s) => s.id), [3, 4])
  // **期间没定下来时不过滤** —— 那不是「这个月没有对账单」，
  // 是「还不知道要看哪个月」，拿它当空列表会把用户的东西藏起来。
  assert.equal(statementsInPeriod(list, null, null).length, 4)
  assert.equal(statementsInPeriod(list, 2026, null).length, 4)
  assert.deepEqual(statementsInPeriod(null, 2026, 9), [])
})

t('别的期间还有哪些对账单（空态里给一条路）', () => {
  const list = [
    { id: 1, period_start: '2026-09-01', period_end: '2026-09-24' },
    { id: 2, period_start: '2026-09-28', period_end: '2026-10-03' },
    { id: 3, period_start: '2026-08-01', period_end: '2026-08-31' },
  ]
  // 停在 2 月 → 9 月那两份（period_start 都在 9 月）算「别的期间」，
  // 8 月那份算另一组 —— **按起始日归组**，跨月的那份归到它起始的那个月。
  const others = statementsOtherPeriods(list, 2026, 2)
  assert.equal(others.length, 2)
  assert.deepEqual(others.map((o) => o.label), ['2026 年 8 月', '2026 年 9 月'])
  assert.equal(others[1].count, 2)
  // 停在 9 月 → 8 月那份算「别的期间」
  assert.deepEqual(
    statementsOtherPeriods(list, 2026, 9).map((o) => o.label), ['2026 年 8 月'])
  // 没有别的期间时是空的（不必显示那一行）
  assert.deepEqual(statementsOtherPeriods(
    [{ id: 3, period_start: '2026-08-01', period_end: '2026-08-31' }], 2026, 8), [])
})

t('页面上对账单列表用的是按期间过滤后的那份', () => {
  // 用户批注：「根据这里选中的期间，来显示对账单」。
  // 表格与流水明细页签的**选择器**都要过滤 —— 只过滤表格的话，
  // 下拉里还是能选到别的月份的对账单，等于没过滤。
  assert.match(VIEW, /visibleStatements/)
  assert.match(VIEW, /statementsInPeriod\(/)
  assert.match(VIEW, /:data="visibleStatements"/)
  assert.match(VIEW, /v-for="s in visibleStatements"/)
  // 期间换了要跟着换选中项
  assert.match(VIEW, /watch\(visibleStatements/)
  // 空态要分两种说法
  assert.match(VIEW, /这个期间还没有对账单/)
  // 上传后不在当前期间 → 切过去（否则刚上传就「消失」）
  assert.match(VIEW, /switchToStatementPeriod/)
})

console.log('== 可绑定的账户：只有 1002 银行存款 ==')

t('1001 库存现金与 1012 第三方支付备付金都不给选', () => {
  // 用户口径（2026-10-28）：先说「一定要排除 1012」「我只会用到 1001 和
  // 1002」，随后又说「1001 没有对账单，可以把 1001 也排除」——
  // 所以这一条线只认 **1002 银行存款**。
  //
  // **与后端同一个判据**（backend/app/bankstatement/caliber.py）：
  // 后端也按同一份口径拒，两边各判一次的话，迟早出现
  // 「界面选不了、接口却收下」。
  const subjects = [
    { id: 1, code: '1001', name: '库存现金' },
    { id: 2, code: '1002', name: '银行存款' },
    { id: 3, code: '100201', name: '基本户' },
    { id: 4, code: '1012', name: '其他货币资金' },
    { id: 5, code: '101201', name: '支付宝' },
    { id: 6, code: '1122', name: '应收账款' },
  ]
  assert.deepEqual(bankAccountSubjects(subjects).map((s) => s.code),
    ['1002', '100201'])
  assert.deepEqual(bankAccountSubjects(null), [])
  // 页面上用的就是这份名单
  assert.match(VIEW, /bankAccountSubjects\(subjects\.value\)/)
})

console.log('== 流水对账页的表头与文案（2026-10-28 逐条批注） ==')

t('对账单台账没有「账户」列也没有「期间」列，都挪到文件列里', () => {
  // 用户批注：「这一列去掉」（账户）、「期间不要单独一列显示了，弄到文件列去」。
  // 但「这份对账单绑在哪个账户上、属于哪段期间」是判断这堆流水的前提 ——
  // 挪到文件名下面，不占列也不丢信息。
  const table = VIEW.slice(VIEW.indexOf('对账单台账'))
  assert.doesNotMatch(table, /<el-table-column label="账户"/, '台账里还有「账户」列')
  assert.doesNotMatch(table, /<el-table-column label="期间"/, '台账里还有「期间」列')
  assert.match(table, /row\.account_subject_code \}\} \{\{ row\.account_subject_name \}\}/)
  // 户名（从银行对账单解析出来的）也不能丢
  assert.match(table, /row\.account_name \|\| row\.account_no/)
  assert.match(table, /row\.period_start \|\| '—' \}\} ~ \{\{ row\.period_end/)
  // 文件列第二行换成的是**期间**（原来那一行是「PDF 对账单 · 6 行」）
  assert.doesNotMatch(table, /row\.container_label \}\} · \{\{ row\.row_count \}\} 行/,
    '文件列里还在显示容器类型与行数')
})

t('流水明细页签那段说明去掉了', () => {
  // 用户批注：去掉这个。
  assert.doesNotMatch(VIEW, /嫌占地方就用行首箭头收起/,
    '流水明细那段说明还在')
})

t('摘要模板输入框的提示写的是「留空使用默认规则」', () => {
  // 用户批注：提示改成「留空使用默认规则」。两处都要改：新增规则表单里
  // 那一个、规则表格每行那一个。
  const n = (VIEW.match(/placeholder="留空使用默认规则"/g) || []).length
  assert.equal(n, 2, `占位符应该有两处，实际 ${n} 处`)
  assert.doesNotMatch(VIEW, /留空 = 用内置口径/, '旧文案还在')
})

t('台账空态用插槽显示动态文案', () => {
  // 「还没上传过」与「这个期间还没有」是**两句话**，不能写死。
  // 后面那个还要把**是哪个期间**写出来 —— 顶栏停在 2 月、账上只有 9 月的
  // 对账单，页面空着；不说清楚用户只会以为功能坏了。
  assert.match(VIEW, /<template #empty>/)
  assert.match(VIEW, /statementsEmptyText/)
  assert.match(VIEW, /还没有上传过对账单/)
  assert.match(VIEW, /月还没有对账单/)
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

t('页面上不再有账套级摘要模板（2026-10-28 下线）', () => {
  // 用户原话：「去掉账套级摘要模板的设置，完全使用内置口径就可以了，
  // 不要让用户更改」。
  //
  // **断言的是剥掉注释后的模板** —— 注释里提到某个词不等于界面上有它。
  const view = stripComments(VIEW)
  assert.doesNotMatch(view, /凭证摘要模板（账套级）/, '账套级模板那块还在')
  assert.doesNotMatch(view, /configForm/, '账套级模板的表单还在')
  assert.doesNotMatch(view, /saveConfigForm/, '账套级模板的保存还在')
  // **它也不能还在发那个请求** —— 后端会回 400（那个字段已经下线）。
  assert.doesNotMatch(view, /api\.saveConfig/, '还在发账套级配置')
  assert.doesNotMatch(view, /api\.getConfig/, '还在拉账套级配置')
})

t('规则级摘要模板留着（那才是「这一类流水用另一种说法」）', () => {
  assert.match(VIEW, /saveRuleTemplate/)
  assert.match(VIEW, /ruleForm\.summary_template/)
  assert.match(VIEW, /SUMMARY_TEMPLATE_EXAMPLES/)
  // **占位符插到规则级模板里** —— 只有一个去处了，不必再问「插到哪一份」
  assert.match(VIEW, /insertPlaceholder\(ruleForm\.summary_template, field\)/)
  assert.doesNotMatch(VIEW, /insertPlaceholder\([^)]*,\s*'config'\)/)
})

t('规则级模板的说明写的是「盖过内置口径」', () => {
  // 「盖过账套级」已经不对了 —— 账套级没有了
  assert.doesNotMatch(VIEW, /盖过账套级/)
  assert.doesNotMatch(VIEW, /用账套级/)
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
