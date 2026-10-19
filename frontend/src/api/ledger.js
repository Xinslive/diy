import { http, getLatest } from './client'

// 账簿三视图 + 报表都跟着期间下拉走，用户连点时前一趟必然还在途。
// 这几个端点改走 getLatest：同 key 的上一趟直接 abort，省掉白跑的往返。
// 键按端点分开 —— 翻总账不该把余额表那次一起掐掉。
export const balance = (bookId, params) =>
  getLatest('ledger/balance', `/books/${bookId}/ledger/balance`, params)
export const general = (bookId, params) =>
  getLatest('ledger/general', `/books/${bookId}/ledger/general`, params)
export const detail = (bookId, params) =>
  getLatest('ledger/detail', `/books/${bookId}/ledger/detail`, params)
/** 凭证录入页每行科目格的余额（借正贷负）。voucherId 用来把这张自己剔掉。
 *  这条**不走** getLatest：VoucherEdit 里 loadBalances 已有自己的代次守卫
 *  （seq/balanceSeq），换过去会让「被取消」和「拉不到」走到同一个 catch，
 *  多一层要解释的耦合，而那边并不在这次要修的范围内。 */
export const voucherBalances = (bookId, params) =>
  http.get(`/books/${bookId}/ledger/voucher-balances`, { params }).then(r => r.data)