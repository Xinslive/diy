import { http, apiUrl } from './client'
export const templates = () => http.get('/reports/templates').then(r => r.data)
export const design = (id) => http.get(`/reports/${id}/design`).then(r => r.data)
export const render = (id, params) => http.get(`/reports/${id}/render`, { params }).then(r => r.data)
export const saveCells = (id, cells, bookId) => http.put(`/reports/${id}/cells`, cells, { params: { book_id: bookId } }).then(r => r.data)

// ---- 模板管理 ----
export const createTemplate = (payload) => http.post('/reports/templates', payload).then(r => r.data)
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
