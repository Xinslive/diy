/**
 * 幂等回放的判定。
 *
 * **为什么单独一个文件**：这个判断的正确性直接影响「回放之后要不要补一次 PUT」
 * —— 判断错了的后果是**界面显示的内容与库里不一致，而且不报错**
 * （人看着改过了，刷新一下又变回去）。
 *
 * 它住在 ``utils/`` 而不是 ``api/``，是因为 ``api/voucher.js`` 依赖
 * element-plus 与 ``@/config`` 别名 —— 测试环境里引不进来，
 * 而这个判断恰恰是最该被单测钉住的那一个。
 */

/**
 * 服务端说「这把键已经用过，这是回放」。
 *
 * axios 的响应头在浏览器里是 AxiosHeaders（``.get()``），
 * 在别的适配器里可能是普通对象 —— 两种都认。
 * 头名一律按小写取（HTTP 头名不区分大小写，axios 也统一小写）。
 *
 * @param {object|Headers|null} headers 响应头
 * @returns {boolean} true = 这是回放（服务端没有新建，返回的是上一次那张）
 */
export function isReplay(headers) {
  if (!headers) return false
  const value = typeof headers.get === 'function'
    ? headers.get('idempotent-replay')
    : (headers['idempotent-replay'] ?? headers['Idempotent-Replay'])
  // trim + 小写：HTTP 头值理论上不带首尾空白，但代理与网关可能加过；
  // 判断错了的后果是「回放之后不补 PUT」，不能因为一个空格就失效。
  return String(value ?? '').trim().toLowerCase() === 'true'
}
