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
 * 「忽略 / 恢复」这一颗按钮**能不能点**。
 *
 * 只有**没生成凭证**的行能改这个状态 —— 后端也这么挡
 * （``app/bankstatement/service.py::update_row``：``matched_voucher_id`` 存在就拒）。
 *
 * **这里曾经写错过一次**，而且错得最隐蔽：条件写成 ``row.status !== 'OPEN'``。
 * 于是「忽略」之后状态变成 ``IGNORED``，按钮**自己把自己禁用了** ——
 * 文案明明写着「恢复」，却永远点不动。用户原话：
 * 「流水明细点击忽略之后怎么没办法点击恢复？」
 *
 * 判据必须是「**状态在这两者之一**」，不是「状态等于某一个」。
 *
 * @param {object|null} row 流水行
 * @returns {boolean}
 */
export const canToggleIgnore = (row) =>
  !!row && (row.status === 'OPEN' || row.status === 'IGNORED')

/**
 * 「指定 / 调整对方科目」这颗按钮**能不能点**。
 *
 * **已经生成过凭证的行不行。** 凭证已经把那个科目定死了（分录里写着），
 * 只改流水行、不改凭证，两边就对不上 —— 而且界面上看不出来。
 * 改科目是**改凭证**的动作：要么去凭证里改，要么用「可改科目」
 * （它走 ``accounting.vouchers.update_voucher``，凭证与流水一起改）。
 *
 * **被发票占用的行也不行。** 发票那张凭证的分录是**发票自己**算出来的
 * （``app/invoice/service.py`` 用的是预览时算出来的 ``resolution.entries``），
 * 流水这一侧只贡献**结算账户**。所以流水行上填的对方科目**根本不会进那张
 * 凭证** —— 让用户去填一个不生效的东西，是骗人；填了还会以为「就这么记了」。
 *
 * **判据必须与后端同一条**：后端那道闸看的是 ``matched_voucher_id`` 与
 * ``matched_invoice_record_id``（``app/bankstatement/service.py::update_row``），
 * 这里也看它们 —— 两边各判一次的话，迟早出现「界面能点、点了被拒」或
 * 反过来的毛病。
 *
 * @param {object|null} row 流水行
 * @returns {boolean}
 */
export const canEditPlanSubject = (row) =>
  !!row && !row.matched_voucher_id && !row.matched_invoice_record_id

/**
 * **默认展开**的行 id。
 *
 * **摊开的是「还有活要干的行」** —— 方向未定、对方科目待确认、还没入账。
 * **已入账的、被发票占用的默认收起**：那些行的对账信息看一眼就够，
 * 全摊开反而把该处理的那几行淹在一堆「已经办完了」的行里。
 *
 * **判据就是「对方科目还能不能改」**（{@link canEditPlanSubject}）——
 * 还能改 = 还有活；不能改 = 账上（或发票上）已经有定论了。
 *
 * @param {Array} rows 流水行
 * @returns {number[]} 要展开的行 id
 */
export const defaultExpandedRowIds = (rows) =>
  (rows || []).filter((row) => canEditPlanSubject(row)).map((row) => row.id)

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
 * 这一条对方科目的**人工摘要**（没写过就是空串）。
 *
 * **人工摘要挂在对方科目上，不是挂在流水行上。** 一张凭证有 N+1 条分录，
 * 每条分录的摘要本来就是各自独立的字段 —— 「这笔记到 6602、摘要写
 * 『备用金』」与「同一笔记到 6601、摘要写『收取押金』」是两个科目的事，
 * 一句话管不住。
 *
 * @param {object|null} item 对方科目（``plan_subjects`` 里的一项）
 * @returns {string}
 */
export const planSubjectSummary = (item) => (item?.summary_override || '').trim()

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

/**
 * **存疑凭证**：账上有、对账单里找不到对应流水的那些凭证。
 *
 * 后端按**对账单**分组返回（一份对账单 = 一个账户 + 一段期间，期间就是
 * 判定的那把尺子）。界面上把它们摊平成一张表、每行带上账户与期间 ——
 * 那样一眼能扫完，又不会把「哪个账户」丢在折叠层里。
 *
 * @param {Array} groups 后端返回的分组
 * @returns {Array} 摊平后的行
 */
export const suspiciousRows = (groups) => {
  const out = []
  for (const group of groups || []) {
    for (const voucher of group?.vouchers || []) {
      out.push({
        ...voucher,
        account_code: group.account_code,
        account_name: group.account_name,
        period_text: group.period_text,
      })
    }
  }
  return out
}

/**
 * 一份对账单是否与某个会计期间**有交集**。
 *
 * 抽出来是因为两处要用它，而且**必须用同一个判据**：一份对账单能不能在
 * 顶栏那个月看到（``statementsInPeriod``），与它算不算「别的期间的那些」
 * （``statementsOtherPeriods``）—— 两边判得不一样，界面上就会出现
 * 「空态说别的期间有，点进去却看不到」的鬼故事。
 *
 * @param {object} s 对账单
 * @param {number} year 年
 * @param {number} month 月（1-12）
 * @returns {boolean}
 */
const overlapsPeriod = (s, year, month) => {
  if (!year || !month) return true
  if (!s?.period_start || !s?.period_end) return true
  const monthStart = Date.UTC(year, month - 1, 1)
  const monthEnd = Date.UTC(year, month, 0, 23, 59, 59, 999)
  const start = Date.parse(`${s.period_start}T00:00:00Z`)
  const end = Date.parse(`${s.period_end}T23:59:59Z`)
  if (Number.isNaN(start) || Number.isNaN(end)) return true
  return end >= monthStart && start <= monthEnd
}

/**
 * **可绑定的账户**：只有 1002 银行存款（含它的明细科目）。
 *
 * **与后端同一个判据**（`backend/app/bankstatement/caliber.py`）：
 * 1001 库存现金（抽屉里的现金）与 1012 其他货币资金（支付宝/微信的备付金）
 * 都不是银行账户 —— 网银导出的对账单对不上它们。后端也按同一份口径拒，
 * 两边各判一次的话，迟早出现「界面选不了、接口却收下」。
 *
 * 抽成纯函数是为了能直接测 —— 这个名单写死在 .vue 里就只能靠正则去猜。
 *
 * @param {Array} subjects 账套的科目表
 * @returns {Array} 可绑定的那些
 */
export const bankAccountSubjects = (subjects) => (subjects || []).filter(
  (s) => String(s?.code || '').startsWith('1002'))

/**
 * **按顶栏选中的会计期间过滤对账单**（2026-10-28 用户批注：
 * 「根据这里选中的期间，来显示对账单」）。
 *
 * 口径：**与该期间有交集**就算在里面。
 *
 * 为什么用「交集」而不是「起始日落在那个月」：银行对账单不一定卡在月初到
 * 月末 —— 有的到 09-24（月末前就出了），有的跨月（09-28 ~ 10-03）。
 * 按「起始日在哪个月」判，后一种会被判成「10 月的对账单」而被 9 月藏起来；
 * 按交集判，两种都落在它们该在的地方。
 *
 * **期间没定下来时不过滤**（`year`/`month` 缺一不可）：那不是「这个月没有
 * 对账单」，那是「还不知道要看哪个月」—— 拿它当空列表会把用户的东西藏起来。
 *
 * **没有期间的老数据不藏**：一份对账单没有 period_start/period_end，说明
 * 它是解析不出期间来的老东西，藏起来等于凭空消失。宁可多显示一份，
 * 也别让用户以为没上传过。
 *
 * @param {Array} statements 后端返回的全部对账单
 * @param {number} year 顶栏选中的年
 * @param {number} month 顶栏选中的月（1-12）
 * @returns {Array} 落在该期间里的对账单
 */
export const statementsInPeriod = (statements, year, month) => {
  const list = statements || []
  if (!year || !month) return list
  return list.filter((s) => overlapsPeriod(s, year, month))
}

/**
 * **别的期间还有哪些对账单**（按期间归并）。
 *
 * 为什么要有它：顶栏的期间是**会计期间**（账套开了哪些月），而对账单是
 * 哪个月从文件里解析出来的。生产实测就是这个错位 —— 顶栏停在 2026 年
 * 2 月，账上的对账单是 9 月的。这时候页面空着，用户只会以为功能坏了。
 *
 * 所以空态里要说清楚：**不是没有对账单，是不在这个月**，并且给一条路
 * 直接切过去。
 *
 * @param {Array} statements 全部对账单
 * @param {number} year 顶栏选中的年
 * @param {number} month 顶栏选中的月
 * @returns {Array} [{ year, month, label, count }]，按期间从早到晚排
 */
export const statementsOtherPeriods = (statements, year, month) => {
  const groups = new Map()
  for (const s of statements || []) {
    if (!s?.period_start) continue
    if (overlapsPeriod(s, year, month)) continue
    const key = String(s.period_start).slice(0, 7)
    if (!groups.has(key)) {
      const [y, m] = key.split('-').map(Number)
      groups.set(key, { year: y, month: m, label: `${y} 年 ${m} 月`, count: 0 })
    }
    groups.get(key).count += 1
  }
  return [...groups.values()].sort(
    (a, b) => a.year - b.year || a.month - b.month)
}
