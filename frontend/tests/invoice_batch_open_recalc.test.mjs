/**
 * 打开批次页就**自动重新计算一次**（用户 2026-10-29 批注）。
 *
 * **为什么。**
 *
 * 结算科目是按「金额 + 收支方向」去对账单流水上认的，而对账单常常是
 * **后来**才传进来的 —— 批次刚传完时还没有流水可认，结算科目只能回落
 * 到配置里那个。以前要用户自己点一次「重新计算预览」才认得出来，于是
 * 「打开批次 → 看到科目不对 → 想起来要点重算」成了每次的固定动作。
 *
 * 自动算过之后，页面上看到的就是最终会记到哪个科目上。
 *
 * 同一批批注还问了两件事：
 *
 *  ① 「当前显示未入账 N 张」「已加载 N 份对账单…」这两段灰字去掉；
 *  ② 「来源文件」那一列，为什么只有点了「重新计算预览」才看见 ——
 *     那一列看的是接口返回的 ``sources``，而批次详情接口以前**没带**它
 *     （只有预览接口带），所以它一直都在库里，只是没送到界面上。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const VIEW = fs.readFileSync(
  new URL('../src/views/voucher/InvoiceImport.vue', import.meta.url), 'utf8')

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK  ', name); pass++ }
  catch (e) { console.log('  FAIL', name, '—', e.message); fail++ }
}

console.log('== 打开批次自动重新计算 ==')

t('有一个自动重算的函数', () => {
  assert.match(VIEW, /async function autoRecalculate\(\)/)
})
t('打开批次就调用它', () => {
  const m = VIEW.match(/async function loadBatch\(row\) \{[\s\S]*?\n\}/)
  assert.ok(m, '找不到 loadBatch')
  assert.match(m[0], /await autoRecalculate\(\)/)
})

t('重算走的是预览接口，并且把人工覆盖项一起带上', () => {
  const m = VIEW.match(/async function autoRecalculate\(\) \{[\s\S]*?\n\}/)
  assert.ok(m, '找不到 autoRecalculate')
  assert.match(m[0], /invoiceApi\.previewBatch\(/)
  assert.match(m[0], /overridePayload\(\)/)
})

t('已作废的批次不重算（那个请求必然失败）', () => {
  const m = VIEW.match(/async function autoRecalculate\(\) \{[\s\S]*?\n\}/)
  assert.ok(m, '找不到 autoRecalculate')
  assert.match(m[0], /DISCARDED/)
})

t('重算失败不弹红条，也不挡住页面', () => {
  const m = VIEW.match(/async function autoRecalculate\(\) \{[\s\S]*?\n\}/)
  assert.ok(m, '找不到 autoRecalculate')
  assert.match(m[0], /catch/)
  // 失败时不能把 ElMessage 弹出来 —— 那是一条用户看不懂的报错。
  assert.doesNotMatch(m[0], /ElMessage/)
})

t('重算完把结果就地合并进去（不整批换掉）', () => {
  const m = VIEW.match(/async function autoRecalculate\(\) \{[\s\S]*?\n\}/)
  assert.ok(m, '找不到 autoRecalculate')
  assert.match(m[0], /mergeBatchPayload\(payload\)/)
})

console.log('== 两段提示去掉了 ==')

t('「当前显示未入账 N 张」不见了', () => {
  // 断言的是**模板里不再渲染它**，不是「这句话在文件里不出现」——
  // 注释里提一句「这句去掉了」是正常的。
  assert.doesNotMatch(VIEW, /\{\{\s*filterHint\s*\}\}/)
  assert.doesNotMatch(VIEW, /const filterHint = computed/)
})

t('「已加载 N 份对账单…」不见了', () => {
  assert.doesNotMatch(VIEW, /\{\{\s*statementHint\s*\}\}/)
  assert.doesNotMatch(VIEW, /const statementHint = computed/)
  assert.doesNotMatch(VIEW, /const statementCount = ref/)
  // 顺带把只为那句提示拉的那次请求也去掉了。
  assert.doesNotMatch(VIEW, /statementApi/)
})

console.log('== 来源文件那一列 ==')

t('列还在（多于一个来源文件时才摆出来）', () => {
  assert.match(VIEW, /v-if="batch\.sources\?\.length > 1" prop="source_file"/)
})

console.log(`\n${pass} passed, ${fail} failed`)
if (fail) process.exit(1)
