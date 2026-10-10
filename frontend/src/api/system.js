import { http } from './client'

export const users = () => http.get('/system/users').then(r => r.data)
export const createUser = (payload) => http.post('/system/users', payload).then(r => r.data)
export const updateUser = (id, payload) => http.patch(`/system/users/${id}`, payload).then(r => r.data)
export const setUserBooks = (id, books) => http.put(`/system/users/${id}/books`, books).then(r => r.data)
export const deleteUser = (id) => http.delete(`/system/users/${id}`).then(r => r.data)

export const allBooks = () => http.get('/system/books').then(r => r.data)
export const auditLogs = (limit = 200) => http.get('/system/audit-logs', { params: { limit } }).then(r => r.data)
