import { http, apiUrl } from './client'
export const templates = () => http.get('/reports/templates').then(r => r.data)
export const design = (id) => http.get(`/reports/${id}/design`).then(r => r.data)
export const render = (id, params) => http.get(`/reports/${id}/render`, { params }).then(r => r.data)
// 公式语法错现在**当场 400**（以前是 200 存进去、等渲染时某一格蹦红字）。
// 整批拒收、一格不存，所以调用方要把 detail 弹给人看，并保留未保存的改动。
export const saveCells = (id, cells, bookId) => http.put(`/reports/${id}/cells`, cells, { params: { book_id: bookId } }).then(r => r.data)

// ---- 模板管理 ----
/**
 * 新建报表模板。
 *
 * `book_id` **不是可选项**（审计 P1-5，后端已 fail-closed）：报表模板是
 * 全局模板，所有账套共用一份口径，所以必须说清「你以哪个账套的身份改它」，
 * 并要求你在那个账套是 OWNER / 管理员。缺 book_id 一律 400。
 * （以前这里只 post payload、不带 book_id，等于拿一个匿名身份改全局口径。）
 */
export const createTemplate = (payload, bookId) =>
  http.post('/reports/templates', payload, bookId ? { params: { book_id: bookId } } : {}).then(r => r.data)
export const copyTemplate = (id, name, bookId) =>
  http.post(`/reports/templates/${id}/copy`, null, { params: { name, book_id: bookId } }).then(r => r.data)
export const updateTemplate = (id, payload, bookId) =>
  http.patch(`/reports/templates/${id}`, payload, { params: { book_id: bookId } }).then(r => r.data)
export const deleteTemplate = (id, bookId) =>
  http.delete(`/reports/templates/${id}`, { params: { book_id: bookId } }).then(r => r.data)
export const addRow = (id, payload, bookId) =>
  http.post(`/reports/templates/${id}/rows`, payload, { params: { book_id: bookId } }).then(r => r.data)
export const deleteRow = (id, rowNo, bookId) =>
  http.delete(`/reports/templates/${id}/rows/${rowNo}`, { params: { book_id: bookId } }).then(r => r.data)

// ---- 结构编辑：行 / 列 ----
// 注意 rows 和 cols 都会连带改写全表公式的单元格引用，服务端负责同步，
// 前端只管把「在哪一格插入」这个意图发过去。
export const updateRow = (id, rowNo, payload, bookId) =>
  http.patch(`/reports/templates/${id}/rows/${rowNo}`, payload, { params: { book_id: bookId } }).then(r => r.data)
export const addCol = (id, payload, bookId) =>
  http.post(`/reports/templates/${id}/cols`, payload, { params: { book_id: bookId } }).then(r => r.data)
export const updateCol = (id, colNo, payload, bookId) =>
  http.patch(`/reports/templates/${id}/cols/${colNo}`, payload, { params: { book_id: bookId } }).then(r => r.data)
export const deleteCol = (id, colNo, bookId) =>
  http.delete(`/reports/templates/${id}/cols/${colNo}`, { params: { book_id: bookId } }).then(r => r.data)
