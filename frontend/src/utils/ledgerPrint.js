/**
 * 账簿（总账 / 明细账）的**打印范围**口径。
 *
 * 总账与明细账是**同一册结构**：一个一级科目一份、连续打成一册，每份
 * 各自从 1 开始编页（后端 `app/ledger/queries.py::detail_ledger_groups`
 * 与 `general_ledger_subjects`）。所以两页的「打印范围」选项**逐字相同** ——
 * 各写一份的话，改了一处忘了另一处，用户就会在总账上看到「一级科目」、
 * 在明细账上看到「末级科目」，以为这两本册子结构不一样。
 *
 * **这个设置现在住在打印预览弹窗里**（用户 2026-10-22 批注 1、2）：
 * 页面工具栏上原来那个「打印范围」下拉已经删掉 —— 同一件事只留一处，
 * 两处都能改的话，迟早有一处是旧值。
 */

/** 「本册」：账套的全部一级科目，连续打成一册。 */
export const PRINT_RANGE_BOOK = 'book'
/** 「仅当前科目」：只打屏幕上正在看的那一个。 */
export const PRINT_RANGE_CURRENT = 'current'

/**
 * 打印范围的下拉选项。总账页与明细账页共用这一份。
 *
 * 「本册」的说法统一成「一级科目」—— 用友的总分类账与明细分类账都是
 * 以一级科目为册页单位（用户 2026-10-22 批注 3）。
 */
export const LEDGER_PRINT_RANGES = [
  { label: '本册（全部一级科目）', value: PRINT_RANGE_BOOK },
  { label: '仅当前科目', value: PRINT_RANGE_CURRENT },
]

/**
 * 「不打印无发生额科目」：**默认打开**（用户 2026-10-22 批注 2）。
 *
 * 整册里几百个一级科目，其中绝大多数在用户拉的那几个月里根本没动过，
 * 全印出来是几百页废纸；而用户点名要打的那一个科目，就算这一段确实没动过，
 * 也**必须**印出来（它得告诉看纸的人「这个科目这段时间是空的」）。
 */
export const SKIP_EMPTY_DEFAULT = true

/**
 * 按打印范围把请求参数拼出来。
 *
 * **「本册」不带科目，「仅当前科目」才带** —— 后端靠这个区分：
 * 给了 ``subject_id`` 就只打那一个科目，不给就是整册。
 *
 * 这一份由**打印预览弹窗**与**页面的「导出 Excel」**共用：两处各拼一遍
 * 的话，纸上是一册、文件里是另一个科目，这种错用户是发现不了的。
 *
 * @param {object} opts
 * @param {string} opts.range 打印范围（PRINT_RANGE_BOOK / PRINT_RANGE_CURRENT）
 * @param {object} [opts.base] 与范围无关的参数（期间、是否含未记账…）
 * @param {object} [opts.subject] 「仅当前科目」时才带的科目参数
 * @param {boolean} [opts.skipEmpty] 不打印无发生额科目（默认打开）
 * @returns {object} 请求参数
 */
export function ledgerPrintParams({ range, base = {}, subject = {}, skipEmpty = SKIP_EMPTY_DEFAULT } = {}) {
  const params = { ...base }
  if (range === PRINT_RANGE_CURRENT) {
    for (const [key, value] of Object.entries(subject || {})) {
      if (value !== undefined && value !== null) params[key] = value
    }
  }
  // 总是显式带上：这个开关默认是**打开**的，不带就等于让后端替用户决定。
  params.skip_empty = !!skipEmpty
  return params
}
