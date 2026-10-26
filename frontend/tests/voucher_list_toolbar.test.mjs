/**
 * 凭证列表工具栏的两处改动（src/views/voucher/VoucherList.vue，**线上那份源码**）：
 *
 *  ① 「打印」默认**藏起来，勾了凭证才冒出来**（用户 2026-10-26 批注）；
 *  ② 「整理凭证」这个按钮**撤掉**（用户 2026-10-26 批注）。
 *
 * 两条都只看**源码**就下断言，因为它们说的都是「工具栏上有没有这个东西」——
 * 真跑起来点一遍只能证明点得动，证明不了它没被换成一个别的禁用条件。
 *
 * ① 那条还钉住两件容易被顺手改坏的事：
 *   - 打印范围在**点按钮的时候定格**。`PrintDialog` 盯着 `params` 变就重画预览，
 *     范围要是跟着 `selection` 一直变，勾选被清空时 `voucher_ids` 会整个消失，
 *     打印范围悄悄从「勾的那几张」变成「整个期间」—— 屏幕上看的是几张，
 *     纸上出来的是几百张，还没有一句提示。
 *   - 范围里**不许**再有「没勾选就打整个期间」那条退路：按钮本来就没勾选时
 *     不出现，留着它只会让人以为「不勾也能打」。
 *
 * ② 那条钉住的是「撤按钮 ≠ 撤功能」：整理页还在（#/vouchers/organize），
 *   断号/时序不对时上方那条提醒里的「去整理」还得能跳过去。
 *   顺手把整页删了，等于把「月结不了账时唯一的出路」也一起删了。
 *
 * 断言前先把 HTML 注释剥掉 —— 注释里写着「整理凭证」「打印」这些字，
 * 留着它们断言就成了在注释里找字，而不是在界面上找按钮。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const RAW = fs.readFileSync(
  new URL('../src/views/voucher/VoucherList.vue', import.meta.url), 'utf8')
const ROUTER = fs.readFileSync(
  new URL('../src/router/index.js', import.meta.url), 'utf8')

/** 剥掉 HTML 注释 —— 只剩真正会渲染出来的东西。 */
const VIEW = RAW.replace(/<!--[\s\S]*?-->/g, '')

/**
 * 工具栏那一段：从 `toolbar no-print` 到下面那条整理提醒之前。
 *
 * 边界在**原文**上切，切完再把注释剥掉 —— 注释里写着「整理凭证」「打印预览」
 * 这些字，剥晚了就在注释里找字，而不是在界面上找按钮；剥早了又找不到
 * 那条注释本身（剥掉之后就没有这句标记话了）。
 */
const toolbar = () => RAW.slice(
  RAW.indexOf('<div class="toolbar no-print">'),
  RAW.indexOf('<!-- 整理提醒搬到这里来了')
).replace(/<!--[\s\S]*?-->/g, '')

/** 「打印」那个按钮的整段标签。 */
const printButton = () => {
  const m = toolbar().match(/<el-button[^>]*printVouchers[^>]*>/)
  assert.ok(m, '工具栏里没有「打印」按钮')
  return m[0]
}

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 打印：默认藏着，勾了才出现 ==')

t('「打印」按钮挂在 selection.length 上（没勾选就不出现）', () => {
  assert.match(printButton(), /v-if="selection\.length"/,
    '「打印」按钮没有 v-if="selection.length" —— 没勾选时它也在界面上')
})

t('它不是换成了别的门（显示条件就这一个）', () => {
  const conds = [...printButton().matchAll(/(?:^|\s)(v-if|v-show|disabled)="([^"]*)"/g)]
    .map((m) => `${m[1]}="${m[2]}"`)
  assert.deepEqual(conds, ['v-if="selection.length"'],
    `「打印」上不该有别的显示/可用性条件，实际有：${conds.join(' ') || '（无）'}`)
})

t('「打印预览」那套「没勾选就打整个期间」的说法没了', () => {
  assert.doesNotMatch(toolbar(), /打印预览/,
    '工具栏里还有「打印预览」—— 按钮只勾选时出现，就不该再有整期预览这个说法')
})

t('按钮文案写死成「打印所选（N）」，范围和文案一致', () => {
  assert.match(toolbar(), /打印所选（{{ selection\.length }}）/,
    '按钮没有把勾选的张数写进文案 —— 看不出现这次要打几张')
})

t('范围在点按钮的时候定格，不是跟着 selection 一直变', () => {
  // 少了这行：勾选被清空 → voucher_ids 没了 → 范围悄悄变成整个期间。
  const fn = RAW.slice(RAW.indexOf('function printVouchers()'))
  const body = fn.slice(0, fn.indexOf('\n}'))
  assert.match(body, /printScope\.value\s*=\s*selection\.value\.map\(/,
    'printVouchers() 没有把勾选范围定格下来')
})

t('打印参数取的是定格的那份，不是活的 selection', () => {
  const fn = RAW.slice(RAW.indexOf('const printParams = computed('))
  const body = fn.slice(0, fn.indexOf('\n})'))
  assert.match(body, /printScope\.value/, 'printParams 还在直接读 selection')
  assert.doesNotMatch(body, /selection\.value/, 'printParams 还在直接读 selection')
})

t('没有「没勾选就退回整期打印」的退路', () => {
  const fn = RAW.slice(RAW.indexOf('const printParams = computed('))
  const body = fn.slice(0, fn.indexOf('\n})'))
  assert.doesNotMatch(body, /voucher_ids\s*:\s*[^,\n]*period|period_id[^,\n]*voucher_ids/,
    '打印参数里还有整期与所选混着走的写法')
  // 整期参数还在（打印页要拿期间上下文），但它不再决定「打哪几张」。
  assert.match(body, /period_id: store\.periodId/,
    '打印参数里 period_id 没了 —— 打印页的期间上下文没了')
})

console.log('== 整理凭证：撤按钮，不撤功能 ==')

t('工具栏里没有「整理凭证」按钮', () => {
  assert.doesNotMatch(toolbar(), /整理凭证/,
    '工具栏里还有「整理凭证」')
})

t('工具栏里也没有别的「去整理」入口混进来', () => {
  assert.doesNotMatch(toolbar(), /vouchers\/organize/,
    '工具栏里还有指向整理页的入口 —— 撤按钮撤干净')
})

t('整理页还在（撤按钮不是撤功能）', () => {
  assert.match(ROUTER, /path: 'vouchers\/organize'/,
    '整理页的路由没了 —— 断号时用户就没有出路了')
})

t('提醒里的「去整理」还跳得过去', () => {
  assert.match(VIEW, /vouchers\/organize/,
    '提醒里的「去整理」入口没了 —— 真需要整理的时候没有路可走')
})

t('Sort 图标不用了，import 也跟着撤', () => {
  const m = RAW.match(/import \{([^}]*)\} from '@element-plus\/icons-vue'/)
  assert.ok(m, '找不到图标 import')
  assert.doesNotMatch(m[1], /\bSort\b/,
    '图标 import 里还留着 Sort —— 它只被撤掉的那个按钮用过')
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
