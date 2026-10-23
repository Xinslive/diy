export function fmt(v, digits = 2) {
  if (v === null || v === undefined || v === '') return ''
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/**
 * 整数分（BigInt）→ 金额文字：带千分位、两位小数。**全程整数，一个浮点都不碰。**
 *
 * 为什么要有这一份（`fmt` 是浮点的）：
 *   · `fmt` 走 Number，18 位整数在浮点里已经不是原来那个数了
 *     （999999999999999999.99 → 1000000000000000000）；
 *   · 而**整数分**是前端内部的金额口径（见 utils/amountGrid.js 的 moneyCents）。
 *     已经拿着整数分的时候，就不要再退回去走浮点 —— 一退就掉精度。
 *
 * 这一份是「整数分 → 显示文字」这一族里的**唯一**实现：
 *   · 凭证页的科目余额、页脚差额（VoucherEdit.vue）
 *   · 期初余额页的合计栏（OpeningBalance.vue）
 *   · 期初导入预览弹窗的金额（OpeningImportDialog.vue）
 * 以前这几处各写各的，同一个业务对象在两个界面上能显示成两个数
 * （1.005 在导入预览里显示 1.005，在期初页显示 1.00）。
 *
 * @param {bigint|number} cents 整数分。number 也收，但那是**整数**的分（不是元）。
 * @returns {string} 例如 "12,345,678.90" / "-0.05" / "0.00"
 */
export function moneyText(cents) {
  const n = typeof cents === 'bigint' ? cents : BigInt(Math.trunc(Number(cents) || 0))
  const neg = n < 0n
  const a = neg ? -n : n
  const yuan = (a / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${neg ? '-' : ''}${yuan}.${String(a % 100n).padStart(2, '0')}`
}

// 这里原来还有一个 `downloadUrl(path, params)`，与 `api/client.js` 的 `apiUrl`
// 实现几乎逐字重复（同为过滤空值拼 URLSearchParams，同返回 `/api${path}?${qs}`），
// 而且零引用。更关键的是：**拼出来的裸 URL 在浏览器里没有 Authorization**，
// 拿它去下载一律 401。要下载文件请用 `@/api/print` 的 `downloadFile()`。
// 已删 —— 留着只会让人挑了不能用的那个。
