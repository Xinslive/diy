/**
 * 账套「起始年度」的候选年份。抽成纯函数好单测。
 *
 * 以前这个下拉只有 6 个写死的候选（去年到四年后），用户想建一本 1998 年起步的账，
 * 界面上根本选不出来 —— 那不是「候选少」，是**没给出口**，剩下的路只有去改库。
 *
 * 现在候选年份铺满整个可用区间（1900 年到今年往后 5 年），再配一个能搜的下拉：
 * 想得远的人可以输，想省事的人往下拉一拉就有。
 */

/**
 * 起始年度的合法范围。
 *
 * 后端 `BookIn.start_year` 用的是同一套边界（`backend/app/api/schemas.py`），
 * 所以界面上能选出来的年份，后端一定收得下。
 */
export const START_YEAR_MIN = 1900
export const START_YEAR_MAX = 2100

/** 往后给几年：明年的账现在开出来是常事，不必等到年底。 */
export const START_YEAR_FUTURE = 5

/**
 * 候选年份：**倒序**（新年份在上），从 `今年 + 5` 往下排到 1900 年。
 *
 * 为什么顶头不是 2100：倒序列表的第一屏就是用户最先看到的，
 * 顶上摆一串 2098、2097 这种没人会选的年份，等于把真正要选的年份挤到屏幕外。
 * 顶头停在「今年往后 5 年」，当前年度（默认选中那个）就在头几行里。
 *
 * 为什么底到 1900：再早的公司多半也不做这套账了；
 * 而真要更早，1900 也够到底了 —— 底下的年份要靠搜，不靠滚。
 *
 * 倒序是为了跟磁贴上的年度下拉方向一致（那边是 `yearsOf` 倒序排的），
 * 同一个页面里两处年度选择器不要一个正一个反。
 */
export function startYearOptions(now = new Date()) {
  const top = Math.min(START_YEAR_MAX, now.getFullYear() + START_YEAR_FUTURE)
  const out = []
  for (let y = top; y >= START_YEAR_MIN; y--) out.push(y)
  return out
}
