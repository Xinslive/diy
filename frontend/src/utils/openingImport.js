/**
 * 期初余额「导入期初余额」这一步的**作用域判定**：这次解析出来的结果，
 * 是不是当前页面这一份？
 *
 * **为什么要有这个模块。**
 *
 * 期初挂在**某一年的科目表**上，解析结果里的每一行都指着**那一年**的科目 id。
 * 年度对不上时，这些 id 在当前页面的科目里**一个都找不到** ——
 * 填进去等于把数写给了不存在的行，页面仍然是 0，
 * 而人看到的只是「导入没生效」。
 *
 * 生产实测（2026-10-31，账套「泉州劲松珠宝有限公司」2023~2026 四张科目表）：
 * 请求没带年度 → 后端按 2026 年认科目 → 页面是 2023 年那一张 →
 * 表格全是 0 → 刷新清空 → 保存把 0 存进去 → 死循环，钱一分没进去。
 *
 * **这里只判定，不改数。** 判定放在 utils 里是为了能单测：
 * 这个错一旦发生，界面上**一个字都看不出来**（全是 0，没有报错），
 * 靠正则去断言源码里有没有那几行，早晚会漏。
 *
 * 判定结果有两种：
 *   · `null` —— 对得上，可以填；
 *   · `{ reason, text }` —— 对不上，`text` 是给用户看的那句话。
 */

/**
 * @param {object} input
 * @param {number|string} [input.parsedYear] 解析时用的是哪一年的科目表（后端回传的）
 * @param {number|string} [input.pageYear]   当前页面显示的是哪一年
 * @param {Array<number>} input.parsedIds     解析结果里出现的科目 id
 * @param {Array<number>} input.onScreenIds   当前页面上摆着的科目 id
 * @returns {{reason: string, text: string}|null} 对不上时给出要说的话；对得上给 null
 */
export function importScopeMismatch({ parsedYear, pageYear, parsedIds, onScreenIds } = {}) {
  const py = Number(parsedYear) || 0
  const page = Number(pageYear) || 0
  // **年度对不上**：两份根本不是同一张表，填进去必然一行都对不上。
  // 先说年度 —— 它是根因，比「科目 id 对不上」更接近可执行的下一步。
  if (py && page && py !== page) {
    return {
      reason: 'year',
      text: `这份余额表解析到的是 ${py} 年科目表，当前页面是 ${page} 年，`
        + '不是同一份，已拒绝填入。请刷新页面后重新导入。',
    }
  }
  const parsed = Array.isArray(parsedIds) ? parsedIds : []
  const onScreen = new Set(Array.isArray(onScreenIds) ? onScreenIds : [])
  if (!parsed.length || !onScreen.size) return null
  const offScreen = parsed.filter((id) => !onScreen.has(id))
  // **一个都对不上**：解析的是另一张表（或另一年的科目）。
  // 只错一部分时先放过 —— 那些行多半是「科目不存在」的脏行，
  // 由导入预览那一步自己报，别在这里连坐。
  if (offScreen.length === parsed.length) {
    return {
      reason: 'ids',
      text: `读进来 ${parsed.length} 行，可是一个科目都对不上当前页面的 `
        + `${page || '本'} 年科目表，已拒绝填入（否则表格会一直是 0）。`
        + '请确认顶栏的年度与这份余额表对应。',
    }
  }
  return null
}
