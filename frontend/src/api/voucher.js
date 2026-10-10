import { http, apiUrl } from './client'
export const queryVouchers = (bookId, params) => http.get(`/books/${bookId}/vouchers`, { params }).then(r => r.data)
export const getVoucher = (bookId, id) => http.get(`/books/${bookId}/vouchers/${id}`).then(r => r.data)
export const createVoucher = (bookId, payload) => http.post(`/books/${bookId}/vouchers`, payload).then(r => r.data)
export const updateVoucher = (bookId, id, payload) => http.put(`/books/${bookId}/vouchers/${id}`, payload).then(r => r.data)
export const deleteVoucher = (bookId, id) => http.delete(`/books/${bookId}/vouchers/${id}`).then(r => r.data)
export const postVouchers = (bookId, ids) => http.post(`/books/${bookId}/vouchers/post`, { ids }).then(r => r.data)
export const unpostVouchers = (bookId, ids) => http.post(`/books/${bookId}/vouchers/unpost`, { ids }).then(r => r.data)
export const batchDelete = (bookId, ids) => http.post(`/books/${bookId}/vouchers/batch-delete`, { ids }).then(r => r.data)
export const nextNo = (bookId, periodId) => http.get(`/books/${bookId}/ledger/voucher-no-preview`, { params: { period_id: periodId } }).then(r => r.data)

// ---- 整理凭证 ----
export const organizePreview = (bookId, payload) =>
  http.post(`/books/${bookId}/vouchers/organize/preview`, payload).then(r => r.data)
export const organize = (bookId, payload) =>
  http.post(`/books/${bookId}/vouchers/organize`, payload).then(r => r.data)
export const sortEntries = (bookId, id) =>
  http.post(`/books/${bookId}/vouchers/${id}/sort-entries`, { by: 'code' }).then(r => r.data)

// ---- Excel 导入 ----
export const importTemplateUrl = (bookId) => apiUrl(`/books/${bookId}/vouchers/import-template.xlsx`)
export const uploadImport = (bookId, file) => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post(`/books/${bookId}/vouchers/import`, fd).then(r => r.data)
}
export const listImports = (bookId) => http.get(`/books/${bookId}/vouchers/import`).then(r => r.data)
export const commitImport = (bookId, batchId) =>
  http.post(`/books/${bookId}/vouchers/import/${batchId}/commit`).then(r => r.data)
export const discardImport = (bookId, batchId) =>
  http.post(`/books/${bookId}/vouchers/import/${batchId}/discard`).then(r => r.data)
