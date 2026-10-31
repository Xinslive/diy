import { http } from './client'
export const subjectTree = (bookId, params) => http.get(`/books/${bookId}/subjects`, { params }).then(r => r.data)
export const subjectFlat = (bookId, params) => http.get(`/books/${bookId}/subjects/flat`, { params }).then(r => r.data)
/**
 * 新增科目。后端会按子级关系重算 level/is_leaf，不接受客户端指定。
 *
 * **年度必须走查询参数**（`?year=`）：接口是从查询参数读年度的，而调用方
 * （`SubjectFormDialog`）把年度放在 payload 里 —— 那一版请求体里的年度
 * **没人读**，后端退回「账套当前在记的那一年」，于是「在 2023 年的科目表上
 * 给 1123 建下级」被当成「2026 年的表里没有这个父科目」而拒掉
 * （2026-10-31 生产实测：账套「泉州劲松珠宝有限公司」四张科目表）。
 *
 * 这里从 payload 里把年度取出来补进查询参数 —— 调用方不用改，契约在这里收口。
 * 请求体里那份**也留着**：后端两处都收（查询参数优先），老前端/其它调用方
 * 只放请求体时也不会被丢掉。
 */
export const createSubject = (bookId, payload) => http.post(`/books/${bookId}/subjects`, payload,
  { params: payload?.year ? { year: payload.year } : {} }).then(r => r.data)
export const updateSubject = (bookId, id, payload) => http.patch(`/books/${bookId}/subjects/${id}`, payload).then(r => r.data)
export const deleteSubject = (bookId, id) => http.delete(`/books/${bookId}/subjects/${id}`).then(r => r.data)

/**
 * 外部系统科目表：先预览，确认时服务端重新解析同一文件并整表覆盖。
 *
 * **年度是必带的**：科目表每个年度各有一份（后端迁移 0032），
 * 2026 与 2027 各有自己的科目表。导入只影响**那一年** ——
 * 不带年度就会落到「这个账套当前在记的那一年」，而那一年可能
 * 已经有凭证（那样就被服务端挡下来了）。
 */
export const previewSubjectImport = (bookId, file, year) => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post(`/books/${bookId}/subjects/import/preview`, fd,
    { params: year ? { year } : {} }).then(r => r.data)
}
export const importSubjects = (bookId, file, year) => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post(`/books/${bookId}/subjects/import`, fd,
    { params: year ? { year } : {} }).then(r => r.data)
}
