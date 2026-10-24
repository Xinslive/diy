import { http } from './client'

export const getConfig = (bookId) => http.get(`/books/${bookId}/invoices/config`).then(r => r.data)
export const saveConfig = (bookId, payload) => http.put(`/books/${bookId}/invoices/config`, payload).then(r => r.data)
export const listRules = (bookId) => http.get(`/books/${bookId}/invoices/rules`).then(r => r.data)
export const createRule = (bookId, payload) => http.post(`/books/${bookId}/invoices/rules`, payload).then(r => r.data)
export const updateRule = (bookId, id, payload) => http.patch(`/books/${bookId}/invoices/rules/${id}`, payload).then(r => r.data)
export const deleteRule = (bookId, id) => http.delete(`/books/${bookId}/invoices/rules/${id}`).then(r => r.data)

export const getLimits = (bookId) => http.get(`/books/${bookId}/invoices/limits`).then(r => r.data)

/**
 * 一次传若干份 PDF，落成一个批次。非 PDF 会被后端忽略并在备注里点名。
 *
 * 第三个参数（文件名）不能省：从**文件夹**选来的 File 带 `webkitRelativePath`，
 * 浏览器会拿 `十月发票/10月8日.pdf` 这样的相对路径当 multipart 文件名发出去，
 * 于是批次标题和「来源文件」列都变成一串目录名。不传就是让浏览器自己决定。
 */
export const uploadBatch = (bookId, files) => {
  const fd = new FormData()
  for (const file of files) fd.append('files', file, file.name)
  return http.post(`/books/${bookId}/invoices/batches`, fd, { timeout: 660000 }).then(r => r.data)
}
export const listBatches = (bookId) => http.get(`/books/${bookId}/invoices/batches`).then(r => r.data)
export const getBatch = (bookId, id) => http.get(`/books/${bookId}/invoices/batches/${id}`).then(r => r.data)
export const previewBatch = (bookId, id, overrides = {}) =>
  http.post(`/books/${bookId}/invoices/batches/${id}/preview`, { overrides }).then(r => r.data)
/**
 * 把一张**解析失败**的发票重新解析一次。
 *
 * 走批次存下来的那份原始 PDF，只重跑那一页 —— 解析器修好之后不用把整个
 * 批次重传一遍（几百张票重传要几分钟，还可能撞上重复上传的拦截）。
 * 覆盖项一起提交：重新解析不该把用户已经做过的人工修正冲掉。
 */
export const reparseRecord = (bookId, batchId, recordId, overrides = {}) =>
  http.post(`/books/${bookId}/invoices/batches/${batchId}/records/${recordId}/reparse`, { overrides }, { timeout: 660000 }).then(r => r.data)
export const commitBatch = (bookId, id) => http.post(`/books/${bookId}/invoices/batches/${id}/commit`).then(r => r.data)
export const discardBatch = (bookId, id) => http.post(`/books/${bookId}/invoices/batches/${id}/discard`).then(r => r.data)
