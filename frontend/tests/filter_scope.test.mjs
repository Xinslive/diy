/**
 * 切换账套清条件（src/utils/filterScope.js，**线上那份源码**）。
 *
 * 用户定的口径（2026-10-23）：**切换账套清条件**。
 *
 * 条件（凭证号、分录金额）是**这一本账**里的检索 —— 拿到另一本账上去查，
 * 命中的是别的账的凭证，看着像「这个账套的凭证少了」。
 *
 * 而**换月不清**：条件一旦填了就把范围放宽到全部期间（见 VoucherList 的
 * hasFilter），月份本来就不参与条件，犯不着为它作废。
 *
 * 判定得放在**重挂时**：账套一变 MainLayout 的 viewKey 就变，整页重挂，
 * 组件里的 watch 活不到那一刻（重挂会把 watch 一起停掉）。
 * 重挂时手里只有 URL 上的条件和当前账套，所以条件得在 URL 里带着归属标记。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const {
  conditionsBelongTo
} = await createServer({
  root: fileURLToPath(new URL('../', import.meta.url)), logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/filterScope.js'))

const LIST = fs.readFileSync(new URL('../src/views/voucher/VoucherList.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + e.message.split('\n')[0]); fail++ }
}

console.log('== 归属判定 ==')

t('归属是当前账套 → 条件继续用', () => {
  assert.equal(conditionsBelongTo({ book: '6', kw: '16' }, 6), true)
})

t('归属是上一本账 → 条件作废', () => {
  assert.equal(conditionsBelongTo({ book: '6', kw: '16' }, 1), false)
})

t('没有归属标记 → 不作废（本来就没有条件，或者老地址）', () => {
  assert.equal(conditionsBelongTo({}, 6), true)
  assert.equal(conditionsBelongTo({ kw: '16' }, 1), true)
  assert.equal(conditionsBelongTo(undefined, 6), true)
})

t('归属是空串 → 不作废', () => {
  assert.equal(conditionsBelongTo({ book: '' }, 1), true)
})

t('数字与字符串的账套 id 认得出是同一个', () => {
  assert.equal(conditionsBelongTo({ book: '6' }, '6'), true)
  assert.equal(conditionsBelongTo({ book: 6 }, 6), true)
})

console.log('== 条件表里带着归属标记 ==')

t('条件表里有 book 这一项', () => {
  assert.match(LIST, /useUrlConditions\(\{[\s\S]*?\bbook\s*:/,
    '条件表里没有 book —— 归属没地方记，重挂后认不出条件是谁的')
})

t('归属取的是当前账套', () => {
  assert.match(LIST, /book\s*:\s*\{[\s\S]*?get\s*:\s*\(\)\s*=>[^}]*store\.bookId/,
    '归属没取当前账套')
})

t('没有条件时不写归属（地址栏只留用户真填过的）', () => {
  assert.match(LIST, /book\s*:\s*\{[\s\S]*?get\s*:\s*\(\)\s*=>\s*\(hasFilter\.value/,
    '归属没跟 hasFilter 挂钩 —— 空条件时地址栏也会多一个 book')
})

console.log('== 页面照这条口径行事 ==')

t('重挂时拿 URL 的归属跟当前账套比', () => {
  assert.match(LIST, /conditionsBelongTo\(route\.query\s*,\s*store\.bookId\)/,
    '没把地址栏上的归属跟当前账套拿来比 —— 换账套时认不出条件是谁的')
})

t('归属不对就清条件，并且不往回捡', () => {
  assert.match(LIST, /conditionsBelongTo\([^)]*\)[^{]*\{[\s\S]{0,300}?clearFilter\(\)/,
    '归属不对时没有清条件')
  assert.match(LIST, /clearFilter\(\)\s*\n\s*restore\(\[\]\)/,
    '清完条件还在从 URL 捡回来 —— 等于什么都没清')
})

t('换月不清条件', () => {
  const watchBlock = LIST.slice(LIST.indexOf('watch(() => [store.bookId'))
  assert.ok(!/clearFilter\(\)/.test(watchBlock),
    'watch 里还在清条件 —— 换月会把条件作废，而条件根本不参与月份')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
// vite 的 middleware 服务还开着，不主动退出会把 node --test 挂住
process.exit(fail ? 1 : 0)
