import { http } from './client'
export const balance = (bookId, params) => http.get(`/books/${bookId}/ledger/balance`, { params }).then(r => r.data)
export const general = (bookId, params) => http.get(`/books/${bookId}/ledger/general`, { params }).then(r => r.data)
export const detail = (bookId, params) => http.get(`/books/${bookId}/ledger/detail`, { params }).then(r => r.data)
