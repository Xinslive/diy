import { http } from './client'

/**
 * 流水对账接口。
 *
 * **与「发票导入」是同级的两个功能**，不是父子关系：发票导入读的是发票，
 * 流水对账读的是银行对账单；两边通过「发票的结算科目」这一件事互相配合
 * （发票按「金额 + 收支方向」命中流水后，结算科目自动跟到那个账户上）。
 */

export const listStatements = (bookId) =>
  http.get(`/books/${bookId}/bank-statements`).then(r => r.data)

export const getStatement = (bookId, id) =>
  http.get(`/books/${bookId}/bank-statements/${id}`).then(r => r.data)

export const listRows = (bookId, id) =>
  http.get(`/books/${bookId}/bank-statements/${id}/rows`).then(r => r.data)

/**
 * 上传一份对账单。
 *
 * `accountSubjectId` 是这份对账单**属于哪个现金科目** —— 1002 下面挂了
 * 几个子科目（基本户、一般户……）时，选哪个就是这份流水记到哪个账户上。
 */
export const uploadStatement = (bookId, file, accountSubjectId) => {
  const fd = new FormData()
  fd.append('file', file, file.name)
  fd.append('account_subject_id', String(accountSubjectId))
  return http.post(`/books/${bookId}/bank-statements`, fd, { timeout: 120000 }).then(r => r.data)
}

/**
 * 重新解析。
 *
 * 带 `columnMapping` 就是**按人工指定的列重跑**（自动映射认不出来时那条路）；
 * 不带就是重读原件按最新规则自动重跑。
 */
export const reparse = (bookId, id, payload = {}) =>
  http.post(`/books/${bookId}/bank-statements/${id}/reparse`, payload, { timeout: 120000 }).then(r => r.data)

/**
 * 下载原件（带鉴权）。
 *
 * **不能用 `window.open(拼出来的 URL)`**：token 存在 localStorage，由 axios
 * 拦截器注入到请求头，裸请求没有 Authorization，后端直接 401 ——
 * 用户看到的是新标签页里一个 JSON 错误，而不是文件。
 */
export async function downloadOriginal(bookId, id, fallbackName = '对账单') {
  let res
  try {
    res = await http.get(`/books/${bookId}/bank-statements/${id}/file`, { responseType: 'blob' })
  } catch (e) {
    const detail = await blobDetail(e?.response)
    throw new Error(detail || '下载失败')
  }
  if ((res.headers['content-type'] || '').includes('application/json')) {
    const detail = await blobDetail(res)
    throw new Error(detail || '下载失败')
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

export const deleteStatement = (bookId, id) =>
  http.delete(`/books/${bookId}/bank-statements/${id}`).then(r => r.data)

export const patchRow = (bookId, rowId, payload) =>
  http.patch(`/books/${bookId}/bank-statements/rows/${rowId}`, payload).then(r => r.data)

export const setRowDirection = (bookId, rowId, direction) =>
  http.post(`/books/${bookId}/bank-statements/rows/${rowId}/direction`, { direction }).then(r => r.data)

export const planVouchers = (bookId, id) =>
  http.post(`/books/${bookId}/bank-statements/${id}/plan-vouchers`).then(r => r.data)

export const commitVouchers = (bookId, id) =>
  http.post(`/books/${bookId}/bank-statements/${id}/commit-vouchers`, {}, { timeout: 120000 }).then(r => r.data)

// ---- 流水科目规则 ----
export const listRules = (bookId) =>
  http.get(`/books/${bookId}/bank-statement-rules`).then(r => r.data)
export const createRule = (bookId, payload) =>
  http.post(`/books/${bookId}/bank-statement-rules`, payload).then(r => r.data)
export const updateRule = (bookId, id, payload) =>
  http.patch(`/books/${bookId}/bank-statement-rules/${id}`, payload).then(r => r.data)
export const deleteRule = (bookId, id) =>
  http.delete(`/books/${bookId}/bank-statement-rules/${id}`).then(r => r.data)

// ---- 格式画像（记住某种对账单的列映射） ----
export const listProfiles = (bookId) =>
  http.get(`/books/${bookId}/bank-statement-profiles`).then(r => r.data)
export const saveProfile = (bookId, payload) =>
  http.post(`/books/${bookId}/bank-statement-profiles`, payload).then(r => r.data)
export const deleteProfile = (bookId, id) =>
  http.delete(`/books/${bookId}/bank-statement-profiles/${id}`).then(r => r.data)
