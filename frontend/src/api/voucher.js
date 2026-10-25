import { http, apiUrl, getLatest } from './client'
import { isReplay } from '@/utils/idempotency'

/**
 * 凭证列表：翻页 / 改页大小 / 换排序都会重发一趟，而这几处控件是**可以连点**的
 * （分页器的 `@current-change`、表头的 `toggleSort`）。
 * 连点第 3、4 页 → 请求 A/B 并发 → B 先回、A 后回 →
 * **页脚停在第 4 页、表格里却是第 3 页的数据**。
 *
 * 所以这一条改走 getLatest：同一把 key 的上一趟直接 abort，
 * 配合页面上的代次守卫（composables/useLatestLoad.js），
 * 两道防线缺一不可 —— 只有 abort 挡不住「abort 到达之前就已经 resolve」的那次。
 * 键按端点分开：翻凭证列表不该把账簿三视图那次一起掐掉。
 */
export const queryVouchers = (bookId, params) =>
  getLatest('voucher/list', `/books/${bookId}/vouchers`, params)
export const getVoucher = (bookId, id) => http.get(`/books/${bookId}/vouchers/${id}`).then(r => r.data)
/** 新开一张时预填的凭证号 + 期间最后一张的日期。号码由后端发，和落库同源。 */
export const nextVoucher = (bookId, periodId) =>
  http.get(`/books/${bookId}/vouchers/next`, { params: periodId ? { period_id: periodId } : {} }).then(r => r.data)

/** 插入位的预填：插在谁前面/后面、这张拿哪个号、谁要顺位。只算不改。
 *  position：before = 向前插入（占参照自己的号），after = 向后插入（拿参照号 +1）。 */
export const insertPreview = (bookId, afterId, position = 'before') =>
  http.get(`/books/${bookId}/vouchers/insert-preview`,
    { params: { after_id: afterId, position } }).then(r => r.data)

export const neighbour = (bookId, id, params) =>
  http.get(`/books/${bookId}/vouchers/${id}/neighbour`, { params }).then(r => r.data)
/**
 * 新建凭证。**带幂等键**（可选）。
 *
 * 幂等键让「请求发出去了、响应丢了」这件事**安全**：服务端按这把键去重，
 * 重复的请求落到同一张凭证上，而不会在库里多出一张几乎一样的
 * （审计 §3.28 —— 没有它的时候，重试一次就是两张凭证）。
 *
 * **回放（replayed）是什么意思。** 服务端发现这把键已经用过了，
 * 就不新建，而是把**上一次建出来的那张凭证**回给你（HTTP 200 而不是 201）。
 * 这正是「上一次其实落库了、只是响应丢了」的情形 ——
 * 上一版内容已经在库里，而用户此刻在屏幕上可能又敲了几个字，
 * 所以调用方要**再补一次 PUT** 把当前内容收敛上去。
 */
export const createVoucher = (bookId, payload, idempotencyKey) =>
  http.post(`/books/${bookId}/vouchers`, payload,
    idempotencyKey ? { headers: { 'Idempotency-Key': idempotencyKey } } : undefined)
    .then(r => ({ ...r.data, replayed: isReplay(r.headers) }))
export const updateVoucher = (bookId, id, payload) => http.put(`/books/${bookId}/vouchers/${id}`, payload).then(r => r.data)
export const deleteVoucher = (bookId, id) => http.delete(`/books/${bookId}/vouchers/${id}`).then(r => r.data)

// ---- 拆分凭证 ----
/**
 * 拆分的预览：一次拿到**所有**候选分界处、两半各自的合计与能不能拆，
 * 以及拆完之后各自的凭证号（号码口径只有后端一份，页面自己猜就一定会猜错）。
 * **只算不改** —— 跟 /insert-preview、/organize/preview 同一个路子。
 */
export const splitPreview = (bookId, id) =>
  http.get(`/books/${bookId}/vouchers/${id}/split-preview`).then(r => r.data)
/** 执行拆分。at_line = 第一张凭证保留到第几条分录（1 基）。 */
export const splitVoucher = (bookId, id, payload) =>
  http.post(`/books/${bookId}/vouchers/${id}/split`, payload).then(r => r.data)

// 记账 / 反记账不在这里 —— 走「期末结账」的结账与反结账（README 业务口径第 6 条）。
// 后端 `/vouchers/post` 与 `/vouchers/unpost` 两个端点仍在（余额表等处会用），
// 但前端没有、也不该在这里再包一层。

// ---- 整理凭证 ----
/** 单期间「整理就绪」体检（断号 + 时序），只读。列表页的提醒、记账/结账的闸门都看它。 */
export const voucherPeriodCheck = (bookId, periodId) =>
  http.get(`/books/${bookId}/vouchers/period-check`, { params: { period_id: periodId } }).then(r => r.data)
export const organizePreview = (bookId, payload) =>
  http.post(`/books/${bookId}/vouchers/organize/preview`, payload).then(r => r.data)
export const organize = (bookId, payload) =>
  http.post(`/books/${bookId}/vouchers/organize`, payload).then(r => r.data)

// ---- Excel 导入 ----
/**
 * 导入模板的 URL 构造器 —— **保留**，而且这个文件里所有调用点都该用它。
 *
 * 以前它零引用，于是 VoucherImport.vue 自己手拼了同一段路径
 * `/books/${bookId}/vouchers/import-template.xlsx`，然后忘了 import
 * `downloadFile`（运行时 ReferenceError，2026-10 修）。
 * 路径写两遍就是这类 bug 的成因：改后端路由时漏改一处，
 * 或者改了调用方式忘了改另一处。留一个单一来源比删掉它更防复发。
 */
export const importTemplateUrl = (bookId) => apiUrl(`/books/${bookId}/vouchers/import-template.xlsx`)
export const uploadImport = (bookId, file) => {
  const fd = new FormData()
  fd.append('file', file)
  return http.post(`/books/${bookId}/vouchers/import`, fd).then(r => r.data)
}
export const listImports = (bookId) => http.get(`/books/${bookId}/vouchers/import`).then(r => r.data)
export const commitImport = (bookId, batchId) =>
  http.post(`/books/${bookId}/vouchers/import/${batchId}/commit`).then(r => r.data)
export const discardImport = (bookId, batchId) =>
  http.post(`/books/${bookId}/vouchers/import/${batchId}/discard`).then(r => r.data)
