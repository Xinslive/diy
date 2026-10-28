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
  assert.match(VIEW, /configPreviewText/)
  assert.match(VIEW, /rulePreviewText/)
  assert.match(VIEW, /renderSummary/)
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
  assert.match(VIEW, /configPreviewText/)
})

console.log('== 页签的名字 ==')

t('页签叫「科目规则」，不是「流水科目规则」', () => {
  // 页签本身就叫「流水对账」，前面再缀一个「流水」是废话。
  assert.match(VIEW, /<el-tab-pane label="科目规则" name="rules">/)
  assert.doesNotMatch(VIEW, /流水科目规则/, '界面里还有「流水科目规则」')
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
