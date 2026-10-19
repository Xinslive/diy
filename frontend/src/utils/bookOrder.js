/**
 * 账套磁贴排序的**纯判定**（拖拽计算与落库口径都在这里）。
 *
 *  为什么单独抽出来：拖拽要处理三类情况，而它们的边界很难靠肉眼在组件里
 *  看住 —— 拖自己（顺序没变，别发请求）、拖到末尾（会不会差一格）、
 *  以及「往前拖」时被指的那张磁贴自己在结果里会往前挪一位。
 *  写成一个纯函数后，这些分支在单测里能挨个跑一遍，不必挂起组件模拟
 *  一整套 drag 事件（tests/book_order.test.mjs）。
 *
 *  **顺序是「相对先后」，不是「第几号」。** 提交时只发一串有序的账套 id，
 *  后端据此重编 0..n-1。所以中途新建的账套能被接在末尾，
 *  而不会把用户已经排好的那一串序号顶歪。
 */

/** 拖动前后的顺序一样时，返回 null 表示「不必发请求」。 */
const NO_CHANGE = null

/**
 * 把 `id` 从它现在的位置挪到**结果里的第 `to` 位**，返回新数组；**不动**原数组。
 *
 *  `to` 刻意定义成「结果里的下标」而不是「原列表里的插入位」：后者每多一层
 *  调用方就要自己判断「被拖的那本排在它前面还是后面、要不要减一」，
 *  而这个减一正是列表拖拽最典型的 off-by-one —— 表现是往前拖时差一格，
 *  越用越糟。
 *
 * @param {Array<{id:number}>} list 当前顺序
 * @param {number} id 被拖的那一本
 * @param {number} to  结果里的下标（越界夹到合法范围）
 * @returns {Array|null} 新顺序；没变化、或落点看不懂时返回 null
 */
export function moveTo(list, id, to) {
  const from = list.findIndex((b) => b.id === id)
  // 拖一个不在列表里的 id：不是「没变化」，是数据对不上，
  // 静默返回 null 等于把这次拖动静默吃掉，用户看不出哪里不对
  if (from < 0) return NO_CHANGE
  const target = Math.max(0, Math.min(Number(to) || 0, list.length - 1))
  if (from === target) return NO_CHANGE
  const next = list.slice()
  const [moved] = next.splice(from, 1)
  next.splice(target, 0, moved)
  return next
}

/**
 * 拖拽落点 → 新顺序。
 *
 *  按光标落在卡片**哪一半**来定「插到它前面还是后面」：只做「插到被指的那张
 *  之前」的话，想把 A 放到 B **后面**（最常见的意图之一）永远做不到，
 *  得先把 A 拖到 B 前面、再绕回去调一次。
 *
 *  「被指的那张在结果里是第几」要分两种情况：被拖的那本原本排在它前面时，
 *  它自己会往前挪一格。这个 ±1 只在这里做一次，调用方不必判断 ——
 *  这正是把它收进本函数、而不是让组件自己算的原因。
 *
 * @param {Array<{id:number}>} list 当前顺序
 * @param {number} dragId 正在被拖的那本
 * @param {number} hoverIndex 光标所在磁贴的下标（-1 = 不在任何磁贴上）
 * @param {boolean} after 光标在它右半边
 * @returns {Array|null} 新顺序；落点无效或没变化时返回 null
 */
export function dropInto(list, dragId, hoverIndex, after) {
  if (hoverIndex < 0) return NO_CHANGE
  const from = list.findIndex((b) => b.id === dragId)
  if (from < 0) return NO_CHANGE
  // 悬停在自己身上：不管光标在左半边还是右半边，都**不许动**。
  // 不挡住「右半边」这一支的话，手指还压在自己卡片上，它就往后窜一格 ——
  // 原地不动的一个手势变成了真拖动，而且每经过一次就窜一次
  if (from === hoverIndex) return NO_CHANGE
  const hoverAt = from < hoverIndex ? hoverIndex - 1 : hoverIndex
  return moveTo(list, dragId, after ? hoverAt + 1 : hoverAt)
}

/** 取出提交给后端的那一串 id（顺序即磁贴顺序）。 */
export const orderIds = (list) => (list || []).map((b) => b.id)

/** 两串 id 是否同一顺序。落库前后拿它判「要不要发这一发」。 */
export const sameOrder = (a, b) => {
  const x = orderIds(a)
  const y = orderIds(b)
  return x.length === y.length && x.every((v, i) => v === y[i])
}

/**
 * 键盘移动：把当前这本往前/往后挪一格。
 *
 *  为什么要有：原生 HTML5 拖拽**只对鼠标有效**，键盘和读屏用户拿不到。
 *  卡片已经是可聚焦的列表项，于是「Alt/⌘ + ↑/↓」就够了 ——
 *  比完整的键盘拖放模式好实现，也不至于把用户挡在功能外。
 *
 * @returns {Array|null} 新顺序；到头了动不了时返回 null
 */
export function moveBy(list, id, delta) {
  const from = list.findIndex((b) => b.id === id)
  if (from < 0) return NO_CHANGE
  const to = from + (delta < 0 ? -1 : 1)
  if (to < 0 || to > list.length - 1) return NO_CHANGE
  return moveTo(list, id, to)
}

/** 键盘事件里读出「这次要挪几格」；不是快捷键则返回 0（= 不处理）。 */
export function moveDelta(event) {
  if (!(event.altKey || event.metaKey)) return 0
  if (event.key === 'ArrowUp') return -1
  if (event.key === 'ArrowDown') return 1
  return 0
}
