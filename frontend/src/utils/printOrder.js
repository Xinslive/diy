/**
 * 打印顺序：这一次打印，纸按什么顺序出。
 *
 * **它是「这一次打印」的设置，不是模板字段** —— 模板只管版式（一张纸长什么样、
 * 一页放几张），不管这叠纸按什么顺序出；后者随打印机、随整理习惯而变。
 * 所以它住在打印弹窗里，与「方向」同属一次性的打印设置。
 *
 * **这里是两个维度，不是一个**：
 *
 * * **怎么配对**（``print_order``，取值与后端 ``app/printing/template.py`` 的
 *   ``PRINT_ORDERS`` 一一对应）：
 *   - ``normal``：连续配对 —— 凭证一张接一张（1+2 / 3+4 / 5+6）。
 *   - ``optimized``：错位配对 —— 一页两张时每张纸的上下两格配成相隔一半的一对
 *     （1+4 / 2+5 / 3+6）。沿中间裁开之后两堆各自成序。
 * * **出纸的先后**（``reverse``，一个布尔）：整叠倒着印。打印机有的把先印的
 *   那张压在最上面，有的把最后印的那张压在最上面，倒过来印才能对上。
 *
 * 合成一个开关就得在开关旁边解释「这一项在两种模板上分别是什么意思」，
 * 解释不清就等于没解释 —— 所以是**两个开关**：「打印顺序优化」管配对，
 * 「倒序打印」管先后。
 */

export const PRINT_ORDER_NORMAL = 'normal'
export const PRINT_ORDER_OPTIMIZED = 'optimized'
export const PRINT_ORDERS = [PRINT_ORDER_NORMAL, PRINT_ORDER_OPTIMIZED]

/**
 * 两个开关各自的默认值。
 *
 * 分开是因为两种打印方式的默认不一样（用户 2026-10-30 批注 2、3）：
 * 错位配对默认开 —— 打完裁开两半叠起来就是顺序，省掉一次手工整理；
 * 倒序打印默认关 —— 有人的打印机本来就是按印出顺序叠的（出纸顺序即顺序），
 * 倒着印反而把顺序弄反了。
 */
export const PRINT_ORDER_DEFAULTS = { interleave: true, reverse: false }

/**
 * 「打印顺序优化」下面那一句：说清打开之后纸上的顺序会变成什么样。
 *
 * 一页一张时这一项**不出现**（返回空串）：每张纸只有一张凭证，没有「配对」
 * 这回事，摆出来只会让人猜它有什么用。
 */
export function printOrderHint(slots) {
  if (Number(slots) < 2) return ''
  return '每张 A4 的上下两格错开印：1-6 号印成 1+4 / 2+5 / 3+6，裁开两半后上堆是 1、2、3、下堆是 4、5、6。'
}

/** 「倒序打印」下面那一句。 */
export function reverseHint(slots) {
  if (Number(slots) >= 2) {
    return '整叠倒着印：先印 3+6、再印 2+5、最后印 1+4（每张纸上配哪两格不变）。打印机把最后印的那张压在最上面时才需要。'
  }
  return '整叠倒着印，打出来的一叠自上而下才是凭证顺序（打印机本来就按印出顺序叠的就用不着）。'
}

/**
 * 这一页要送给后端的**配对**口径。
 *
 * @param {number} slots 当前模板一页放几张凭证
 * @param {{interleave?: boolean}} opts 「打印顺序优化」开关的状态
 * @returns {string} ``normal`` / ``optimized``
 */
export function printOrderParam(slots, opts = {}) {
  if (Number(slots) < 2) return PRINT_ORDER_NORMAL
  return opts.interleave ? PRINT_ORDER_OPTIMIZED : PRINT_ORDER_NORMAL
}
