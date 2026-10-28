/**
 * 流水对账页的**纯逻辑**：状态文案、来源徽标、按钮可用性。
 *
 * 单独抽出来是为了能直接测 —— 这些判断（尤其「什么时候才让生成凭证」）
 * 是这个页面的业务核心，藏在 .vue 里就只能靠正则去猜它对不对。
 *
 * 与后端的口径一一对应：
 *   · 方向 IN=收入 / OUT=支出（**与会计的借贷相反**，见 bankstatement 的说明）；
 *   · 结算科目来源 MANUAL / STATEMENT / CONFIG；
 *   · 流水状态 OPEN / POSTED / IGNORED 与 plan_status NONE/RULE/MANUAL/PENDING。
 */

/** 人工指定列时能选的字段。跟后端 ``semantics.FIELDS`` 一一对应。 */
export const FIELD_OPTIONS = [
  { value: 'DATE', label: '日期' },
  { value: 'TIME', label: '时间' },
  { value: 'IN', label: '收入' },
  { value: 'OUT', label: '支出' },
  { value: 'AMOUNT', label: '金额' },
  { value: 'BALANCE', label: '余额' },
  { value: 'DIRECTION', label: '方向' },
  { value: 'CP_NAME', label: '对方户名' },
  { value: 'CP_ACCOUNT', label: '对方账号' },
  { value: 'SUMMARY', label: '摘要' },
  { value: 'PURPOSE', label: '用途' },
  { value: 'TX_TYPE', label: '交易类型' },
  { value: 'EXTRA', label: '其他' },
]

const FIELD_LABELS = Object.fromEntries(FIELD_OPTIONS.map((f) => [f.value, f.label]))

export const fieldLabel = (f) => FIELD_LABELS[f] || f

/** 结算科目的来源文案。 */
export const SETTLEMENT_SOURCE_TEXT = {
  MANUAL: '人工指定',
  STATEMENT: '对账单识别',
  CONFIG: '配置科目',
}

export const SETTLEMENT_SOURCE_TYPE = {
  MANUAL: 'info',
  STATEMENT: 'success',
  CONFIG: 'warning',
}

export const settlementSourceText = (source) => SETTLEMENT_SOURCE_TEXT[source] || source || ''

export const settlementSourceType = (source) => SETTLEMENT_SOURCE_TYPE[source] || 'info'

/** 流水行的处理状态文案。 */
export const VOUCHER_STATE_TEXT = {
  READY: '可制证',
  PENDING: '待确认科目',
  UNKNOWN: '方向未知',
  NOAMOUNT: '金额为 0',
  POSTED: '已入账',
  INVOICE: '已被发票占用',
  IGNORED: '已忽略',
  NONE: '待处理',
}

export const VOUCHER_STATE_TYPE = {
  READY: 'success',
  PENDING: 'warning',
  UNKNOWN: 'danger',
  NOAMOUNT: 'info',
  POSTED: 'success',
  INVOICE: 'info',
  IGNORED: 'info',
  NONE: 'info',
}

export const voucherStateText = (state) => VOUCHER_STATE_TEXT[state] || state || '—'

export const voucherStateType = (state) => VOUCHER_STATE_TYPE[state] || 'info'

/**
 * 「生成流水凭证」这个按钮**能不能点**。
 *
 * 判据是后端给的候选清单 —— 后端已经替我们判过「方向已知、金额大于 0、
 * 对方科目已定、没被发票占用、没有已入账的凭证」。前端只照着它显示，
 * **不自己再判一遍**：两边各判一次的话，迟早会出现「按钮能点、点了没反应」
 * 或者「按钮点不动、其实能生成」这两种最难查的毛病。
 *
 * @param {object|null} plan 后端 ``POST /plan-vouchers`` 的返回
 * @returns {boolean}
 */
export const canCommit = (plan) => !!plan && Number(plan.candidate_count || 0) > 0

/**
 * 余额差额的着色。
 *
 * 三档：对上了（绿）、能被未入账流水解释（黄）、原因待查（红）。
 * 「原因待查」是红的 —— 因为那意味着**账上少了一笔或者多了一笔**，
 * 而用户还不知道是哪一笔。
 */
export const diffClass = (reconcile) => {
  if (!reconcile) return ''
  if (reconcile.verdict === 'MATCH') return 'diff-ok'
  if (reconcile.verdict === 'EXPLAINED') return 'diff-warn'
  return 'diff-bad'
}

/**
 * 手工指定的列映射 → 后端要的 ``{字段: 列号}``。
 *
 * @param {object} mapping ``{列号(字符串): 字段名}``
 * @returns {object} ``{字段名: 列号}``
 */
export const toColumnMapping = (mapping) => {
  const out = {}
  for (const [index, field] of Object.entries(mapping || {})) {
    if (field) out[field] = Number(index)
  }
  return out
}

// ----------------------------------------------------------------------
// 对方科目（一行流水可以有好几个）
// ----------------------------------------------------------------------

/**
 * 金额换算成**分**（整数）。
 *
 * **为什么不在这里直接加减浮点。** 金额是两位小数的十进制字符串，
 * `0.1 + 0.2` 在双精度里是 `0.30000000000000004` —— 合计与流水金额
 * 比不相等时，界面会说「对不上」，而实际上它是对的。钱不容许这种误差，
 * 所以先换成整数分再算。
 */
export const toCents = (value) => {
  if (value === '' || value === null || value === undefined) return 0
  const text = String(value).trim()
  if (!text) return 0
  const negative = text.startsWith('-')
  const body = negative ? text.slice(1) : text
  const [intPart = '0', decPart = ''] = body.split('.')
  const cents = Number(intPart || '0') * 100 + Number((decPart + '00').slice(0, 2))
  return negative ? -cents : cents
}

/**
 * 对方科目的金额合计（单位：分）。
 *
 * @param {Array} subjects 后端给的 ``plan_subjects``
 * @returns {number} 合计（分）
 */
export const planSubjectCents = (subjects) =>
  (subjects || []).reduce((sum, item) => sum + toCents(item?.amount), 0)

/**
 * 对方科目合计**是不是等于**流水金额。
 *
 * 差一分钱都不算 —— 凭证会不平衡。
 */
export const planTotalMatches = (subjects, amount) =>
  planSubjectCents(subjects) === toCents(amount)

/**
 * 对方科目合计与流水金额差多少（分）。**0 = 一致**。
 */
export const planTotalDiff = (subjects, amount) =>
  planSubjectCents(subjects) - toCents(amount)

/** 对方科目的显示文本：`6602 业务招待费 800.00`。 */
export const planSubjectText = (item) => {
  if (!item) return ''
  const name = [item.subject_code, item.subject_name].filter(Boolean).join(' ')
  return name ? `${name} ${item.amount}` : String(item.amount ?? '')
}

/** 一个科目的来源说法。 */
export const planSubjectSourceText = (source) =>
  (source === 'RULE' ? '规则命中' : source === 'MANUAL' ? '人工指定' : source || '')

// ----------------------------------------------------------------------
// 改科目（账上已有分录 → 挪到本账户）
// ----------------------------------------------------------------------

/** 有多少行「账上可能已有同一笔、只是记错了账户」。 */
export const reclassifyRowCount = (rows) =>
  (rows || []).filter((row) => (row.reclassify_candidates || []).length > 0).length

/** 候选的显示文本：`记在 1001 库存现金 上的 5,000.00（凭证 记0003）`。 */
export const reclassifyCandidateText = (item) => {
  if (!item) return ''
  const subject = [item.subject_code, item.subject_name].filter(Boolean).join(' ')
  const amount = item.amount ?? ''
  const voucher = item.voucher_no ? `（凭证 ${item.voucher_no}）` : ''
  return `记在 ${subject} 上的 ${amount}${voucher}`
}

/**
 * 这条候选是不是**跨期**的（日期差超出了主匹配窗口）。
 *
 * 跨期的**不是不能改** —— 是要人确认一下：银行流水与会计记账的日期本来
 * 就会差（跨月入账是常事），但差得多了也可能压根不是同一笔钱。
 * 所以它照样列出来，只是标出来、点下去时多问一句。
 *
 * 判断用 `in_window === false` 而不是「没有 in_window 就是跨期」：后端
 * 没给这个字段时（老数据、接口出错）宁可不提示，也不能把正常候选全标成
 * 跨期。
 */
export const isCrossPeriod = (item) => !!item && item.in_window === false

/** 跨期候选的提示文字。窗口内的返回空串。 */
export const reclassifyCrossPeriodText = (item) => {
  if (!isCrossPeriod(item)) return ''
  const days = Number(item.days_diff ?? 0)
  return `日期相差 ${days} 天，请确认是不是同一笔`
}

/** 跨期候选点下去时的二次确认文案。 */
export const reclassifyCrossPeriodConfirm = (item) => {
  const days = Number(item?.days_diff ?? 0)
  return `这笔流水与该凭证的日期相差 ${days} 天。`
    + '日期差得多也可能不是同一笔钱——确认要把它的现金科目改到本账户吗？'
}

// ----------------------------------------------------------------------
// 凭证摘要模板
// ----------------------------------------------------------------------

/** 摘要模板里能用的占位符。**与后端 ``service.SUMMARY_TEMPLATE_FIELDS`` 一一对应。** */
export const SUMMARY_TEMPLATE_FIELDS = [
  '摘要', '用途', '对方户名', '日期', '金额', '方向', '序号',
]

/**
 * 模板里有没有不认识的占位符。
 *
 * **前端也查一遍**，是为了当场告诉用户「这个占位符不认识」——
 * 后端存的时候也会拒，但那时用户已经填完一整张表单了。
 */
export const unknownTemplateFields = (template) => {
  const text = String(template || '')
  const found = []
  const re = /\{([^{}]*)\}/g
  let match
  while ((match = re.exec(text)) !== null) {
    if (!SUMMARY_TEMPLATE_FIELDS.includes(match[1]) && !found.includes(match[1])) {
      found.push(match[1])
    }
  }
  return found
}

/**
 * 模板取值。**与后端 `service.render_summary` 里的取值一一对应**。
 *
 * 值本身按「取到什么就用什么」：对账单上那一列是空的就取空串 —— 模板里的
 * 占位符换成空的，渲染出来就是少一段，而不是冒出个「None」。
 */
export const summaryTemplateValues = (row) => ({
  '摘要': row?.summary || '',
  '用途': row?.purpose || '',
  '对方户名': row?.counterparty_name || '',
  '日期': row?.tx_date || '',
  '金额': row?.amount ?? '',
  '方向': row?.direction === 'IN' ? '收入' : (row?.direction === 'OUT' ? '支出' : ''),
  '序号': row?.seq ?? '',
})

/**
 * 内置口径（没设模板时的样子）：**用途优先，摘要兜底**。
 *
 * 与后端 `service._default_summary` 同口径 —— 预览要让人看到「不设模板
 * 时长什么样」，不然用户没有参照。
 */
export const defaultSummaryText = (row) => {
  if (row?.purpose) return row.purpose
  if (row?.summary) return row.summary
  const who = row?.counterparty_name || '对方'
  return `银行${row?.direction === 'IN' ? '收' : '付'} ${who}`
}

/**
 * 按模板渲染摘要 —— **与后端 `service.render_summary` 同口径**。
 *
 * 口径漂移的后果是「预览里长这样、生成的凭证上长那样」，那就等于没有预览。
 * 所以两边**共用一份基准**（`backend/tests/fixtures/summary_template_render_cases.json`），
 * 各自钉一条用例：口径一改，至少有一边会红。
 *
 * **有一处故意的不同**：模板里出现**不认识的**占位符时，后端是整段渲染失败
 * → 退回内置口径；这里改成**原样留着**（`{摘}` 就显示 `{摘}`）。
 * 因为前端预览是给**正在输入的人**看的 —— 打到一半的 `{摘` 要是直接变成
 * 默认口径，用户会以为模板没生效。而已保存的模板里不会有不认识的占位符
 * （后端存的时候就拒了），所以这个不同**不影响真实生成的摘要**。
 */
export const renderSummary = (template, row) => {
  const text = String(template || '').trim()
  if (!text) return defaultSummaryText(row)
  const values = summaryTemplateValues(row)
  const out = text.replace(/\{([^{}]*)\}/g, (whole, key) =>
    (Object.prototype.hasOwnProperty.call(values, key) ? String(values[key]) : whole))
  const trimmed = out.trim()
  return trimmed || defaultSummaryText(row)
}

/**
 * 现成的示例模板 —— **给用户照抄的**，不用自己想。
 *
 * 一个「看不懂」的模板功能，光列占位符是不够的：用户不知道该拼成什么样。
 * 这几条覆盖了最常见的几种拼法，点一下就填进输入框。
 */
export const SUMMARY_TEMPLATE_EXAMPLES = [
  { label: '只取用途', template: '{用途}' },
  { label: '用途 + 对方', template: '{用途}-{对方户名}' },
  { label: '日期 + 用途', template: '{日期}-{用途}' },
  { label: '日期 + 摘要 + 金额', template: '{日期}-{摘要}-{金额}' },
]

/**
 * 那一行各字段的值 —— **预览要把它一起摆出来**。
 *
 * 为什么：模板渲染出来是空的时候，后端会**退回内置口径**（用途优先、摘要
 * 兜底），预览显示的也就是内置口径那个结果。只给结果的话，用户会看见
 * 「{用途} → 杨育军」，而「杨育军」其实是**摘要** —— 他会以为用途字段是
 * 杨育军。把字段值摆出来，他一眼就看出「用途是空的，所以换了摘要顶上」。
 */
export const summaryTemplateValuesText = (row) => {
  const values = summaryTemplateValues(row)
  return SUMMARY_TEMPLATE_FIELDS
    .map((field) => `${field}=${values[field] === '' ? '（空）' : values[field]}`)
    .join('　')
}
