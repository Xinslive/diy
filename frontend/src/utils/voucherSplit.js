/**
 * 凭证拆分的本地计算：候选分界处、两半各自的合计、能不能拆。
 *
 * **服务端给的那份才是权威**（`GET /vouchers/{id}/split-preview`）：凭证号怎么发、
 * 平不平怎么判，都是后端的账 —— 它还要连号段顺带算一遍，页面算不了。
 * 这里的这份只管一件事：**弹窗刚打开、预览还没回来时，先把能切的位置摆出来**
 * （不至于空着）。
 *
 * 确认按钮**永远以服务端的判定为准**：本地算出来的 `valid` 只用来画线，
 * 不用来放行 —— 否则「本地与服务端口径不一致」就会变成一张不平的凭证落进库。
 *
 * **为什么这里自己算一遍合计，而不复用凭证编辑页那一段**
 * （`VoucherEdit.vue` 里的 `voucherTotals`）：那一段在 .vue 文件里，
 * `tests/voucher_balance.test.mjs` 按标记把它抠出来单测 —— 抠出来是为了测
 * 「页面上真在用的那份代码」；搬进 utils 就得改那份测试的断言，而那份测试
 * 守的是判平口径本身，不该为了复用去动它。
 *
 * 两处的规则逐字相同：
 *   · 展示口径 —— 红字算到对面，两栏只摆正数（`借 −300` 摆在贷方那一栏）；
 *   · 判平口径 —— 带符号的借方合计 = 带符号的贷方合计。
 * 这两个口径本身**等价**（移项即得），所以「平不平」的结论永远一致，
 * 差的只是红字凭证上摆出来的数。
 *
 * 全程整数分（BigInt）：18 位整数折成分是 20 位，早就越过 Number 的安全整数
 * 范围 —— 浮点会把 999999999999999999.99 变成 1000000000000000000（审查 P0-1）。
 */
import { moneyCents, centsToText } from './amountGrid.js'

/**
 * 一半分录的合计与判平。
 *
 * @returns {{lines:number, debit:string, credit:string, balanced:boolean, diff:string}}
 *   `debit` / `credit` 是**展示口径**（红字算到对面，两栏都是正数）；
 *   `diff` 是**带符号**的差额（借 − 贷），正数是借方多。
 */
export function halfTotals(entries) {
  const rows = entries || []
  let debit = 0n, credit = 0n      // 展示口径
  let sDebit = 0n, sCredit = 0n    // 带符号（判平用它）
  for (const e of rows) {
    const c = moneyCents(e.amount)
    if (e.direction === 'DEBIT') sDebit += c
    else sCredit += c
    if (c > 0n) {
      if (e.direction === 'DEBIT') debit += c
      else credit += c
    } else if (c < 0n) {
      // 红字：写到对面去
      if (e.direction === 'DEBIT') credit += -c
      else debit += -c
    }
  }
  return {
    lines: rows.length,
    debit: centsToText(debit),
    credit: centsToText(credit),
    balanced: sDebit === sCredit,
    diff: centsToText(sDebit - sCredit),
  }
}

/**
 * 全部候选分界处。
 *
 * 候选 = 相邻两条分录之间的缝，所以**分录数 - 1** 个。
 * 一张 n 条分录的凭证有 n-1 个切点，每个切点都要能看到「两半各自平不平」——
 * **不分的那些也在这里**（`valid: false`），由界面决定要不要摆出来。
 */
export function splitCandidates(entries) {
  const rows = entries || []
  const out = []
  for (let k = 1; k < rows.length; k++) {
    const first = halfTotals(rows.slice(0, k))
    const second = halfTotals(rows.slice(k))
    out.push({
      at_line: k,
      first,
      second,
      valid: first.balanced && second.balanced,
    })
  }
  return out
}
