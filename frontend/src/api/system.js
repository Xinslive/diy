import { http } from './client'

export const users = () => http.get('/system/users').then(r => r.data)
export const createUser = (payload) => http.post('/system/users', payload).then(r => r.data)
export const updateUser = (id, payload) => http.patch(`/system/users/${id}`, payload).then(r => r.data)
export const setUserBooks = (id, books) => http.put(`/system/users/${id}/books`, books).then(r => r.data)
export const deleteUser = (id) => http.delete(`/system/users/${id}`).then(r => r.data)

export const allBooks = () => http.get('/system/books').then(r => r.data)

// 外部识别服务（发票 OCR）。token 只写不读：GET 回来的是 token_set 与末 4 位，
// 明文永远不在任何响应里。
export const getOcrConfig = () => http.get('/system/ocr').then(r => r.data)
export const saveOcrConfig = (payload) => http.put('/system/ocr', payload).then(r => r.data)

// 审计日志走 `@/api/book` 的 `auditLogs(bookId, limit)` —— 审计是**按账套**的，
// 而这里原来包的是 `/system/audit-logs`（不带 book_id），两个函数**同名不同义**。
// `AuditLog.vue` 实际 import 的是 `@/api/book`，这个从未被调用，留着只会让
// 下一个人 import 错的那个。已删。
