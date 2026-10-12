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
 */
export function displayBalance(direction, signed) {
  const n = Number(signed)
  if (!Number.isFinite(n)) return 0
  return direction === 'CREDIT' ? -n : n
}
