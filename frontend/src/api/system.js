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

// 数据安全（账套备份到阿里云 OSS）。两个 AccessKey 项与 OCR 的 token 一样
// **只写不读**：GET 回来的是 *_set 与末 4 位，明文永远不在任何响应里。
export const getBackupConfig = () => http.get('/system/backup').then(r => r.data)
export const saveBackupConfig = (payload) => http.put('/system/backup', payload).then(r => r.data)
export const testBackupConnection = () => http.post('/system/backup/test').then(r => r.data)
// 立即备份是**后台**跑的（几本书要传几分钟，同步跑会把 HTTP 连接占住），
// 进度靠 getBackupRuns 轮询。
export const triggerBackup = () => http.post('/system/backup/run').then(r => r.data)
export const getBackupRuns = (limit = 50) => http.get('/system/backup/runs', { params: { limit } }).then(r => r.data)

// 审计日志走 `@/api/book` 的 `auditLogs(bookId, limit)` —— 审计是**按账套**的，
// 而这里原来包的是 `/system/audit-logs`（不带 book_id），两个函数**同名不同义**。
// `AuditLog.vue` 实际 import 的是 `@/api/book`，这个从未被调用，留着只会让
// 下一个人 import 错的那个。已删。
