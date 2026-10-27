/**
 * 发票导入的**兜底科目**与**按项目列支**（**线上那份源码**）。
 *
 * 用户提了三件事，这里各钉一条：
 *
 *  ① 科目规则里没法设置兜底科目 —— 进项发票什么奇怪的项目都有，
 *     配不出规则的那张发票以前是**整批都生成不了**；
 *  ② 「一张发票一张凭证」指的是一张凭证上**列支这张发票的所有项目**，
 *     同一张凭证里能看见买了哪几样；
 *  ③ **借方摘要就是发票上的项目名** —— 摘要写「发票 XXXXX」的话，
 *     翻凭证认不出是哪笔。
 *
 * 界面上必须能看出来：哪几行是系统替用户兜的底（要有人回头核对）、
 * 每条分录的摘要是什么。
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

console.log('== 兜底科目：进项、销项各一个 ==')

t('配置卡片里有进项、销项两个兜底科目下拉', () => {
  assert.match(VIEW, /<el-select[^>]*v-model="config\.input_fallback_subject_id"/)
  assert.match(VIEW, /<el-select[^>]*v-model="config\.output_fallback_subject_id"/)
  // 标签一律四个字，和「销项税额 / 进项贷方 / 销项借方」一致
  assert.match(VIEW, /label="进项兜底"/)
  assert.match(VIEW, /label="销项兜底"/)
})

t('自动制证配置里的标签都是四个字', () => {
  // 只圈「自动制证配置」那张卡里的表单 —— 下面明细区的「这张」是两字，
  // 它不在这一排里。
  const card = VIEW.slice(VIEW.indexOf('invoice-config-card'), VIEW.indexOf('</el-card>', VIEW.indexOf('invoice-config-card')))
  const labels = [...card.matchAll(/<el-form-item label="([^"]+)"/g)].map((m) => m[1])
  assert.ok(labels.length >= 6, labels.join('、'))
  for (const label of labels) {
    assert.equal(label.length, 4, `「${label}」不是四个字，和别的标签不齐`)
  }
})

t('config 里声明了这两个字段（saveConfig 提交整个 config）', () => {
  // 少声明一个字段 = 保存时把它清空。用户配好的兜底科目会莫名其妙地没了。
  assert.match(VIEW, /input_fallback_subject_id:\s*null/)
  assert.match(VIEW, /output_fallback_subject_id:\s*null/)
})

t('两个兜底科目分得开，不是同一个字段', () => {
  // 进项兜底记成本费用、销项兜底记收入，混用必有一边记错。
  const input = VIEW.match(/v-model="config\.input_fallback_subject_id"/)
  const output = VIEW.match(/v-model="config\.output_fallback_subject_id"/)
  assert.ok(input && output && input[0] !== output[0])
})

t('规则页签的提示里指路到兜底科目', () => {
  const rules = VIEW.slice(VIEW.indexOf('科目规则'), VIEW.indexOf('</el-tab-pane>', VIEW.indexOf('科目规则')))
  assert.match(rules, /兜底科目/)
})

t('规则列表把 BOTH 显示成中文', () => {
  assert.match(VIEW, /BOTH:\s*'进项和销项'/)
})

t('结算科目优先显示服务端计算出的实际科目', () => {
  assert.match(VIEW, /function settlementSubjectFor\(row\)/)
  assert.match(VIEW, /resolution\.entries\?\.find\(\(item\) => item\.direction === direction\)/)
})

t('发票类型、税额科目、结算科目与操作按钮保持一行', () => {
  assert.match(VIEW, /class="detail-toolbar-form"/)
  assert.match(VIEW, /flex-wrap: nowrap/)
  assert.match(VIEW, /overflow-x: auto/)
})

console.log('== 按项目列支：摘要就是发票的项目名 ==')

t('明细表的「发票项目」列显示 display_name', () => {
  // name 是解析出来的原名（规格型号/数量被 PDF 文字层粘在后面），
  // display_name 才是发票上印的那段。
  assert.match(VIEW, /function lineName\(line\) \{ return line\?\.display_name \|\| line\?\.name/)
  assert.match(VIEW, /\{\{ lineName\(line\) \}\}/)
})

t('凭证预览里每条分录都带摘要', () => {
  // 没有摘要的话，凭证预览只是一串「借 5602 30.00」，看不出买的是什么。
  assert.match(VIEW, /entry\.summary/)
})

t('落到兜底科目的行有标记', () => {
  // 兜底是「先记着、等人核对」，不是「已经对了」。不标出来没人会去核对。
  assert.match(VIEW, /function isFallbackLine\(row, index\)/)
  assert.match(VIEW, /isFallbackLine\(row, \$index\)/)
  assert.match(VIEW, /兜底</)
})

console.log('== 入账科目那一格：下拉与标记同一行 ==')

t('下拉不占满整格，留出「兜底」标记的位置', () => {
  // 占满了整个格子，「兜底」就被挤到第二行 —— 一个格子里两行字看着是散的。
  assert.match(VIEW, /style="width: 160px"/)
  assert.doesNotMatch(VIEW, /style="width: 100%"[^\n]*setLineSubject/)
})

console.log('== 金额：显示实际入账的那个数 ==')

t('明细表的金额显示的是实际入账金额，不是原数', () => {
  // 解析出来的原数与实际入账的数可能差在最后一项上那几分钱
  // （为了和发票合计对齐）。显示原数会让人以为凭证算错了。
  assert.match(VIEW, /function lineAmount\(row, index\)/)
  assert.match(VIEW, /resolution\?\.line_amounts\?\.\[String\(index\)\]/)
  assert.match(VIEW, /\{\{ lineAmount\(row, \$index\) \}\}/)
})

t('调整过金额时要说出来', () => {
  assert.match(VIEW, /row\.resolution\?\.rounding/)
  assert.match(VIEW, /各项目金额之和与发票金额一致/)
})

console.log(`\n${pass} 通过，${fail} 失败`)
if (fail) process.exit(1)
