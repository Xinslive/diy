/**
 * 「只显示未入账发票」开着的时候，被忽略的那几张也要摊开**票面明细**
 * （src/views/voucher/InvoiceImport.vue）。
 *
 * 为什么要有：被忽略的发票同样没入账，正是要人工二次核对的那一批。以前
 * 展开它只有一句「已忽略」，没得看 —— 人工连「这张到底该不该入账」都
 * 判断不了。
 *
 * 两条纪律盯在这里：
 *
 *  ① 只摊**票面内容**（项目、金额、税额、税率）。被忽略的票还没分科目、
 *     没算凭证，摊「入账科目」和「凭证预览」是摊两样还没发生的事。
 *  ② 用**原生表格**而不是 el-table：一个批次几十张被忽略的，每张再挂一个
 *     el-table，组件开销会翻倍 —— 上一版把这张表从 47900 个 DOM 节点降到
 *     14372，靠的就是别让组件在这里堆起来。
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

console.log('== 被忽略的行也要摊开票面明细 ==')

t('摊开的条件是「被忽略」且「只显示未入账」', () => {
  assert.match(VIEW, /v-if="isIgnored\(row\) && onlyUnbooked"/)
})

t('用原生 table，不是 el-table', () => {
  const m = VIEW.match(/<table[^>]*class="ignored-line-table"[\s\S]*?<\/table>/)
  assert.ok(m, '找不到那张票面明细表')
  assert.doesNotMatch(m[0], /el-table/)
})

t('摊的是票面内容四列：项目、金额、税额、税率', () => {
  const m = VIEW.match(/<table[^>]*class="ignored-line-table"[\s\S]*?<\/table>/)
  assert.ok(m)
  for (const label of ['发票项目', '不含税金额', '税额', '税率']) {
    assert.ok(m[0].includes(label), `少了「${label}」这一列`)
  }
  assert.doesNotMatch(m[0], /入账科目/, '被忽略的票还没分科目，不该摊这一列')
})

t('逐行摊开，不是只给一个合计', () => {
  const m = VIEW.match(/<table[^>]*class="ignored-line-table"[\s\S]*?<\/table>/)
  assert.ok(m)
  assert.match(m[0], /v-for="\(line, index\) in row\.lines"/)
  assert.match(m[0], /lineAmount\(row, index\)/)
})

console.log('== 样式别忘了 ==')

t('有配套的样式', () => {
  assert.match(VIEW, /\.ignored-line-table \{/)
  assert.match(VIEW, /\.ignored-line-table \.num \{/)
})

console.log(`\n${pass} 通过，${fail} 失败`)
if (fail) process.exit(1)
