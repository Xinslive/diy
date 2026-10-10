import { http, apiUrl } from './client'
import { ElMessage } from 'element-plus'

export const vouchersXlsx = (bookId, params) => apiUrl(`/books/${bookId}/print/vouchers.xlsx`, params)
export const balanceXlsx = (bookId, params) => apiUrl(`/books/${bookId}/print/balance.xlsx`, params)
export const generalXlsx = (bookId, params) => apiUrl(`/books/${bookId}/print/general-ledger.xlsx`, params)
export const reportXlsx = (bookId, templateId, periodId) =>
  apiUrl(`/books/${bookId}/print/report.xlsx`, { template_id: templateId, period_id: periodId })

// ---- HTML 预览（所见即所得，与打印同一份渲染）----
export const vouchersHtml = (bookId, params) => apiUrl(`/books/${bookId}/print/vouchers.html`, params)
export const balanceHtml = (bookId, params) => apiUrl(`/books/${bookId}/print/balance.html`, params)
export const generalHtml = (bookId, params) => apiUrl(`/books/${bookId}/print/general-ledger.html`, params)
export const detailHtml = (bookId, params) => apiUrl(`/books/${bookId}/print/detail-ledger.html`, params)
export const reportHtml = (bookId, params) => apiUrl(`/books/${bookId}/print/report.html`, params)

// ---- 打印模板 ----
export const templateMeta = (bookId) => http.get(`/books/${bookId}/print/templates/meta`).then(r => r.data)
export const templates = (bookId) => http.get(`/books/${bookId}/print/templates`).then(r => r.data)
export const templateDetail = (bookId, id) => http.get(`/books/${bookId}/print/templates/${id}`).then(r => r.data)
export const createTemplate = (bookId, payload) => http.post(`/books/${bookId}/print/templates`, payload).then(r => r.data)
export const updateTemplate = (bookId, id, payload) => http.put(`/books/${bookId}/print/templates/${id}`, payload).then(r => r.data)
export const deleteTemplate = (bookId, id) => http.delete(`/books/${bookId}/print/templates/${id}`).then(r => r.data)
export const copyTemplate = (bookId, id) => http.post(`/books/${bookId}/print/templates/${id}/copy`).then(r => r.data)


// ---------------------------------------------------------------------
// HTML 打印是「带鉴权取回 HTML -> 交给浏览器渲染」，而不是直接把 URL 丢给
// window.open / iframe.src。后者不会带 Authorization 头（token 存在
// localStorage，由 axios 拦截器注入），后端会直接返回 401。
// ---------------------------------------------------------------------

/** 取回打印页的 HTML 源码 */
export const fetchPrintHtml = (path, params = {}) =>
  http.get(path, { params, responseType: 'text' }).then((r) => r.data)

/** 在新窗口里打开打印页并唤起打印。内容与预览完全一致。 */
export async function openPrint(path, params = {}) {
  let html
  try {
    html = await fetchPrintHtml(path, params)
  } catch (e) {
    if (e.response?.data && typeof e.response.data === 'object' && e.response.data.detail) {
      ElMessage.error(e.response.data.detail)
    }
    return
  }
  const w = window.open('', '_blank')
  if (!w) {
    ElMessage.warning('浏览器拦截了新窗口，请允许弹出窗口后重试')
    return
  }
  w.document.open()
  w.document.write(html)
  w.document.close()
  // 等字体与布局稳定再唤起打印，否则 Chrome 偶尔会打出空白页
  setTimeout(() => {
    w.focus()
    w.print()
  }, 600)
}
