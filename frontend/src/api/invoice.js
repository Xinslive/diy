import { http } from './client'

export const getConfig = (bookId) => http.get(`/books/${bookId}/invoices/config`).then(r => r.data)
export const saveConfig = (bookId, payload) => http.put(`/books/${bookId}/invoices/config`, payload).then(r => r.data)
export const listRules = (bookId) => http.get(`/books/${bookId}/invoices/rules`).then(r => r.data)
export const createRule = (bookId, payload) => http.post(`/books/${bookId}/invoices/rules`, payload).then(r => r.data)
export const updateRule = (bookId, id, payload) => http.patch(`/books/${bookId}/invoices/rules/${id}`, payload).then(r => r.data)
export const deleteRule = (bookId, id) => http.delete(`/books/${bookId}/invoices/rules/${id}`).then(r => r.data)

export const uploadBatch = (bookId, file) => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post(`/books/${bookId}/invoices/batches`, fd, { timeout: 660000 }).then(r => r.data)
}
export const listBatches = (bookId) => http.get(`/books/${bookId}/invoices/batches`).then(r => r.data)
export const getBatch = (bookId, id) => http.get(`/books/${bookId}/invoices/batches/${id}`).then(r => r.data)
export const previewBatch = (bookId, id, overrides = {}) =>
  http.post(`/books/${bookId}/invoices/batches/${id}/preview`, { overrides }).then(r => r.data)
export const commitBatch = (bookId, id) => http.post(`/books/${bookId}/invoices/batches/${id}/commit`).then(r => r.data)
export const discardBatch = (bookId, id) => http.post(`/books/${bookId}/invoices/batches/${id}/discard`).then(r => r.data)
