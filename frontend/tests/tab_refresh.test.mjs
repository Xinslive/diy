/**
 * 标签栏左端的「刷新当前页」按钮（**线上那份源码**）。
 *
 * 用户 2026-10-23 点名：原来那两支「关闭上一页 / 关闭下一页」按钮去掉，
 * 换成一个刷新按钮。
 *
 * 只看源码就下断言：它说的都是「界面上有没有这个东西」，
 * 真跑起来点一遍只能证明点得动，证明不了它没被换成一个别的控件。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const LAYOUT = fs.readFileSync(
  new URL('../src/layouts/MainLayout.vue', import.meta.url), 'utf8')

/** MainLayout 的**模板段**。「界面上有没有这个东西」只跟模板有关 ——
 *  注释里提到那两支按钮的名字是正常的（解释为什么去掉），不该被当成还在。 */
const LAYOUT_TPL = LAYOUT.slice(
  LAYOUT.indexOf('<template>'), LAYOUT.indexOf('</template>'))

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 标签栏：左端是刷新 ==')

t('有一支「刷新当前页」按钮', () => {
  assert.match(LAYOUT, /:title="refreshing \? '正在刷新' : '刷新当前页'"/)
  assert.match(LAYOUT, /@click="refreshView"/)
})

t('「关闭上一页 / 关闭下一页」两支按钮去掉了', () => {
  assert.doesNotMatch(LAYOUT_TPL, /关闭上一页/)
  assert.doesNotMatch(LAYOUT_TPL, /关闭下一页/)
  // 连着处理它们的两个函数一起走 —— 留着没人调的死函数更难查
  assert.doesNotMatch(LAYOUT, /function closePrev\(/)
  assert.doesNotMatch(LAYOUT, /function closeNext\(/)
  // 标签栏右端的「标签操作」下拉还在 —— 关标签的路子没被一起砍掉
  assert.match(LAYOUT, /关闭其他/)
})

t('刷新是真的重挂当前页：viewKey 带上刷新计数', () => {
  // 只是调一次接口不算刷新 —— 页面得重新取数，而取数是在组件挂载时做的，
  // 所以要让 key 变一变、让当前页重挂。
  assert.match(LAYOUT, /refreshKey\.value \+= 1/)
  assert.match(LAYOUT, /\$\{scope\}-\$\{refreshKey\.value\}/)
  // 刷新前先后台校验账套/期间：顶栏的月份下拉是全局上下文
  assert.match(LAYOUT, /await store\.bootstrap\(\{ force: true \}\)/)
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
