import { http } from './client'
export const balance = (bookId, params) => http.get(`/books/${bookId}/ledger/balance`, { params }).then(r => r.data)
export const general = (bookId, params) => http.get(`/books/${bookId}/ledger/general`, { params }).then(r => r.data)
export const detail = (bookId, params) => http.get(`/books/${bookId}/ledger/detail`, { params }).then(r => r.data)
/** 凭证录入页每行科目格的余额（借正贷负）。voucherId 用来把这张自己剔掉。 */
export const voucherBalances = (bookId, params) =>
  http.get(`/books/${bookId}/ledger/voucher-balances`, { params }).then(r => r.data)
