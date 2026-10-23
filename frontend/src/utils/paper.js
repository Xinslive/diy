/**
 * 纸型口径：**整个系统只提供 A4**。
 *
 * 为什么砍到一种：打印纸型最终要落到实物上，而系统里真正会装进打印机的就是
 * A4。列着 A3/B5/A5/连续纸看着齐全，实际是让用户在每次打印前先想一遍
 * 「我这台机器进得了吗」—— 那是选纸型之前就该解决的事，不该在打印对话框里解决。
 * 更关键的是：纸型原先还兼职表达「一页放几张凭证」（A5 = 一页一张），
 * 纸型只剩一种之后这个含义就丢了，所以它独立成模板里的一个字段
 * ``voucher.slots_per_page``（见 backend/app/printing/template.py）。
 *
 * 这里同时收编原先散在设计器和打印弹窗里的两份常量。三处各写一份的结果是：
 * 后端加了选项，前端忘了加，用户看到的列表就少一项，而且没有任何报错。
 */

/** 允许选择的纸型。后端 PAPERS 仍保留其余纸型，老模板照常能取回和渲染。 */
export const PAPER_KEYS = ['A4']

/**
 * 一页放几张凭证 —— 这是**用户自己定的**，不再从纸型推。
 *
 * 为什么必须独立：纸型恒为 A4，从纸型推出来的答案永远是 2，
 * 「A4 一页一张」这条正当需求就走不通了。
 */
export const SLOTS_OPTIONS = [1, 2]

const PAPER_LABEL = {
  A4: 'A4',
}

/** 只在凭证场景说「每页几张」；账簿报表说这句话没有意义。 */
export function paperLabel(key, isVoucher = false) {
  const raw = PAPER_LABEL[key] || key
  return isVoucher ? raw : key
}

/** 把后端下发的 papers 收敛到允许选择的那一种。 */
export function pickPapers(papers = []) {
  return papers.filter((p) => PAPER_KEYS.includes(p.key))
}

/**
 * 模板存的是纸型 key。模板若是老数据、纸型已不在允许范围内，
 * 兜回 A4 —— 宁可打一张标准纸，也不要让下拉框显示一个列表里没有的值。
 */
export function normalizePaper(key) {
  return PAPER_KEYS.includes(key) ? key : 'A4'
}

/**
 * 读出一份模板「一页几张凭证」。
 *
 * 缺省是 2 而不是 1：后端 ``validate_template`` 对没有该字段的模板也补 2
 * （A4 的旧含义就是一页两张），两边必须取同一个缺省，否则设计器上显示的数字
 * 和真打出来的张数会对不上 —— 而这种错只有在真的打印之后才看得出来。
 */
export function slotsPerPage(tpl) {
  const n = Number(tpl?.voucher?.slots_per_page)
  return SLOTS_OPTIONS.includes(n) ? n : 2
}

/**
 * 正文字号的**下限**（pt）。
 *
 * 总账与明细账是拿给外部看的正式账簿，10pt 的正文在 A4 上看不清 ——
 * 用户 2026-10-23 定的：这两个范围的下限是 **12pt**，别的范围（凭证 / 余额表 /
 * 报表）仍是 6pt。
 *
 * **两边必须一样**：前端的下限与后端的下限是同一条线 ——
 * 后端是 ``app/printing/template.py`` 的 ``LEDGER_MIN_FONT_SIZE``（也是 12），
 * 校验 / 打印 / 导出三处都执行。
 * 各写各的就会有一处漏：漏在前端是**能填小值**（填 9pt，存下去后端再抬成 12pt，
 * 用户只会以为界面在骗人），漏在后端是**印出小字**。
 *
 * 放在这里而不是留在页面里，是为了让这条线**可测**：
 * 以前它只是 PrintTemplate.vue 里的一行三元表达式，测试只能对着源码做正则断言
 * （断言源码里存在字面量 `? 12 : 6`）—— 那种断言守不住「12 是不是真的是下限」，
 * 只守得住「那行字面量还在不在」。规则要有一个**能被直接调用**的地方。
 *
 * @param {string} scope 模板范围：``'LEDGER'`` 总账 / ``'DETAIL_LEDGER'`` 明细账
 * @returns {number} 正文字号下限（pt）
 */
export function minFontSize(scope) {
  return (scope === 'LEDGER' || scope === 'DETAIL_LEDGER') ? 12 : 6
}
