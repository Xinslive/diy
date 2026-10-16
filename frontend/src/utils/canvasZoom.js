/**
 * 画布缩放的纯计算。
 *
 * 抽成独立模块是因为这三条规则每一条都容易写错，而它们错的方式都很隐蔽：
 *   · 缩放比没夹住 —— 表格缩成负数，或者大到点不中格子，而且不报错；
 *   · 「适应宽度」拿容器宽当分母 —— 表格本来就铺满容器，比值恒等于 1，
 *     这个按钮点了永远停在 100%，看上去「能用」，其实什么也没做；
 *   · 拿过期宽度算 —— 面板一收起画布变宽，按上一版的表宽算出 130%，
 *     而且因为宽度之后再也不变，永远纠不回来。
 *
 * 所以这里只放纯函数：给定数、得出数，浏览器的事留给调用方。
 */

/** 再小也还认得出是一张表，再大也不会大到点不中格子。 */
export const MIN_SCALE = 0.3
export const MAX_SCALE = 2.5

/**
 * 缩放比夹在 [MIN_SCALE, MAX_SCALE] 内，并**保留两位小数**。
 *
 * 保留两位不是洁癖：界面上读的是 Math.round(scale*100)%，不夹住小数的话
 * 连点几下会攒出一串 0.7000000000000001，百分比跟着跳。
 * 非法值（NaN / Infinity）回落到 1，而不是 NaN —— 一个 NaN 的 scale
 * 会让整张表连同占位层一起消失，比显示错更糟。
 */
export function clampScale(v) {
  if (!Number.isFinite(v)) return 1
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, Math.round(v * 100) / 100))
}

/**
 * 「适应宽度」：把**内容**缩进**容器**。
 *
 * @param {number} availW   滚动容器扣掉内边距后的可用宽
 * @param {number} contentW 内容的自然宽（表格自己的 offsetWidth）
 * @returns {number|null}   算不出时返回 null，调用方该保持原档而不是写个 1 进去
 */
export function fitScale(availW, contentW) {
  if (!(availW > 0) || !(contentW > 0)) return null
  return clampScale(availW / contentW)
}

/**
 * 表格的自然宽度下限：各列模板自带列宽之和。
 *
 * **为什么缩放之前得先有这个下限。** `table-layout` 默认 auto，`th` 上的
 * width 只是「建议」：容器比列宽之和窄时，浏览器为了塞下会把列压到最小内容
 * 宽度，项目名列直接夹成两三个字。这时候再点「适应宽度」，缩小的只是一张
 * **已经被压扁的表** —— 整张表看得全了，可每列还是读不出字，那不是缩放要解决
 * 的问题。所以顺序得反过来：先让表按自己的列宽站住，再整体缩到一屏。
 *
 * 逐列 `Number()` 兜一层：列宽在模型里是可空的（`Optional[int]`），
 * 直接 `sum + c.width` 遇上一个 undefined 就把整个 min-width 变成 NaN，
 * 表格会连带占位层一起消失。
 *
 * @param {Array<{width?: number|null}>} cols
 * @returns {number} 容器比它宽时不生效，表格照样铺满，查看模式老样子不变
 */
export function naturalWidth(cols) {
  return (cols || []).reduce((sum, c) => sum + (Number(c?.width) || 0), 0)
}
