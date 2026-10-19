/**
 * 凭证号的两处改动（src/views/voucher/VoucherEdit.vue，**线上那份源码**）：
 *
 *  ① 凭证号输入框一律只读 —— 新增 / 插入 / 修改三处都不许手工改（用户
 *     2026-10-19 定的）。生产事故就是这么来的：优境 9 月一张 18 条分录的工资
 *     凭证，号被人在**修改页**改成 "wochakan"，凭证列表四种排序全部 500
 *     （排序键要对凭证号做 CAST AS INTEGER），整月打不开。
 *  ② 「增加凭证」= **向后**插入：拿当前号 +1，比它大的号顺位；
 *     「插入凭证」= 向前插入：占当前号，当前及以后的顺位。
 *
 * 都只看源码就下断言：①说的是「这个框还能不能打字」，跑起来点一遍只能证明
 * 点不动，证明不了它没被换成一个别的可编辑形态（比如换成 contenteditable）。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')
const API = fs.readFileSync(
  new URL('../src/api/voucher.js', import.meta.url), 'utf8')

/** 凭证号那个输入框的整段标签。 */
const noInput = () => {
  const m = VIEW.match(/<el-input[^>]*v-model="form\.voucher_no"[^>]*>/)
  assert.ok(m, '凭证号输入框不见了')
  return m[0]
}

/** 取某个函数体的源码。 */
const fnSrc = (name) => {
  const start = VIEW.indexOf(`function ${name}(`)
  assert.ok(start >= 0, `找不到函数 ${name}`)
  const end = VIEW.indexOf('\n}', start)
  return VIEW.slice(start, end)
}

let pass = 0, fail = 0
const t = (name, body) => {
  try { body(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 凭证号只读（新增 / 插入 / 修改三处）==')

t('凭证号输入框带 readonly', () => {
  assert.match(noInput(), /\breadonly\b/)
})

t('没有 placeholder 暗示「还能手填」', () => {
  assert.doesNotMatch(noInput(), /placeholder/,
    'placeholder 还在提示可以清空/手填，与只读矛盾')
})

t('不是靠 disabled 挡的 —— disabled 会连背景一起变灰，看着像「这张不能看」', () => {
  assert.doesNotMatch(noInput(), /\bdisabled\b/)
})

t('没有把输入框换成别的可编辑形态', () => {
  const bar = VIEW.slice(VIEW.indexOf('<div class="toolbar no-print">'),
                         VIEW.indexOf('<div class="card">'))
  assert.doesNotMatch(bar, /contenteditable/, '凭证号变成了 contenteditable')
})

console.log('== 增加凭证 = 向后插入，插入凭证 = 向前插入 ==')

t('增加凭证带参照凭证 id', () => {
  assert.match(fnSrc('addVoucher'), /voucherId\.value/,
    '增加凭证必须带上参照凭证，否则没有号可算')
})

t('增加凭证是向后插入（拿参照号 +1）', () => {
  assert.match(fnSrc('addVoucher'), /position:\s*'after'/)
})

t('插入凭证是向前插入（占参照自己的号）', () => {
  assert.match(fnSrc('insertVoucher'), /position:\s*'before'/)
})

t('增加凭证不再退化成「不带参照的新开一张」', () => {
  assert.doesNotMatch(fnSrc('addVoucher'),
    /router\.push\(\{\s*name:\s*'voucher-new'\s*\}\)/)
})

t('预填时把 position 透传给后端', () => {
  assert.match(VIEW, /loadInsertPreview\(after,\s*route\.query\.position === 'after'/,
    '新页面没把 position 传给 insert-preview，预填的号会算成向前插入')
})

t('API 客户端带上了 position 参数', () => {
  assert.match(API, /insert-preview/)
  assert.match(API, /position/, 'insertPreview 没传 position，后端拿不到前后语义')
})

t('保存时把位置一起提交', () => {
  assert.match(VIEW, /insert_position:/,
    'payload 里没有 insert_position，后端只能默认向前插入')
})

console.log(`\n通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
