/**
 * 筛选条件**属于哪本账**的判定。
 *
 *  ## 背景
 *
 *  凭证列表的条件（凭证号、分录金额）住在 URL 里 —— 页面重挂后只认 URL
 *  （见 composables/useUrlConditions）。而账套一变，MainLayout 的 viewKey
 *  就变，整页**重挂**：组件里的 watch 活不到那一刻（重挂会把 watch 一起停掉），
 *  于是「切换账套要清条件」只能在重挂时判断。
 *
 *  重挂时手里只有两样东西：URL 上的条件，和当前账套。要判断
 *  「这组条件是不是上一本账留下的」，就得知道**条件是谁的**——
 *  光记住「条件存在过」不够：切走再切回来也是重挂，条件得留着。
 *
 *  所以条件在 URL 里带着一个归属标记（``book``），判定就是拿它跟当前账套比。
 *
 *  ## 两条规矩
 *
 *  · **没有条件就没有归属。** 条件全空的时候不写标记 —— 地址栏里只留
 *    用户真填过的那些（见 useUrlConditions 的 toQuery）。
 *  · **没有标记就当作「不是别人的」。** 老地址（没有 book 参数）、
 *    或者本来就没有条件，都不该因此清掉什么。
 */

/**
 * 这组条件是不是**当前账套**的。
 *
 * @param {Object} query  地址栏上的 query（route.query）
 * @param {number|string} bookId 当前账套
 * @returns {boolean} true = 条件可以继续用；false = 是上一本账的，该作废
 */
export function conditionsBelongTo(query, bookId) {
  const owner = query?.book
  // 没有归属标记：要么本来就没有条件，要么是老地址 —— 都当作「不是别人的」
  if (owner === undefined || owner === null || owner === '') return true
  return String(owner) === String(bookId)
}
