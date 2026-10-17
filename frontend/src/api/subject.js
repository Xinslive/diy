import { http } from './client'
export const subjectTree = (bookId, params) => http.get(`/books/${bookId}/subjects`, { params }).then(r => r.data)
export const subjectFlat = (bookId, params) => http.get(`/books/${bookId}/subjects/flat`, { params }).then(r => r.data)
/** 新增科目。后端会按子级关系重算 level/is_leaf，不接受客户端指定。 */
export const createSubject = (bookId, payload) => http.post(`/books/${bookId}/subjects`, payload).then(r => r.data)
export const updateSubject = (bookId, id, payload) => http.patch(`/books/${bookId}/subjects/${id}`, payload).then(r => r.data)
export const deleteSubject = (bookId, id) => http.delete(`/books/${bookId}/subjects/${id}`).then(r => r.data)

/** 外部系统科目表：先预览，确认时服务端重新解析同一文件并整表覆盖。 */
export const previewSubjectImport = (bookId, file) => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post(`/books/${bookId}/subjects/import/preview`, fd).then(r => r.data)
}
export const importSubjects = (bookId, file) => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post(`/books/${bookId}/subjects/import`, fd).then(r => r.data)
}
