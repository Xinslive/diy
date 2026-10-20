/**
 * 账套管理页的空状态（src/views/book/BookManager.vue，**线上那份源码**）。
 *
 * 这里钉的是「一个账套都没有时，页面上**不该出现**的话」。
 * 以前空状态里挂着一整段提示：
 *   「还没有账套，点右上角「新建账套」自己开一个；
 *     也可以请管理员在「用户与授权」里把已有账套授权给你」
 *
 * 去掉它有两个理由：
 *   1. 右上角那颗按钮已经写着「新建账套」，再用一整句话复述一遍是噪音；
 *   2. 后半句「请管理员在「用户与授权」里把已有账套授权给你」只对**被授权**的
 *      场景成立 —— 「用户与授权」那一项本来只有管理员看得见，对普通用户
 *      是把一条走不通的路指出来。
 *
 * 用源码级断言而不是渲染快照：要防的是「这段话又被写回去」，
 * 断的是页面上还有没有这段话，不是它长什么样。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const BOOKS = fs.readFileSync(new URL('../src/views/book/BookManager.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 空状态不再有长提示 ==')

t('页面上没有那句「还没有账套…」', () => {
  assert.doesNotMatch(BOOKS, /还没有账套/, '空状态的长提示又回来了')
})

t('也没有指向「用户与授权」的那半句', () => {
  assert.doesNotMatch(BOOKS, /用户与授权/, '空状态又在指一条普通用户走不通的路')
})

t('空状态不挂一个无字的占位框', () => {
  // 只删文字、留下那个灰框图标，页面中间悬着一个空箱子比什么都没有更怪
  assert.doesNotMatch(BOOKS, /<el-empty/, '空状态还剩一个无字的 el-empty 占位')
})

console.log('')
console.log('== 但出路的按钮必须在 ==')

t('右上角「新建账套」按钮还在', () => {
  assert.match(BOOKS, /class="create-book-button"/, '没有账套的人就没有建账套的入口了')
  assert.match(BOOKS, /新建账套/, '按钮上的字没了')
})

t('这颗按钮没有挂在「有账套才显示」的条件上', () => {
  // 提示删了以后，这颗按钮就是无账套用户唯一的出路 ——
  // 它要是被条件藏起来，页面就成了一条走不通的死路。
  const m = BOOKS.match(/<el-button[^>]*class="create-book-button"[^>]*>/)
  assert.ok(m, '没找到那颗按钮')
  assert.doesNotMatch(m[0], /v-if/, '按钮被条件藏起来了，没有账套的人反而建不了账套')
})

t('「N 个账套」这个计数还在（空状态下它是唯一的状态说明）', () => {
  assert.match(BOOKS, /class="book-count"/, '空状态下没有任何地方说明现在有几个账套')
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
