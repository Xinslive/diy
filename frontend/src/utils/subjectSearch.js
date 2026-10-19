/** 科目面板的搜索：**摊开几层**、**留下哪几行**。抽成纯函数好单测。 */

import { matchSubject } from './subjectCode'

/**
 * 搜索时**自动**摊到第几级（0 起）：只摊到第二级。
 *
 *  编码是前缀命中 —— 搜「5001」，5001、500101、50010101、5001010101 一个个
 *  全都算命中。整条链一路摊开，面板一下子推出几十行，要找的那条末级
 *  反而淹在里头。更深的照旧摆 ▸，点一下 / 按 → 自己往下走。
 */
export const SEARCH_AUTO_DEPTH = 1

/**
 * 打上关键字之后留下哪几条：**命中的**，加上**从一级到它的整条路**。
 *
 *  路要留全，不能只看直接下级：搜「100202」命中的是三级，它的二级、一级
 *  都得留着，不然面板上凭空冒出一条没有上级的 100202，看着像孤儿科目。
 *  （留了整条路也**不等于**摊开整条路 —— 摊多深是 rowOpen 那边的规矩。）
 */
export function searchHitIds(options, kw) {
  if (!String(kw ?? '').trim()) return null
  const byId = new Map(options.map((s) => [s.id, s]))
  const hit = new Set()
  for (const s of options) if (matchSubject(s, kw)) hit.add(s.id)
  // 命中项的整条路一起补齐。边加边往上走：父进了 hit 就停，
  // 免得同一个科目按深度反复爬（命中得多时是白跑）。
  for (const id of [...hit]) {
    let cur = byId.get(id)
    while (cur && cur.parent_id != null && !hit.has(cur.parent_id)) {
      hit.add(cur.parent_id)
      cur = byId.get(cur.parent_id)
    }
  }
  return hit
}

/**
 * 这一支摊不摊开（面板上那个 ▸ / ▾）。
 *
 *  不搜：只看人点过的 —— 面板打开是一张一级目录，收窄到已填的那一支也是靠它。
 *  搜：默认只自动摊到 SEARCH_AUTO_DEPTH 那一级，更深的收着。
 *
 *  但**人点过的一律作数**，两个方向都是：
 *  点深一层的 ▸ 要真能摊开（不然第三级以后走不进去），
 *  点一级那个自动摊开的 ▾ 也要真能收起来 —— 不然那个箭头就是个摆设：
 *  它明明画着 ▾，点下去什么都不发生，人只会以为是自己没点中
 *  （以前就是这样：搜索时 expanded 被忽略，▾ 纯属摆设）。
 *  所以「点开」记 expanded、「点收」记 collapsed，后者压过一切。
 */
export function rowOpen({ hasKids, depth = 0, searching = false, expanded = null, collapsed = null, id = null, autoDepth = SEARCH_AUTO_DEPTH }) {
  if (!hasKids) return false
  if (collapsed && collapsed.has(id)) return false
  if (expanded && expanded.has(id)) return true
  return searching ? depth < autoDepth : false
}
