import { http, getLatest } from './client'

// ---- 哪些端点改走 getLatest，以及为什么 ----
//
// 账簿三视图 + 报表都跟着期间下拉走，用户连点时前一趟必然还在途。
// 这几个端点改走 getLatest：同 key 的上一趟直接 abort，省掉白跑的往返。
// 键按端点分开 —— 翻总账不该把余额表那次一起掐掉。
//
// **凭证列表**（api/voucher.js 的 queryVouchers）也在这里，尽管它不住在这个文件里：
//   分页器的 `@current-change` / `@size-change` 与表头的 `toggleSort` 都能**连点**，
//   连点第 3、4 页 → 请求 A/B 并发 → B 先回、A 后回 →
//   **页脚停在第 4 页、表格里却是第 3 页的数据**（没有任何报错）。
//   所以它同样改走 getLatest('voucher/list', ...)，键与账簿三视图分开。
//
// getLatest 只解决「掐掉上一趟」这一半；另一半是**代次守卫**
// （composables/useLatestLoad.js）：abort 到达之前就已经 resolve 的那次照样会写状态。
// 两道防线配对使用，缺一不可。
export const balance = (bookId, params) =>
  getLatest('ledger/balance', `/books/${bookId}/ledger/balance`, params)
export const general = (bookId, params) =>
  getLatest('ledger/general', `/books/${bookId}/ledger/general`, params)
export const detail = (bookId, params) =>
  getLatest('ledger/detail', `/books/${bookId}/ledger/detail`, params)
/** 凭证录入页每行科目格的余额（借正贷负）。传 voucher_id（或 insert_after）
 *  是为了知道这张在期间里的**位置** —— 基数取的是「它之前」的账，不是整期期末。
 *  这条**不走** getLatest：VoucherEdit 里 loadBalances 已有自己的代次守卫
 *  （seq/balanceSeq），换过去会让「被取消」和「拉不到」走到同一个 catch，
 *  多一层要解释的耦合，而那边并不在这次要修的范围内。 */
export const voucherBalances = (bookId, params) =>
  http.get(`/books/${bookId}/ledger/voucher-balances`, { params }).then(r => r.data)