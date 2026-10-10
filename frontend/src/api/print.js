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

/**
 * 按**草稿**模板渲染一页预览 HTML。
 *
 * 用 POST 而不是 GET：草稿是整个模板 JSON，塞不进 URL（而且模板名可能很长）。
 * 服务端只渲染不落库，所以可以一边调版式一边看效果。
 */
export const previewHtml = (bookId, payload) =>
  http.post(`/books/${bookId}/print/preview`, payload, { responseType: 'text' })
    .then((r) => r.data)

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

/**
 * 带鉴权下载文件。
 *
 * **不能用 `window.open(apiUrl(...))`**：token 存在 localStorage，由 axios 拦截器
 * 注入到请求头，`window.open` 发出去的裸请求没有 Authorization，后端直接 401，
 * 用户看到的是新标签页里一个 JSON 错误，而不是文件。
 */
export async function downloadFile(path, params = {}, fallbackName = '导出.xlsx') {
  let res
  try {
    res = await http.get(path, { params, responseType: 'blob' })
  } catch (e) {
    // 出错时 axios 抛异常，响应体也是 blob，得自己解
    const detail = await blobDetail(e?.response)
    ElMessage.error(detail || '导出失败')
    return
  }
  if ((res.headers['content-type'] || '').includes('application/json')) {
    ElMessage.error((await blobDetail(res)) || '导出失败')
    return
  }
  saveBlob(res.data, filenameOf(res) || fallbackName)
}

function blobDetail(res) {
  try {
    return JSON.parse(res.data.text())?.detail
  } catch {
    return null
  }
}

function filenameOf(res) {
  const cd = res.headers['content-disposition'] || ''
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd)
  if (star) return decodeURIComponent(star[1])
  const plain = /filename="?([^";]+)"?/i.exec(cd)
  return plain ? decodeURIComponent(plain[1]) : null
}

function saveBlob(data, name) {
  const url = URL.createObjectURL(new Blob([data]))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 2000)
}
