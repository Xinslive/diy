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
 * 合成一个开关，那一项在两种模板上分别是什么意思就说不清了 —— 所以是
 * **两个开关**：「打印顺序优化」管配对，「倒序打印」管先后。
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
