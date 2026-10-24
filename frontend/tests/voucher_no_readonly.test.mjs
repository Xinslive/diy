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

console.log('== 改日期跨期：号跟着目标期间重发（用户 2026-10-24）==')

t('保存时不再把凭证号送回去', () => {
  // 号是服务端发的（新增走 next_voucher_no、插入走 insert_slot、
  // 改期跨期时按目标期间重发）。送回去的是**旧号**时，服务端刚重发完、
  // 页面还没来得及换（或者人连着改了两次日期，两趟保存撞在一起），
  // 服务端会回一句「凭证号不能手工修改（记0009 → 记0012）」——
  // 人完全看不懂是自己改的日期惹的祸。
  const payload = VIEW.match(/const payload = \{[\s\S]*?\n  \}/)
  assert.ok(payload, '找不到 payload')
  assert.doesNotMatch(payload[0], /voucher_no:/,
    'payload 里还在送凭证号 —— 跨期重发之后送过去的会是旧号')
})

t('保存成功后把服务端发的号回写到表单', () => {
  assert.match(VIEW, /form\.voucher_no = saved\.voucher_no/,
    '服务端重发过号之后，页面上那个只读的凭证号必须跟着换')
})

t('跨期之后期间也跟着换（页眉、辅助记账余额、打印都跟着这张走）', () => {
  assert.match(VIEW, /voucherPeriodId\.value = saved\.period_id/,
    '跨期之后期间没跟着换，页眉还写着旧期间')
})

t('凭证号不进「这张变了没有」的签名', () => {
  // 号不是人改的（页面上只读），而是服务端发的：它一变签名就变，
  // 等于凭空多一趟自动保存，还把「服务端改的号」误判成「人改了这张凭证」。
  const sig = VIEW.match(/const entrySig = computed\(\(\) => JSON\.stringify\(\[[\s\S]*?\]\)\)/)
  assert.ok(sig, '找不到 entrySig')
  assert.doesNotMatch(sig[0], /form\.voucher_no/,
    '凭证号进了 dirty 签名 —— 服务端重发号会触发一趟多余的自动保存')
})

console.log(`\n通过 ${pass} / 失败 ${fail}`)
process.exit(fail ? 1 : 0)
