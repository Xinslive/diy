/**
 * 账套磁贴排序（src/utils/bookOrder.js，**线上那份源码**）。
 *
 *  这个特性最怕的不是「拖不动」，是**看起来能拖、其实没存住**：
 *  刷新一次顺序弹回去，用户试两下就再也不信它了。所以这里重点测三件事 ——
 *  拖完算出新顺序、算出「没变就别发请求」、以及越界不越界。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const {
  dropInto, moveBy, moveDelta, moveTo, orderIds, sameOrder
} = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/bookOrder.js'))

const BOOKS = fs.readFileSync(new URL('../src/views/book/BookManager.vue', import.meta.url), 'utf8')
const API = fs.readFileSync(new URL('../src/api/book.js', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

const books = (...ids) => ids.map((id) => ({ id, name: '账套' + id }))

console.log('== moveTo：拖动算新顺序 ==')

t('往后挪（to 是**结果里**的下标）', () => {
  assert.deepEqual(orderIds(moveTo(books(1, 2, 3), 1, 2)), [2, 3, 1])
})
t('往前挪', () => {
  assert.deepEqual(orderIds(moveTo(books(1, 2, 3), 3, 0)), [3, 1, 2])
})
t('挪一格', () => {
  assert.deepEqual(orderIds(moveTo(books(1, 2, 3), 2, 0)), [2, 1, 3])
  assert.deepEqual(orderIds(moveTo(books(1, 2, 3), 1, 1)), [2, 1, 3])
})
t('拖到自己原来的位置 = 没变（返回 null，别发请求）', () => {
  assert.equal(moveTo(books(1, 2, 3), 2, 1), null)
})
t('拖一个不在列表里的 id —— 不许静默吃掉', () => {
  // 静默返回 null 等于「这次没拖」，用户只会以为功能坏了
  assert.equal(moveTo(books(1, 2, 3), 99, 0), null)
})
t('原数组不被改（拖拽失败要能直接回滚）', () => {
  const src = books(1, 2, 3)
  const copy = src.slice()
  moveTo(src, 1, 2)
  assert.deepEqual(src, copy, '拖完原列表被改了 —— 落库失败时就回不去了')
})
t('越界夹到合法范围', () => {
  assert.deepEqual(orderIds(moveTo(books(1, 2, 3), 1, 99)), [2, 3, 1], '往后拖过头')
  assert.deepEqual(orderIds(moveTo(books(1, 2, 3), 3, -5)), [3, 1, 2], '往前拖过头')
})
t('看不懂的落点按「没拖」处理，不许把列表搅乱', () => {
  assert.equal(moveTo(books(1, 2, 3), 1, NaN), null)
  assert.equal(moveTo(books(1, 2, 3), 1, undefined), null)
})
t('只有一本时拖不动', () => {
  assert.equal(moveTo(books(7), 7, 0), null)
})

console.log('')
console.log('== dropInto：光标落点怎么变成新顺序 ==')
//
// 这一段钉的是拖拽最容易错的一格：被拖的那本原本排在被指磁贴**前面**时，
// 它自己会先被拿走，被指磁贴在结果里就往前挪了一位。不扣掉这一位的话，
// 「往前拖」每次都会差一格。

t('落在右半边 = 插到那张**后面**', () => {
  assert.deepEqual(orderIds(dropInto(books(1, 2, 3), 1, 2, true)), [2, 3, 1])
  assert.deepEqual(orderIds(dropInto(books(1, 2, 3), 3, 0, true)), [1, 3, 2])
})
t('落在左半边 = 插到那张**前面**', () => {
  // 拖 1 到「3」的左半边 → 1 紧挨在 3 前面
  assert.deepEqual(orderIds(dropInto(books(1, 2, 3), 1, 2, false)), [2, 1, 3])
  // 拖 1 到「2」的左半边 → 它本来就在 2 前面，不动
  assert.equal(dropInto(books(1, 2, 3), 1, 1, false), null)
  assert.deepEqual(orderIds(dropInto(books(1, 2, 3), 3, 0, false)), [3, 1, 2])
})
t('往前拖时不差一格（被指磁贴会先往前挪一位，必须扣掉）', () => {
  // 把 1 拖到 2 的右半边：结果里 1 该紧跟在 2 后面，即 [2, 1, 3]
  assert.deepEqual(orderIds(dropInto(books(1, 2, 3), 1, 1, true)), [2, 1, 3])
  // 把 3 拖到 1 的左半边：结果里 3 该紧挨在 1 前面，即 [3, 1, 2]
  assert.deepEqual(orderIds(dropInto(books(1, 2, 3), 3, 0, false)), [3, 1, 2])
})
t('落到自己身上 = 没变（右半边也算）', () => {
  // 手指还压在自己卡片上，它却往后窜一格 —— 原地不动的手势变成了真拖动
  assert.equal(dropInto(books(1, 2, 3), 2, 1, false), null)
  assert.equal(dropInto(books(1, 2, 3), 2, 1, true), null)
})
t('不在任何磁贴上（拖到空白处）= 不动', () => {
  assert.equal(dropInto(books(1, 2, 3), 1, -1, true), null)
})
t('拖一个不在列表里的 id = 不动', () => {
  assert.equal(dropInto(books(1, 2, 3), 99, 0, true), null)
})

console.log('')
console.log('== 落库口径 ==')

t('同顺序返回 true —— 拖了个寂寞就别打扰后端', () => {
  assert.equal(sameOrder(books(1, 2, 3), books(1, 2, 3)), true)
})
t('只要差一位就是不同', () => {
  assert.equal(sameOrder(books(1, 2, 3), books(2, 1, 3)), false)
  assert.equal(sameOrder(books(1, 2, 3), books(1, 2)), false, '长度不同也要算变了')
})
t('空表不炸', () => {
  assert.deepEqual(orderIds(null), [])
  assert.equal(sameOrder([], []), true)
})

console.log('')
console.log('== 键盘也要能排（原生拖拽只认鼠标） ==')

t('moveBy 前后各一格', () => {
  assert.deepEqual(orderIds(moveBy(books(1, 2, 3), 1, 1)), [2, 1, 3])
  assert.deepEqual(orderIds(moveBy(books(1, 2, 3), 3, -1)), [1, 3, 2])
})
t('到头了就动不了', () => {
  assert.equal(moveBy(books(1, 2, 3), 1, -1), null)
  assert.equal(moveBy(books(1, 2, 3), 3, 1), null)
})
t('只有按住 Alt/⌘ 才算快捷键', () => {
  assert.equal(moveDelta({ key: 'ArrowUp' }), 0, '光按方向键不该把别人的卡片挪了')
  assert.equal(moveDelta({ key: 'ArrowUp', altKey: true }), -1)
  assert.equal(moveDelta({ key: 'ArrowDown', metaKey: true }), 1)
  assert.equal(moveDelta({ key: 'Enter', altKey: true }), 0)
})

console.log('')
console.log('== 组件确实用上了（源码级断言） ==')

t('磁贴必须可拖，且声明了排布容器', () => {
  assert.match(BOOKS, /draggable="true"/, '卡片不能拖')
  assert.match(BOOKS, /@dragstart|onDragstart/, '没有拖拽起点')
  assert.match(BOOKS, /@drop|onDrop/, '没有落点')
})
t('拖动计算走 utils，不在模板里现拼数组', () => {
  assert.match(BOOKS, /dropInto\(/)
  assert.match(BOOKS, /moveBy\(/)
  assert.ok(!/\.splice\(/.test(BOOKS), '组件里又手写了一遍 splice')
})
t('落库失败要把顺序退回去（不然后面每次刷新都在跟用户打架）', () => {
  assert.match(BOOKS, /catch[\s\S]{0,400}?store\.books\s*=/,
    '失败分支没有还原顺序')
})
t('API 里有保存顺序这一条', () => {
  assert.match(API, /\/books\/order/)
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
