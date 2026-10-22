/**
 * 总账页的**用友形态**（src/views/ledger/GeneralLedger.vue）。
 *
 *  公司现在用的是用友，迁到本系统之后用户与审计方认的是用友那张纸。
 * 总账因此不再是「一行一个科目」的余额表，而是**一个一级科目一份的月度台账**
 * （上年结转 → 每月「本月合计 / 累计」→ 结转下年）—— 屏幕上的样子要与打印件一致。
 *
 * 这些是**源码级**断言：盯住页面上那几处最容易悄悄退回去的地方。
 * 退回去的样子很具体 —— 列又变回九列的余额表、科目下拉自己查一遍科目表、
 * 打印时又只打屏幕上的那一个科目。哪一条都不报错，只是纸不对了。
 */
import assert from 'node:assert/strict'
import fs from 'node:fs'

const GENERAL = fs.readFileSync(
  new URL('../src/views/ledger/GeneralLedger.vue', import.meta.url), 'utf8')
const RANGE = fs.readFileSync(
  new URL('../src/utils/ledgerPrint.js', import.meta.url), 'utf8')
/** 只看模板部分：注释里提到「打印范围」不算，那个下拉得真的不在页面上。 */
const GENERAL_TPL = GENERAL.split('<script setup>')[0]

let pass = 0, fail = 0
const t = (name, fn) => {
  try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ }
}

console.log('== 表体是 8 列用友式 ==')
//
// 用友总账：月 / 日 / 凭证号数 / 摘要 / 借方 / 贷方 / 方向 / 余额，
// 两行表头（年份横跨「月+日」）。「凭证号数」「日」两列全程空白 ——
// 总账只印汇总行，这是用友的固定写法。

t('八列齐全', () => {
  for (const label of ['月', '日', '凭证号数', '摘要', '借方', '贷方', '方向', '余额']) {
    assert.match(GENERAL, new RegExp(`label="${label}"`), `缺「${label}」列`)
  }
})
t('不再退回余额表形态', () => {
  // 这几列是「一行一个科目」那版总账的形状。留着它们，用户看到的是
  // 屏幕上一张余额表、纸上另一张用友总账，两边对不上。
  for (const label of ['编码', '科目名称', '期初余额', '期末余额', '本期借方', '本年借方']) {
    assert.ok(!GENERAL.includes(`label="${label}"`), `还有余额表形态的「${label}」列`)
  }
})
t('月份与日期各占一列，摘要直接用行里的摘要', () => {
  assert.match(GENERAL, /prop="month"/, '缺月份列')
  assert.match(GENERAL, /prop="day"/, '缺日期列')
  assert.match(GENERAL, /prop="summary"/, '摘要没直接取行里的值')
})
t('方向列单独一列（余额方向是总账的关键一栏）', () => {
  assert.match(GENERAL, /label="方向"/)
  assert.match(GENERAL, /prop="direction"/)
})

console.log('')
console.log('== 单位是一级科目，不是一行一个科目 ==')

t('科目清单来自后端，不另查一遍科目表', () => {
  // 两处各算一遍「一级」迟早会对不上：对不上时用户看到的是空白下拉，
  // 或者选了一个后端根本不认的科目（404）。
  assert.match(GENERAL, /data\.value\.subjects/, '没取后端给的一级科目清单')
  assert.ok(!/subjectApi|subjectTree/.test(GENERAL), '页面自己去查科目表了')
})
t('选的是科目编码，不是 id', () => {
  // 科目表每个年度各有一份（后端迁移 0032）：2026 的 1001 与 2027 的 1001
  // 是两行不同的记录、id 也不同。跨年还认同一个科目，只能认编码。
  assert.match(GENERAL, /subjectCode/, '没按科目编码记选中项')
  assert.match(GENERAL, /v-model="subjectCode"/, '下拉绑的不是科目编码')
})
t('拿编码换当年那一份的 id', () => {
  assert.match(GENERAL, /function idOfCode/, '没有「编码 → 当年 id」的解析')
  assert.match(GENERAL, /subject_id: id \?\? undefined/, '取数时没带上解析出来的 id')
})

console.log('')
console.log('== 区间默认是整年 ==')
//
// 总账是**一本年账**：从 1 月的「上年结转」一路排到 12 月的「本年累计」。
// 默认只取当前一期的话，表里只有四行，用友那套行型一个都看不到。

t('起止默认取全年第一期与最后一期', () => {
  assert.match(GENERAL, /fromId\.value = pool\[0\]\.id/, '起点没落在全年第一期')
  assert.match(GENERAL, /toId\.value = pool\[pool\.length - 1\]\.id/, '止点没落在全年最后一期')
})
t('页内控件不改全局期间', () => {
  // 与明细账一致：本地 ref，不 setPeriod
  assert.ok(!/store\.setPeriod\(/.test(GENERAL), '总账页不该改全局期间')
})

console.log('')
console.log('== 打印与导出 ==')
//
// 打印范围（整册 / 仅当前科目）与「不打印无发生额科目」现在住在**打印预览
// 弹窗**里（用户 2026-10-22 批注 1、2）：工具栏上那个下拉已经删掉 ——
// 同一个设置只留一处，两处都能改的话迟早有一次是旧值。

t('工具栏上不再有打印范围下拉', () => {
  assert.ok(!/打印范围/.test(GENERAL_TPL), '工具栏上还有「打印范围」')
  assert.ok(!/ledger-scope-select/.test(GENERAL), '工具栏上还有那个下拉')
})
t('打印范围搬进打印预览弹窗', () => {
  assert.match(GENERAL, /:print-ranges="LEDGER_PRINT_RANGES"/)
  assert.match(GENERAL, /v-model:range="printRange"/)
  assert.match(GENERAL, /v-model:skip-empty="printSkipEmpty"/)
})
t('两档文案与口径共用一份（utils/ledgerPrint.js）', () => {
  // 总账与明细账是同一册结构，各写一份文案的话，用户会以为两本册子结构不同。
  assert.match(GENERAL, /from '@\/utils\/ledgerPrint'/)
  assert.match(RANGE, /本册（全部一级科目）/)
  assert.match(RANGE, /仅当前科目/)
})
t('默认是整册', () => {
  assert.match(GENERAL, /ref\(PRINT_RANGE_BOOK\)/)
})
t('「仅当前科目」才带 subject_id', () => {
  // 整册不带 subject_id —— 带了后端就只打一个科目，一整册打不出来。
  assert.match(GENERAL, /ledgerPrintParams\(/)
  assert.match(GENERAL, /subject_id: data\.value\.subject\.id/)
})
t('导出与打印共用同一份参数', () => {
  // 各写一遍迟早漏：漏的那一处就会出现「屏幕是一段、文件是全账」。
  assert.match(GENERAL, /\{ \.\.\.printParams\.value \}/, '导出没有复用打印参数')
})
t('打印与导出走的是总账通道', () => {
  assert.match(GENERAL, /print\/general-ledger\.html/)
  assert.match(GENERAL, /print\/general-ledger\.xlsx/)
})
t('不再传 leaf_only', () => {
  // 总账的单位是一级科目，这个参数早就没有意义了。
  assert.ok(!/leaf_only/.test(GENERAL), '还在传 leaf_only')
})

console.log('')
console.log('== 换账套/换年度后要重新取数 ==')

t('watcher 盯住 periods.length', () => {
  // store.loadBooks() 先灌 books 再灌 periods：currentYear 会先变成真值，
  // 那一刻 periods 还是空的。只盯年份的话 watcher 只响一次，刷新直接进本页时
  // 会停在空表上。
  assert.match(GENERAL, /watch\(\(\) => \[store\.bookId, store\.currentYear, store\.periods\.length\]/,
    'watcher 没盯住 periods.length')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
