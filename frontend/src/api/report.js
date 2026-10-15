import { http, apiUrl, getLatest } from './client'
// 列表必须带 book_id：服务端要靠它判断「这张模板对当前账套可见吗」，
// 并按 code 去重（官方内置基线 vs 本账套副本并存时，副本优先）。
export const templates = (bookId) =>
  http.get('/reports/templates', { params: { book_id: bookId } }).then(r => r.data)
export const design = (id, bookId) =>
  http.get(`/reports/${id}/design`, { params: { book_id: bookId } }).then(r => r.data)

/**
 * 把一张报表复制一份归属当前账套（后端的「写时复制」）。
 *
 * 只有进入设计模式时才会真的复制：系统内置的资产负债表/利润表是所有账套
 * 共用的原版，直接改会立刻扩散到别的账套，所以先复制一份到本账套再改。
 * 已经在本账套的报表原样返回同一个 id，不做任何事 —— 反复进设计模式不会
 * 复制出一堆副本。
 */
export const forkTemplate = (id, bookId) =>
  http.post(`/reports/templates/${id}/fork`, null, { params: { book_id: bookId } }).then(r => r.data)
// 跟着期间/账套/模板走，切换时同样会连点 —— 走 getLatest 让上一趟直接 abort。
export const render = (id, params) => getLatest('report/render', `/reports/${id}/render`, params)
// 公式语法错现在**当场 400**（以前是 200 存进去、等渲染时某一格蹦红字）。
// 整批拒收、一格不存，所以调用方要把 detail 弹给人看，并保留未保存的改动。
export const saveCells = (id, cells, bookId) => http.put(`/reports/${id}/cells`, cells, { params: { book_id: bookId } }).then(r => r.data)

// ---- 模板管理 ----
/**
 * 新建报表模板。
 *
 * `book_id` **不是可选项**（审计 P1-5，后端已 fail-closed）：报表模板是
 * 全局模板，所有账套共用一份口径，所以必须说清「你以哪个账套的身份改它」，
 * 并要求你在那个账套是 OWNER / 管理员。缺 book_id 一律 400。
 * （以前这里只 post payload、不带 book_id，等于拿一个匿名身份改全局口径。）
 */
export const createTemplate = (payload, bookId) =>
  http.post('/reports/templates', payload, bookId ? { params: { book_id: bookId } } : {}).then(r => r.data)
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

// ---- 结构编辑：行 / 列 ----
// 注意 rows 和 cols 都会连带改写全表公式的单元格引用，服务端负责同步，
// 前端只管把「在哪一格插入」这个意图发过去。
export const updateRow = (id, rowNo, payload, bookId) =>
  http.patch(`/reports/templates/${id}/rows/${rowNo}`, payload, { params: { book_id: bookId } }).then(r => r.data)
export const addCol = (id, payload, bookId) =>
  http.post(`/reports/templates/${id}/cols`, payload, { params: { book_id: bookId } }).then(r => r.data)
export const updateCol = (id, colNo, payload, bookId) =>
  http.patch(`/reports/templates/${id}/cols/${colNo}`, payload, { params: { book_id: bookId } }).then(r => r.data)
export const deleteCol = (id, colNo, bookId) =>
  http.delete(`/reports/templates/${id}/cols/${colNo}`, { params: { book_id: bookId } }).then(r => r.data)

export const previewTemplateImport = (file, bookId) => {
  const form = new FormData()
  form.append('file', file)
  return http.post('/reports/templates/import/preview', form, { params: { book_id: bookId } }).then(r => r.data)
}
export const importTemplates = (templates, bookId, acceptWarnings = false) =>
  http.post('/reports/templates/import', { templates, accept_warnings: acceptWarnings },
    { params: { book_id: bookId } }).then(r => r.data)
