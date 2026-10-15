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
/**
 * 结账 —— **必须带期间版本号**（乐观锁）。
 *
 *  两个账号各开着一个页面，A 先结了账，B 那边还拿着「未结账 + 版本 1」。
 *  不带版本号时后端按 B 的陈旧状态照做（他那张屏幕上确实写着未结账），
 *  带着版本号时后端比对库里那一份：不一致就 409 拒掉，两边都落到同一个事实上。
 *
 *  这条以前不在本文件里：PeriodPanel.vue 自己 import 了共享 http 手写路径，
 *  理由是「closePeriod 还没有 version 参数」。现在有了 ——
 *  路径写在两个文件里，改后端路由时必然漏改一处。
 */
export const closePeriod = (id, pid, version) =>
  http.post(`/books/${id}/periods/${pid}/close`, { version }).then(r => r.data)
export const unclosePreview = (id, pid) => http.get(`/books/${id}/periods/${pid}/unclose-preview`).then(r => r.data)
/** 反结账。同样带版本号，口径与结账一致。 */
export const unclose = (id, pid, version, revertPosting = true) =>
  http.post(`/books/${id}/periods/${pid}/unclose`, { revert_posting: revertPosting, version }).then(r => r.data)
// 年度结转：不再新建账套，只在同一个账套里追加下一年度
export const yearEnd = (id, fromYear, newYear) => http.post(`/books/${id}/year-end`, { from_year: fromYear || null, new_year: newYear || null }).then(r => r.data)
export const auditLogs = (id, limit = 100) => http.get(`/books/${id}/audit-logs`, { params: { limit } }).then(r => r.data)

export const getOpenings = (id, periodId) =>
  http.get(`/books/${id}/openings`, { params: periodId ? { period_id: periodId } : {} }).then(r => r.data)
export const parseOpenings = (id, fd) => {
  const h = { 'Content-Type': 'multipart/form-data' }
  return http.post(`/books/${id}/openings/parse`, fd, { headers: h }).then(r => r.data)
}
