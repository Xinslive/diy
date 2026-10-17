/**
 * 凭证编辑页工具条的两处调整（src/views/voucher/VoucherEdit.vue，**线上那份源码**）：
 *
 *  ① 去掉「附件」那个数字框；
 *  ② 「打印预览」不再随「这张不能改」变灰。
 *
 * 两处都只看**源码**就下断言，因为它们说的都是「工具条上有没有这个东西」——
 * 真跑起来点一遍只能证明点得动，证明不了它没被换成一个别的禁用条件。
 * 第二组（附件的读写还在）更要紧：那个字段现在界面上没有输入口了，看着像
 * 死字段，而它其实还在 load/save 里来回搬 —— 一旦有人按「界面都没了」把它
 * 一起删掉，**存量凭证的附单据数会在下一次保存时静默清零**。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')

/** 工具条那一段：从 `toolbar no-print` 到卡片区之前。 */
const toolbar = () => VIEW.slice(
  VIEW.indexOf('<div class="toolbar no-print">'), VIEW.indexOf('<div class="card">'))

/** 「打印预览」那个按钮的整段标签。 */
const printButton = () => {
  const m = toolbar().match(/<el-button[^>]*printVoucher[^>]*>/)
  assert.ok(m, '工具条里没有「打印预览」按钮')
  return m[0]
}

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 工具条上不再有「附件」输入 ==')

t('工具条里没有附件标签，也没有那个数字框', () => {
  const bar = toolbar()
  assert.doesNotMatch(bar, /附件/)
  assert.doesNotMatch(bar, /el-input-number/)
  // 整页也顺手看一眼：别改个地方又从别处冒出来一个
  assert.doesNotMatch(VIEW, /<el-input-number/)
})

t('工具条仍然只剩那几个动作，没顺手删掉别的', () => {
  const bar = toolbar()
  for (const label of ['返回', '凭证号', '日期', '前一张', '后一张',
    '插入凭证', '增加凭证', '打印预览']) {
    assert.ok(bar.includes(label), `工具条上少了「${label}」`)
  }
})

console.log('== 打印是读，不受「不能改」的三条限制 ==')

t('「打印预览」不挂任何 disabled 条件', () => {
  // 原来这里是 :disabled="form.status === 'POSTED'"。三条限制各有各的判据
  // （posted / locked / !canWrite），挨个加回去就等于把同一个错抄三遍 ——
  // 所以这里只断言「按钮上没有 disabled」，不逐条枚举。
  assert.doesNotMatch(printButton(), /:disabled/)
})

t('也确认它不是换成了 v-if 之类的另一道门（该留的条件只有 v-if="voucherId"）', () => {
  const tag = printButton()
  const conds = [...tag.matchAll(/(?:^|\s)(v-if|v-show|disabled)="([^"]*)"/g)]
    .map((m) => `${m[1]}="${m[2]}"`)
  assert.deepEqual(conds, ['v-if="voucherId"'],
    `「打印预览」上不该有别的显示/可用性条件，实际有：${conds.join(' ') || '（无）'}`)
})

t('打开打印弹窗的函数不因只读而提前返回', () => {
  // printVoucher 的守卫只有「这张凭证在不在」和「账套/期间就绪没就绪」。
  // 曾经它在这种状态下也该能点，守卫里不能长出 posted / readonly 的判断。
  const fn = VIEW.slice(VIEW.indexOf('function printVoucher()'))
  const body = fn.slice(0, fn.indexOf('\n}'))
  assert.doesNotMatch(body, /posted|readonly|locked|canWrite/)
})

console.log('== 附件的读写还在（界面没了不等于字段没了）==')

t('载入时把附件数读回表单', () => {
  assert.match(VIEW, /attachments: v\.attachments/)
})

t('保存时把附件数原样送回去', () => {
  // 少这一句的症状是静默的：老凭证的附单据数还在库里显示（读取没坏），
  // 但下一次保存这一张就归零 —— 只有翻历史数据才发现。
  assert.match(VIEW, /attachments: form\.attachments/)
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
