/**
 * 纸型口径：**整个系统只提供 A4 和 A5 两种**。
 *
 * 为什么砍到两种：打印纸型最终要落到实物上，而用户手里真正会装进打印机的就是
 * A4 和 A5 两种纸。列着 A3/B5/连续纸看着齐全，实际是让用户在每次打印前先想一遍
 * 「我这台机器进得了吗」—— 那是选纸型之前就该解决的事，不该在打印对话框里解决。
 *
 * 这里同时收编原先散在设计器和打印弹窗里的两份常量（纸型文案、惯用方向、
 * 每页几张凭证）。三处各写一份的结果是：后端 PAPERS 加了纸型，前端忘了加，
 * 用户看到的列表就少一项，而且没有任何报错。
 */

/** 允许选择的纸型。后端 PAPERS 仍保留其余纸型，老模板照常能取回和渲染。 */
export const PAPER_KEYS = ['A4', 'A5']

/** 一页能放两张凭证的纸型，要和后端 TWO_UP_PAPERS 保持一致。 */
export const TWO_UP_PAPERS = ['A4', 'A3', 'A4_LANDSCAPE']

/** 各纸型的惯用方向：记账凭证在 A5 上是横向小册子，A4 则是纵向。 */
export const DEFAULT_ORIENTATION = {
  A4: 'portrait', A3: 'portrait', A4_LANDSCAPE: 'landscape',
  A5: 'landscape', B5: 'landscape', CONTINUOUS: 'landscape',
}

const PAPER_LABEL = {
  A4: 'A4（每页 2 张凭证）',
  A5: 'A5（每页 1 张凭证）',
}

/** 只在凭证场景说「每页几张」；账簿报表说这句话没有意义。 */
export function paperLabel(key, isVoucher = false) {
  const raw = PAPER_LABEL[key] || key
  return isVoucher ? raw : key
}

/** 把后端下发的 papers 收敛到允许选择的两种。 */
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