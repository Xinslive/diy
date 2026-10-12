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
console.log(`通过 ${pass} / 失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
