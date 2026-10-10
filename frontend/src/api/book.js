import { http, apiUrl } from './client'

export const listBooks = () => http.get('/books').then(r => r.data)
export const createBook = (payload) => http.post('/books', payload).then(r => r.data)
export const listPeriods = (id) => http.get(`/books/${id}/periods`).then(r => r.data)
export const setOpenings = (id, rows) => http.post(`/books/${id}/periods`, rows).then(r => r.data)
export const carryForward = (id, pid) => http.post(`/books/${id}/periods/${pid}/carry-forward`).then(r => r.data)
export const closePeriod = (id, pid) => http.post(`/books/${id}/periods/${pid}/close`).then(r => r.data)
export const unclosePreview = (id, pid) => http.get(`/books/${id}/periods/${pid}/unclose-preview`).then(r => r.data)
export const unclose = (id, pid, revertPosting = true) =>
  http.post(`/books/${id}/periods/${pid}/unclose`, { revert_posting: revertPosting }).then(r => r.data)
export const yearEnd = (id, newBookName) => http.post(`/books/${id}/year-end`, { new_book_name: newBookName || null }).then(r => r.data)
export const auditLogs = (id, limit = 100) => http.get(`/books/${id}/audit-logs`, { params: { limit } }).then(r => r.data)
