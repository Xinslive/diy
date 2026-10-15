/**
 * 科目格余额的显示口径（utils/balanceSign.js）。
 *
 * 用户点名要的正负规则：按科目表那一列「余额方向」定，
 * 借属性借正贷负、贷属性借负贷正 —— 余额跑到反方向就显示负数。
 *
 * 同样用 vite 的 ssrLoadModule 加载**线上那份源码**，不复制逻辑（零新依赖）。
 */
import { createServer } from 'vite'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../', import.meta.url))
const { displayBalance } = await createServer({
  root: ROOT, logLevel: 'error', server: { middlewareMode: true }
}).then(s => s.ssrLoadModule('/src/utils/balanceSign.js'))

let pass = 0, fail = 0
const t = (name, fn) => { try { fn(); console.log('  OK   ' + name); pass++ }
  catch (e) { console.log('  FAIL ' + name + '\n       ' + String(e.message).split('\n')[0]); fail++ } }

const near = (got, want, msg) => assert.ok(Math.abs(got - want) < 1e-9,
  `${msg}：期望 ${want}，实际 ${got}`)

// ---- 借属性：资产 / 成本 / 费用，借正贷负 ----
t('1001 库存现金（借属性）借方余额显示正数', () => {
  near(displayBalance('DEBIT', 29963500), 29963500, '借方余额')
})
t('1001 库存现金（借属性）出现贷方余额显示负数', () => {
  near(displayBalance('DEBIT', -29963500), -29963500, '贷方余额')
})

// ---- 贷属性：负债 / 权益 / 收入，借负贷正 ----
t('2001 短期借款（贷属性）正常贷方余额显示正数', () => {
  near(displayBalance('CREDIT', -1000000), 1000000, '贷方余额')
})
t('600101 主营业务收入（贷属性）在借方有余额 → 显示负数', () => {
  // 这就是用户指的那一条：借属性看到正数会以为没事，按余额方向才看得出录反了
  near(displayBalance('CREDIT', 3030), -3030, '借方余额')
})
t('600101 主营业务收入（贷属性）正常余额显示正数', () => {
  near(displayBalance('CREDIT', -50000), 50000, '贷方余额')
})

// ---- 边界 ----
t('余额为 0 就是 0（正负随属性翻也不变）', () => {
  near(displayBalance('DEBIT', 0), 0, '借属性')
  near(displayBalance('CREDIT', 0), 0, '贷属性')
})
t('属性缺失 / 未知时按借正贷负（宁可不翻，也不把符号翻反）', () => {
  near(displayBalance(undefined, 1234), 1234, 'undefined 属性')
  near(displayBalance(null, 1234), 1234, 'null 属性')
  near(displayBalance('SOMETHING', 1234), 1234, '未知属性')
})
t('脏输入不炸：空串 / null / undefined / NaN 一律当 0', () => {
  for (const v of ['', null, undefined, NaN, 'abc']) {
    near(displayBalance('DEBIT', v), 0, `脏输入 ${JSON.stringify(v)}`)
    near(displayBalance('CREDIT', v), 0, `脏输入 ${JSON.stringify(v)}`)
  }
})
t('后端回来的字符串金额照样能翻（接口给的是字符串）', () => {
  near(displayBalance('CREDIT', '3030.00'), -3030, '字符串')
  near(displayBalance('DEBIT', '-29963800.00'), -29963800, '字符串')
})

// ---- 翻正负必须是线性的：翻(base + delta) == 翻(base) + 翻(delta) ----
// 否则余额显示会出现「基数翻了、增量没翻」的组合错误
t('翻正负对加法可分配（base 与本张分录不会各翻各的）', () => {
  const base = 36500, delta = -3030
  near(displayBalance('CREDIT', base + delta),
       displayBalance('CREDIT', base) + displayBalance('CREDIT', delta), '可分配性')
})

console.log('')
console.log('== 科目余额表自己带期间控件（源码级断言）==')
//
// 余额表这一页以前**没有**期间控件，期间全靠顶栏：同一组里的明细账、总账
// 都各有一个。用户在三页之间来回切，「看哪个月」在两页要换地方找，
// 很容易把上月的余额表当本月用。
//
// 口径跟那两页一致：**页内自选，不改顶栏的全局期间** —— 本地控件悄悄改
// 全局状态是用户想不到的副作用。

const BALANCE = fs.readFileSync(
  new URL('../src/views/ledger/BalanceSheet.vue', import.meta.url), 'utf8')
const GENERAL = fs.readFileSync(
  new URL('../src/views/ledger/GeneralLedger.vue', import.meta.url), 'utf8')

t('余额表工具栏有期间下拉', () => {
  assert.match(BALANCE, /v-model="periodId"/, '没有页内期间控件')
  assert.match(BALANCE, /store\.periodsOfYear\(/, '期间候选没跟总账/明细账用同一个口径')
})
t('取数、打印、导出三处都用同一个期间', () => {
  // 三处各写一个 store.periodId 的话，改控件时必然漏掉一处，
  // 表现是「表上是一月、打印出来是三月」
  const uses = (BALANCE.match(/period_id:\s*periodId\.value/g) || []).length
  assert.ok(uses >= 2, `只有 ${uses} 处用了页内期间，取数/打印/导出应共用`)
  assert.ok(!/period_id:\s*store\.periodId/.test(BALANCE),
    '还有地方在用顶栏的期间 —— 与页内控件不是同一个口径')
})
t('表头显示的是本页选中的那一期', () => {
  assert.ok(!/store\.currentPeriod\?\.code/.test(BALANCE),
    '表头还跟着顶栏走，页内换了期间表头不会变')
})
t('页内控件不去改顶栏的全局期间', () => {
  // 与总账/明细账一致：本地 ref，不 setPeriod
  assert.ok(!/store\.setPeriod\(/.test(BALANCE), '页内控件顺手改了全局期间')
  assert.ok(!/store\.setPeriod\(/.test(GENERAL), '总账也不该改（口径基准）')
})
t('换账套/换年度后，期间要归位到当前口径里', () => {
  // 不归位就会用新账套的 bookId 去查旧期间 id
  assert.match(BALANCE, /alignPeriod\(\)/, '没有归位逻辑')
  assert.match(BALANCE, /watch\(\(\) => \[store\.bookId, store\.currentYear\]/,
    '换账套/换年度后没有重新归位')
})

console.log('')
console.log('== 区间查询：屏幕、打印、导出必须是同一段 ==')
//
// 总账栽过一次「屏幕上是区间、文件里是全账」（审计第 7.3 节）。
// 余额表加区间时最容易把同一个毛病再犯一遍：屏幕支持起止，导出还只认止期。
// 用户不会以为导出坏了，只会把纸上那一份当成对的。

t('有本期 / 区间两种模式，区间多一个起点', () => {
  assert.match(BALANCE, /value="range"/, '没有区间模式')
  assert.match(BALANCE, /v-model="fromId"/, '区间没有起点选择')
})
t('区间发生额那两列要改名字', () => {
  // 字段还叫 period_debit，里面装的是整段的发生额；
  // 列名不跟着变，用户会把「区间借方」读成「这个月借方」
  assert.match(BALANCE, /区间借方/, '区间模式下没有改列名')
  assert.match(BALANCE, /moveDebitLabel/, '列名没有跟着模式走')
})
t('区间跨年时「本年」两列不显示', () => {
  // 2026-11 ~ 2027-02 时「本年」是哪个年都不成立，给一个语义不对的数
  // 比不给这一列更糟
  const ytd = BALANCE.match(/<el-table-column[^>]*label="本年借方"[\s\S]{0,400}?<\/el-table-column>/g) || []
  assert.ok(ytd.length, '找不到本年两列')
  for (const col of ytd) assert.match(col, /v-if="!isRange"/, '本年列在区间模式下仍然显示')
})
t('起点晚于止期时要把止期一起带过去', () => {
  // 不挡的话算出来是一段「倒着走」的区间：期初比期末还晚，数字能看不能信
  assert.match(BALANCE, /onFromChange/, '没有处理起止倒置')
  assert.match(BALANCE, /from\.ord > to\.ord/, '没有比较期间先后')
})
t('区间必须一路带到打印和导出', () => {
  assert.match(BALANCE, /from_period_id: fromId\.value/, '取数没带区间起点')
  // 打印与导出共用 printParams，带一次就够 —— 各写一遍迟早漏
  assert.match(BALANCE, /\{ \.\.\.printParams\.value \}/, '导出没有复用打印参数')
})

console.log('')
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
