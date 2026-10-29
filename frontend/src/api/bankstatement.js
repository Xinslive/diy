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

/**
 * 改一行流水的**对方科目**。
 *
 * `planSubjects` 传数组 = 整组换掉（可能好几个科目，每个带自己的金额，
 * 合计必须等于流水金额）；传 `null` = 清空。`planSubjectId` 是「就一个
 * 科目」的简写（金额自动取流水金额），两种不能同时给。
 */
export const patchRow = (bookId, rowId, payload) =>
  http.patch(`/books/${bookId}/bank-statements/rows/${rowId}`, payload).then(r => r.data)

/**
 * 把一条流水对应的那条分录的**科目**改掉（记在 1001 上的那笔 → 本账户）。
 *
 * **只改现金那一侧**，对方侧不动。走的是凭证编辑那条路，所以已记账 /
 * 已结账的凭证会被挡下（400），并发改动会回 409。
 */
export const reclassifyRow = (bookId, rowId, entryId) =>
  http.post(`/books/${bookId}/bank-statements/rows/${rowId}/reclassify`,
    { entry_id: entryId }).then(r => r.data)

export const setRowDirection = (bookId, rowId, direction) =>
  http.post(`/books/${bookId}/bank-statements/rows/${rowId}/direction`, { direction }).then(r => r.data)

/**
 * 「可制证清单」—— 这一份对账单能生成哪些凭证。
 *
 * **标了 ``silent``**（2026-10-28）：这个接口**会写库**（持久化去重标记），
 * 所以后端给它挂了写权限，只读账号调用必然 403。而只读账号打开「流水明细」
 * 时，**流水行其实好好地显示着** —— 那个 403 只是一条**假错误**。
 * 标上 ``silent``，失败时拦截器就不出声，由调用方自己降级
 * （见 ``FlowReconciliation.vue`` 的 ``loadPlan``）。
 */
export const planVouchers = (bookId, id) =>
  http.post(`/books/${bookId}/bank-statements/${id}/plan-vouchers`, {}, { silent: true })
    .then(r => r.data)

export const commitVouchers = (bookId, id) =>
  http.post(`/books/${bookId}/bank-statements/${id}/commit-vouchers`, {}, { timeout: 120000 }).then(r => r.data)

/**
 * **人工复核：判「是同一笔」** → 挂上那张凭证，这一行变成「已入账」。
 *
 * **什么时候需要它**：账上有同一笔（金额相等、方向对应），但**日期对不上**
 * —— 常见的是账上记的是发票日期、对账单是实际支付日期，差个十来天。
 * 这种机器不敢替人认定（认错了就是钱记两遍），所以报出来让人点一下。
 *
 * **服务端会核实那张凭证上真有对得上的分录** —— 挂一张别的凭证上去会被拒。
 */
export const confirmRowReview = (bookId, rowId, voucherId) =>
  http.post(`/books/${bookId}/bank-statements/rows/${rowId}/review/confirm`,
    { voucher_id: voucherId }).then(r => r.data)

/**
 * **人工复核：判「不是同一笔」** → 放行，这一行回到「可制证」。
 *
 * **服务端会记下否掉的是哪一张凭证** —— 不记的话下次重算又把它报一遍，
 * 用户会以为刚才那一下没生效。
 */
export const dismissRowReview = (bookId, rowId, voucherId) =>
  http.post(`/books/${bookId}/bank-statements/rows/${rowId}/review/dismiss`,
    { voucher_id: voucherId }).then(r => r.data)

/**
 * **存疑凭证**：账上有、对账单里找不到对应流水的那些凭证。
 *
 * 流水对账的另一半：`planVouchers` 那条路是「对账单上有、账上没有」→
 * 生成凭证；这一路是「账上有、对账单上没有」→ 那是**错账**。
 *
 * **只读接口**：它是给人找错账的清单，不改任何东西 —— 只读账号也能看。
 */
export const getSuspiciousVouchers = (bookId) =>
  http.get(`/books/${bookId}/bank-statements/suspicious-vouchers`).then(r => r.data)

// ---- 账套级配置 ----
//
// **账套级摘要模板已下线（2026-10-28）** —— 一律用内置口径。
// 这两个函数留着是为了**还能拿到一句明确的报错**（而不是静默丢掉字段）：
// 老客户端继续发 `summary_template` 时，服务层会回 400 并说清新口径。
export const getConfig = (bookId) =>
  http.get(`/books/${bookId}/bank-statement-config`).then(r => r.data)
export const saveConfig = (bookId, payload) =>
  http.put(`/books/${bookId}/bank-statement-config`, payload).then(r => r.data)

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
