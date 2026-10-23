/**
 * ⚠️ **与后端 `ledger/engine.py` 的 `display_side` 约定相反，勿混用。**
 *
 * 引擎里的 ``display_side`` 只看符号（借正贷负那一套）；
 * 这里的前端 `displayBalance` 按**科目属性**翻正负 —— 两者对同一个数给出的
 * 正负号是**反的**。取哪一份要看清楚上下文，别把这里的数直接塞进试算平衡。
 * 这一点在本文件的说明里已经写过一遍，这里再摆到最顶上 —— 名字太像，
 * 拿错一次就是一个「借贷不平」的假象。
 */

/**
 * 凭证录入页科目格那个余额的**显示口径**：正负按科目的「余额方向」属性定，
 * 而不是按实际落在借方还是贷方。
 *
 *     借属性（资产 / 成本 / 费用）：借方余额为正、贷方为负   借正贷负
 *     贷属性（负债 / 权益 / 收入）：贷方余额为正、借方为负   借负贷正
 *
 * 为什么这么定：显示的是「这个科目按它的属性还剩多少」。
 * 600101 主营业务收入是贷属性，在借方挂了 3030 时显示 **-3030** ——
 * 一个负数直接说明「这个科目的余额跑到反方向去了」。
 * 若按实际借贷显示，同样的情况会是一个看着很正常的 +3030，
 * 录错方向这件事就被这个数字盖住了。
 *
 * **只用于显示。** 引擎里的 ``Figures.closing`` 始终是借正贷负，
 * 科目余额表 / 总账 / 试算平衡 / 报表一律用它，两套口径不要混：
 * 试算平衡是按符号正负累计借贷方的，拿这里的数去加会算不平。
 *
 * @param {string} direction 科目余额方向：``'DEBIT'`` 借 / ``'CREDIT'`` 贷
 * @param {number|string} signed 引擎给的借正贷负余额
 * @returns {number} 按余额方向定好正负的显示值
 *
 * **手上已经拿着整数分就别用这一份** —— 用下面的 `displayBalanceCents()`。
 * 这一份走浮点，金额一大就掉精度（凭证页的科目余额因此改用整数分那一版）。
 */
export function displayBalance(direction, signed) {
  const n = Number(signed)
  if (!Number.isFinite(n)) return 0
  return direction === 'CREDIT' ? -n : n
}

/**
 * `displayBalance` 的**整数分**版本 —— 同一条规则，换一种表示。
 *
 * 为什么要有两份：
 *   `displayBalance` 走浮点（Number），金额一大就掉精度；而前端内部的金额口径
 *   是**整数分**（BigInt，见 utils/amountGrid.js 的 moneyCents）。
 *   已经拿着整数分的时候，再用它去换算浮点，等于把精度主动扔掉 ——
 *   凭证页的科目余额以前就是这么在 18 位金额上差出 0.03 元的。
 *
 * **两条规则必须一模一样**（同方向翻正负、同一个 CREDIT 翻、其余不翻）。
 * 这份文件是这条规则的**唯一**出处 —— 别在别处再写一遍「贷属性要翻负」。
 *
 * @param {string} direction 科目余额方向：``'DEBIT'`` 借 / ``'CREDIT'`` 贷
 * @param {bigint} signedCents 引擎给的借正贷负余额，单位**分**
 * @returns {bigint} 按余额方向定好正负的显示值，单位分
 */
export function displayBalanceCents(direction, signedCents) {
  const n = typeof signedCents === 'bigint'
    ? signedCents
    : BigInt(Math.trunc(Number(signedCents) || 0))
  return direction === 'CREDIT' ? -n : n
}
