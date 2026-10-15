import { http, apiUrl } from './client'
export const queryVouchers = (bookId, params) => http.get(`/books/${bookId}/vouchers`, { params }).then(r => r.data)
export const getVoucher = (bookId, id) => http.get(`/books/${bookId}/vouchers/${id}`).then(r => r.data)
/** 新开一张时预填的凭证号 + 期间最后一张的日期。号码由后端发，和落库同源。 */
export const nextVoucher = (bookId, periodId) =>
  http.get(`/books/${bookId}/vouchers/next`, { params: periodId ? { period_id: periodId } : {} }).then(r => r.data)

/** 「插入凭证」的预填：插在谁后面、这张拿哪个号、谁要顺位。只算不改。 */
export const insertPreview = (bookId, afterId) =>
  http.get(`/books/${bookId}/vouchers/insert-preview`, { params: { after_id: afterId } }).then(r => r.data)

export const neighbour = (bookId, id, params) =>
  http.get(`/books/${bookId}/vouchers/${id}/neighbour`, { params }).then(r => r.data)
export const createVoucher = (bookId, payload) => http.post(`/books/${bookId}/vouchers`, payload).then(r => r.data)
export const updateVoucher = (bookId, id, payload) => http.put(`/books/${bookId}/vouchers/${id}`, payload).then(r => r.data)
export const deleteVoucher = (bookId, id) => http.delete(`/books/${bookId}/vouchers/${id}`).then(r => r.data)

// 记账 / 反记账不在这里 —— 走「期末结账」的结账与反结账（README 业务口径第 6 条）。
// 后端 `/vouchers/post` 与 `/vouchers/unpost` 两个端点仍在（余额表等处会用），
// 但前端没有、也不该在这里再包一层。

// ---- 整理凭证 ----
/** 单期间「整理就绪」体检（断号 + 时序），只读。列表页的提醒、记账/结账的闸门都看它。 */
export const voucherPeriodCheck = (bookId, periodId) =>
  http.get(`/books/${bookId}/vouchers/period-check`, { params: { period_id: periodId } }).then(r => r.data)
export const organizePreview = (bookId, payload) =>
  http.post(`/books/${bookId}/vouchers/organize/preview`, payload).then(r => r.data)
export const organize = (bookId, payload) =>
  http.post(`/books/${bookId}/vouchers/organize`, payload).then(r => r.data)

// ---- Excel 导入 ----
/**
 * 导入模板的 URL 构造器 —— **保留**，而且这个文件里所有调用点都该用它。
 *
 * 以前它零引用，于是 VoucherImport.vue 自己手拼了同一段路径
 * `/books/${bookId}/vouchers/import-template.xlsx`，然后忘了 import
 * `downloadFile`（运行时 ReferenceError，2026-10 修）。
 * 路径写两遍就是这类 bug 的成因：改后端路由时漏改一处，
 * 或者改了调用方式忘了改另一处。留一个单一来源比删掉它更防复发。
 */
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
