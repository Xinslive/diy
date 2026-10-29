/** 科目面板的搜索：**摊开几层**、**留下哪几行**。抽成纯函数好单测。 */

import { matchSubject } from './subjectCode'

/**
 * 搜索时**自动**摊到第几级（0 起）：只摊到第二级。
 *
 *  编码是前缀命中 —— 搜「5001」，5001、500101、50010101、5001010101 一个个
 *  全都算命中。整条链一路摊开，面板一下子推出几十行，要找的那条末级
 *  反而淹在里头。更深的照旧摆 ▸，点一下 / 按 → 自己往下走。
 *
 *  这是**指不出名来**时的默认：人敲的是个片段，命中一大片，
 *  那是「浏览这一片」，不是「找这一条」。
 *  指得出名来时（``searchNamed``）不按这个数走 —— 见那个函数。
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
 * 这条科目在树里是**第几级**（一级科目是 1）。
 *
 *  顺着 parent_id 一级一级数，不看 level 字段 ——
 *  字段是库里存的那一份，改挂父级之后要等后端重算才准
 *  （见 backend/app/api/subjects.py 的 _recalc_levels），面板这边等不得。
 */
export function levelOf(options, s) {
  const byId = new Map(options.map((x) => [x.id, x]))
  const seen = new Set()
  let level = 1
  let cur = s
  while (cur && cur.parent_id != null && !seen.has(cur.parent_id)) {
    seen.add(cur.parent_id)
    const parent = byId.get(cur.parent_id)
    if (!parent) break
    level++
    cur = parent
  }
  return level
}

/**
 * 这一串输入**指名了哪一条（或哪几条）科目**。
 *
 *  指名只有两种：
 *    ① 编码或名称**全等** —— 人把这一条科目的编码/名字整个敲进来了；
 *    ② **只有它一条**命中 —— 敲的是片段，但全表里只有这一条对得上。
 *
 *  命中一大片就**指不出名来**：那是「浏览这一片」，不是「找这一条」。
 *  这两种都指不出来（关键字是空的、或者压根没命中）时，返回 null ——
 *  调用方退回 SEARCH_AUTO_DEPTH 那条默认规矩（只摊到第二级）。
 *
 *  返回值：
 *    level    —— 被指名的科目是第几级。面板据此摊开：**输入第几级就显示到第几+1 级**
 *                （输入一级显示到二级、输入二级显示到三级、输入三级显示到四级）。
 *    ids      —— 被指名的那些科目自己。
 *    childIds —— 它们的**直接下级**：按名字指名的场合，下级名字不一样、匹配不上，
 *                不单列出来就看不见 —— 「输入二级显示到三级」这条对名字也该算数。
 *
 * ``match`` 是「算什么算命中」：**判定（摊多深）是同一件事，口径不该有两套**，
 * 但「用什么算命中」是各页面自己的契约 —— 科目表页的筛选框写的是「按编码或名称
 * 筛选」，不带拼音首字母那一路（那一路要另外把拼音字典加载进来，在那个页面上
 * 时灵时不灵）。所以把匹配器作为参数传进来，默认仍是全应用那一份。
 */
export function searchNamed(options, kw, match = matchSubject) {
  const k = String(kw ?? '').trim().toLowerCase()
  if (!k) return null
  const hits = []
  for (const s of options) if (match(s, k)) hits.push(s)
  if (!hits.length) return null
  const exact = hits.filter((s) => {
    const code = String(s.code ?? '').trim().toLowerCase()
    const name = String(s.name ?? '').trim().toLowerCase()
    return code === k || name === k
  })
  const named = exact.length ? exact : (hits.length === 1 ? hits : [])
  if (!named.length) return null
  const ids = new Set(named.map((s) => s.id))
  const childIds = new Set()
  for (const s of options) if (s.parent_id != null && ids.has(s.parent_id)) childIds.add(s.id)
  const level = Math.max(...named.map((s) => levelOf(options, s)))
  return { level, ids, childIds }
}

/**
 * 这一支摊不摊开（面板上那个 ▸ / ▾）。
 *
 *  不搜：只看人点过的 —— 面板打开是一张一级目录，收窄到已填的那一支也是靠它。
 *  搜：按级数自动摊开 —— 摊到 ``autoDepth`` 那一级，更深的收着。
 *  ``autoDepth`` 不是写死的：指名了某一级的科目就按那一级来
 *  （``searchNamed`` → 输入三级显示到四级），指不出名来才用默认的
 *  ``SEARCH_AUTO_DEPTH``（只摊到第二级，免得一堆下级把面板撑爆）。
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
