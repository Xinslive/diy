/**
 * 凭证拆分（前端）：本地那份计算 + 界面上的几个接线点。
 *
 * ① **纯函数**（`src/utils/voucherSplit.js`）：用 vite 的 ssrLoadModule 加载
 *    **线上那份源码**跑，不复制逻辑 —— 与 `amount_grid.test.mjs` 同一个路子。
 *
 * ② **源码级断言**：工具栏上有没有那个按钮、它挂在哪些条件下、弹窗的确认
 *    按钮什么时候才放行、接口路径拼对没有。这些是「点起来才知道对不对」的事，
 *    但源码能查得更快也更准。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const {
  halfTotals, splitCandidates,
} = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/voucherSplit.js'))

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/VoucherEdit.vue', import.meta.url), 'utf8')
const DIALOG = fs.readFileSync(
  new URL('../src/components/VoucherSplitDialog.vue', import.meta.url), 'utf8')
const API = fs.readFileSync(
  new URL('../src/api/voucher.js', import.meta.url), 'utf8')
const AUDIT = fs.readFileSync(
  new URL('../src/views/system/AuditLog.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', String(e.message).split('\n')[0]); fail++ }
}

const D = (amount) => ({ direction: 'DEBIT', amount })
const C = (amount) => ({ direction: 'CREDIT', amount })

console.log('== 合计与判平（红字算到对面）==')

t('借 500 / 贷 0：不平，差额是借方多 500', () => {
  const s = halfTotals([D('500')])
  assert.equal(s.debit, '500.00')
  assert.equal(s.credit, '0.00')
  assert.equal(s.balanced, false)
  assert.equal(s.diff, '500.00')
})

t('红字（借 −300 / 贷 −300）是平的', () => {
  // 红字冲销是平的（凭证页那套口径），拆开后两半也各自得是平的
  const s = halfTotals([D('-300'), C('-300')])
  assert.equal(s.balanced, true)
  assert.equal(s.diff, '0.00')
  // 展示口径：红字搬到对面，两栏都是正数
  assert.equal(s.debit, '300.00')
  assert.equal(s.credit, '300.00')
})

t('正负相抵（借 111 / 借 −111）也算平', () => {
  const s = halfTotals([D('111'), D('-111')])
  assert.equal(s.balanced, true)
})

t('差额的方向要能算出来', () => {
  // 借方多 → diff 为正；贷方多 → diff 为负
  assert.equal(halfTotals([D('500')]).diff, '500.00')
  assert.equal(halfTotals([C('800')]).diff, '-800.00')
  assert.equal(halfTotals([D('100'), C('100')]).diff, '0.00')
})

console.log('== 候选分界处 ==')

t('四条分录有三个切点，只有中间那个两半都平', () => {
  const c = splitCandidates([D('5000'), C('5000'), D('3000'), C('3000')])
  assert.equal(c.length, 3)
  assert.deepEqual(c.map((x) => x.at_line), [1, 2, 3])
  assert.equal(c[1].valid, true)
  assert.equal(c[0].valid, false)   // 前半张只有借方
  assert.equal(c[2].valid, false)   // 后半张只有贷方
})

t('不平的切点也算得出来（画不画由界面决定）', () => {
  const c = splitCandidates([D('500'), D('800'), C('1300')])
  assert.equal(c.length, 2)
  assert.ok(c.every((x) => x.valid === false))
  assert.equal(c[0].first.debit, '500.00')
  assert.equal(c[0].second.debit, '800.00')
  assert.equal(c[0].second.credit, '1300.00')
})

t('只有一条分录时没有切点', () => {
  assert.equal(splitCandidates([D('100')]).length, 0)
  assert.equal(splitCandidates([]).length, 0)
})

console.log('== 凭证编辑页：工具栏上的「拆分凭证」==')

const toolbar = () => VIEW.slice(
  VIEW.indexOf('<div class="toolbar no-print">'), VIEW.indexOf('<div class="card">'))

t('工具栏上有这个按钮', () => {
  assert.match(toolbar(), /拆分凭证/)
})

t('它排在「增加凭证」之后、「打印预览」之前', () => {
  // 打印是**读**，不受「这张不能改」的三条限制影响；
  // 拆分是**写**，跟插入/增加归一组，排在打印前面才不误导
  const bar = toolbar()
  const add = bar.indexOf('增加凭证')
  const split = bar.indexOf('拆分凭证')
  const print = bar.indexOf('打印预览')
  assert.ok(add !== -1 && split !== -1 && print !== -1)
  assert.ok(add < split && split < print, '顺序不对')
})

t('按钮挂在 canSplit 上，而 canSplit 有三道闸', () => {
  assert.match(toolbar(), /:disabled="!canSplit"/)
  const m = VIEW.match(/const canSplit = computed\([\s\S]*?\)\)/)
  assert.ok(m, '源码里找不到 canSplit')
  const body = m[0]
  assert.match(body, /voucherId\.value/, '少了「已落库」这一道')
  assert.match(body, /readonly\.value/, '少了「这张不能改」这一道')
  assert.match(body, /length >= 2/, '少了「至少两条分录」这一道')
})

t('按钮灰着的时候会说清为什么灰', () => {
  assert.match(VIEW, /splitHint/)
  assert.match(VIEW, /反结账/, '已记账的提示要指名「期末结账 → 反结账」')
})

t('打开弹窗前先把没存上的改动冲掉', () => {
  // 这一页是自动保存的，拆的是**库里**那张 —— 改动没落库就拆，拆走的是旧内容
  const m = VIEW.match(/async function openSplit\(\)[\s\S]*?\n}/)
  assert.ok(m, '源码里找不到 openSplit')
  assert.match(m[0], /flushIfDirty\(\)/)
})

t('拆完要重载这一页（原凭证行数少了，新凭证在后面）', () => {
  const m = VIEW.match(/async function onSplit[\s\S]*?\n}/)
  assert.ok(m, '源码里找不到 onSplit')
  assert.match(m[0], /api\.splitVoucher/)
  assert.match(m[0], /await load\(\)/)
})

console.log('== 拆分弹窗 ==')

t('确认按钮在服务端说「可拆」之前不许点', () => {
  // 本地算的 valid 只用来画线；放行一律看服务端 ——
  // 两边口径不一致时，本地放行会推出一张不平的凭证落进库
  assert.match(DIALOG, /:disabled="!canConfirm"/)
  const m = DIALOG.match(/const canConfirm = computed\([\s\S]*?\)\n/)
  assert.ok(m, '源码里找不到 canConfirm')
  assert.match(m[0], /serverCandidate\.value\.valid/)
})

t('号怎么走写在两张卡片上', () => {
  // **原凭证继承它自己的号**，新凭证往后拿一个（用户 2026-10-21 定的）。
  // 用 `preview?.` 而不是 `preview.`：预览还没回来时（用户手快先点了本地候选），
  // 直接取属性会抛 TypeError，整个弹窗在渲染时炸掉。
  assert.match(DIALOG, /原凭证 \{\{ preview\?\.original_no/)
  assert.match(DIALOG, /新凭证 \{\{ preview\?\.created_no/)
  // 卡片上原凭证在前、新凭证在后 —— 原凭证是「留下来的那张」
  assert.ok(DIALOG.indexOf('原凭证 {{') < DIALOG.indexOf('新凭证 {{'))
})

t('不平的切点压根不画出来', () => {
  // 用户 2026-10-21 定的：拆不开的选项不要展示。
  // 会计自己知道切开了要平 —— 不平的切点摆在那儿只是噪音。
  assert.match(DIALOG, /\.filter\(\(c\) => c\.valid\)/)
  assert.doesNotMatch(DIALOG, /可拆/)
  assert.doesNotMatch(DIALOG, /借方多|贷方多/)
})

t('界面不再有那些絮絮叨叨的提醒', () => {
  // 「在分录之间选一个分界处……」「两张的日期与摘要沿用原凭证；
  // 附单据数留在原凭证，新凭证为 0。」—— 这些是规则，不是人要看的。
  assert.doesNotMatch(DIALOG, /在分录之间选一个分界处/)
  assert.doesNotMatch(DIALOG, /附单据数留在原凭证/)
  assert.doesNotMatch(DIALOG, /顺延是为了不撞号/)
})

console.log('== 接口与审计 ==')

t('api 里两个方法，路径拼对了', () => {
  assert.match(API, /splitPreview[\s\S]*?split-preview/)
  assert.match(API, /splitVoucher[\s\S]*?\/split`/)
})

t('审计页认得 SPLIT 这个动作', () => {
  assert.match(AUDIT, /SPLIT:/)
})

console.log('')
console.log(fail ? `通过 ${pass} / 失败 ${fail}` : `通过 ${pass} / 失败 0`)
process.exit(fail ? 1 : 0)
