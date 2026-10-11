import { http, apiUrl } from './client'

export const listBooks = () => http.get('/books').then(r => r.data)
export const createBook = (payload) => http.post('/books', payload).then(r => r.data)
export const getBook = (id) => http.get(`/books/${id}`).then(r => r.data)
export const updateBook = (id, payload) => http.put(`/books/${id}`, payload).then(r => r.data)
export const listPeriods = (id) => http.get(`/books/${id}/periods`).then(r => r.data)
// 期初跟着顶栏那一期走：不传 period_id 就是启用期间
export const setOpenings = (id, rows, periodId) =>
  http.post(`/books/${id}/periods`, rows, { params: periodId ? { period_id: periodId } : {} }).then(r => r.data)
export const carryForwardInfo = (id, pid) =>
  http.get(`/books/${id}/periods/${pid}/carry-forward`).then(r => r.data)
// 结转损益永远是「有旧凭证就删掉、按最新余额重算」，replace 只为兼容旧调用
export const carryForward = (id, pid, replace = true) =>
  http.post(`/books/${id}/periods/${pid}/carry-forward`, { replace }).then(r => r.data)
export const closePeriod = (id, pid) => http.post(`/books/${id}/periods/${pid}/close`).then(r => r.data)
export const unclosePreview = (id, pid) => http.get(`/books/${id}/periods/${pid}/unclose-preview`).then(r => r.data)
export const unclose = (id, pid, revertPosting = true) =>
  http.post(`/books/${id}/periods/${pid}/unclose`, { revert_posting: revertPosting }).then(r => r.data)
// 年度结转：不再新建账套，只在同一个账套里追加下一年度
export const yearEnd = (id, fromYear, newYear) => http.post(`/books/${id}/year-end`, { from_year: fromYear || null, new_year: newYear || null }).then(r => r.data)
export const auditLogs = (id, limit = 100) => http.get(`/books/${id}/audit-logs`, { params: { limit } }).then(r => r.data)

export const getOpenings = (id, periodId) =>
  http.get(`/books/${id}/openings`, { params: periodId ? { period_id: periodId } : {} }).then(r => r.data)
export const parseOpenings = (id, fd) => {
  const h = { 'Content-Type': 'multipart/form-data' }
  return http.post(`/books/${id}/openings/parse`, fd, { headers: h }).then(r => r.data)
}
