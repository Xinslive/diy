/**
 * 凭证列表「操作」列的列宽（src/views/voucher/VoucherList.vue）。
 *
 * 用户批注「缩小一下这个列宽」：那一列只放两个链接按钮，却占着 168px，
 * 右边空出四十多像素 —— 旁边的「状态」列本来就被它压住一截。
 *
 * 列宽按**最宽的那一格**定，不是按当前这批数据：未记账凭证的按钮是
 * 「查看/改」（3 个汉字 + 一个半角斜杠），已记账是「查看」；两个按钮之间
 * 12px 间距，单元格左右内边距各 12px。
 * → 56 + 12 + 34 + 24 = 126px 是下限，留 6px 余量给字体差异 → 132px。
 *
 * 这里守住三件事：列宽用固定值（min-width 会让它随内容长回去）、数值就是
 * 132、以及**按钮文案没变** —— 一旦「查看/改」改成更长的文案，132 就不再
 * 够了，这条例子要先把宽度重算一遍。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const SRC = fs.readFileSync(
  new URL('../src/views/voucher/VoucherList.vue', import.meta.url), 'utf8')

/** 操作列那一行的标签 */
const actionCol = SRC.match(/<el-table-column[^>]*label="操作"[^>]*>/)

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 凭证列表：操作列宽按最宽的那一格定 ==')

t('操作列用的是固定 width，不是 min-width（min-width 会长回去）', () => {
  assert.ok(actionCol, '找不到「操作」列')
  assert.match(actionCol[0], /\bwidth="/, '操作列没有固定宽度')
  assert.doesNotMatch(actionCol[0], /min-width=/, '操作列用了 min-width —— 它会随内容长回去')
})

t('操作列宽是 132px（最宽内容 126px + 6px 余量）', () => {
  const m = actionCol[0].match(/\bwidth="(\d+)"/)
  assert.ok(m, '操作列没有写死宽度')
  assert.equal(Number(m[1]), 132, `操作列宽是 ${m[1]}px，不是 132px`)
})

t('列宽的依据写在注释里：最宽的是「查看/改」那一种', () => {
  assert.match(SRC, /查看\/改/, '模板里没有「查看/改」这个文案分支')
  const idx = SRC.indexOf('label="操作"')
  const before = SRC.slice(Math.max(0, idx - 900), idx)
  assert.match(before, /126/, '没写清最宽一格需要多少像素 —— 改文案时就没法重算')
})

t('收窄不改变这一列的其它行为：仍固定在右侧，且仍按权限显示', () => {
  assert.match(actionCol[0], /fixed="right"/, '操作列不再固定在右侧')
  assert.match(actionCol[0], /v-if="store\.canWrite"/, '操作列的权限判断被改了')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
