import { http } from './client'

export const listBooks = () => http.get('/books').then(r => r.data)
export const createBook = (payload) => http.post('/books', payload).then(r => r.data)
/**
 * 保存账套磁贴的**个人**排序。
 *
 *  传的是相对先后（一串有序的账套 id），不是「第 2 本写在第 1 位」——
 *  绝对序号每拖一次都要重算全部，中途新建的账套还会把后面全顶掉。
 *  没出现在这串里的账套由后端接在末尾并补齐序号。
 */
export const saveBookOrder = (ids) => http.put('/books/order', { book_ids: ids }).then(r => r.data)
export const getBook = (id) => http.get(`/books/${id}`).then(r => r.data)
export const updateBook = (id, payload) => http.put(`/books/${id}`, payload).then(r => r.data)
export const listPeriods = (id) => http.get(`/books/${id}/periods`).then(r => r.data)
// 期初跟着顶栏的**年度**走：一笔期初 = 一个会计年度的起点，
// 切到哪个月都不影响它指向哪一份数。不传就是账套的启用年度。
export const setOpenings = (id, rows, year) =>
  http.post(`/books/${id}/periods`, rows, { params: year ? { year } : {} }).then(r => r.data)
export const carryForwardInfo = (id, pid) =>
  http.get(`/books/${id}/periods/${pid}/carry-forward`).then(r => r.data)
/**
 * 本年利润科目（结转损益的**目的科目**）。
 *
 * 系统按候选编码自动识别，但每本账的科目表是各人自己的 ——
 * 识别不到时账就结不了转，而界面上得有个地方能改，所以有这一对接口。
 *
 * 传的是**科目编码**而不是 id：科目表每个年度各有一份，同一编码在 2026 与
 * 2027 是两行不同的记录，存 id 就只能管一年。`code` 传 null 表示
 * 「清掉手工设定，回到自动识别」—— 自动识别是一个正当的选项，不是「没填」。
 */
export const getProfitSubject = (id, year) =>
  http.get(`/books/${id}/years/${year}/profit-subject`).then(r => r.data)
export const setProfitSubject = (id, year, code) =>
  http.put(`/books/${id}/years/${year}/profit-subject`, { code: code || null }).then(r => r.data)
/**
 * 未分配利润科目 —— **年度结转**的目的科目（本年利润的余额冲到它上面）。
 *
 * 与本年利润科目同一套口径：年度级、传科目编码、`code` 传 null = 回到自动识别。
 * 系统按候选编码（310415 / 3104 / 4104）自动识别，识别不到时年度结转就结不了，
 * 所以有这一对接口让用户自己指定。
 */
export const getRetainedSubject = (id, year) =>
  http.get(`/books/${id}/years/${year}/retained-subject`).then(r => r.data)
export const setRetainedSubject = (id, year, code) =>
  http.put(`/books/${id}/years/${year}/retained-subject`, { code: code || null }).then(r => r.data)
// 结转损益永远是「有旧凭证就删掉、按最新余额重算」，replace 只为兼容旧调用
export const carryForward = (id, pid, replace = true) =>
  http.post(`/books/${id}/periods/${pid}/carry-forward`, { replace }).then(r => r.data)
/**
 * 结账 —— **必须带期间版本号**（乐观锁）。
 *
 *  两个账号各开着一个页面，A 先结了账，B 那边还拿着「未结账 + 版本 1」。
 *  不带版本号时后端按 B 的陈旧状态照做（他那张屏幕上确实写着未结账），
 *  带着版本号时后端比对库里那一份：不一致就 409 拒掉，两边都落到同一个事实上。
 *
 *  这条以前不在本文件里：PeriodPanel.vue 自己 import 了共享 http 手写路径，
 *  理由是「closePeriod 还没有 version 参数」。现在有了 ——
 *  路径写在两个文件里，改后端路由时必然漏改一处。
 *  一张凭证都没有的期间后端一律拒收，**没有旁路**：空月份恰好能穿过整理体检、
 *  自动记账和损益结平三道闸门，「忘录账」会被静默封进历史，而补录得反结账、
 *  反结账会连带把后面已结的月份全部退回。真的无业务也要录凭证（哪怕一张
 *  说明凭证）—— 那张凭证本身就是「这个月我看过、是空的」的记录。
 */
export const closePeriod = (id, pid, version) =>
  http.post(`/books/${id}/periods/${pid}/close`, { version }).then(r => r.data)
export const unclosePreview = (id, pid) => http.get(`/books/${id}/periods/${pid}/unclose-preview`).then(r => r.data)
/** 反结账。同样带版本号，口径与结账一致。 */
export const unclose = (id, pid, version, revertPosting = true) =>
  http.post(`/books/${id}/periods/${pid}/unclose`, { revert_posting: revertPosting, version }).then(r => r.data)
// 年度结转：不再新建账套，只在同一个账套里追加下一年度
export const yearEnd = (id, fromYear, newYear) => http.post(`/books/${id}/year-end`, { from_year: fromYear || null, new_year: newYear || null }).then(r => r.data)
export const auditLogs = (id, limit = 100) => http.get(`/books/${id}/audit-logs`, { params: { limit } }).then(r => r.data)

// 期初余额是**年度**口径：传的是年度，不是期间
export const getOpenings = (id, year) =>
  http.get(`/books/${id}/openings`, { params: year ? { year } : {} }).then(r => r.data)
/**
 * 解析期初余额文件，**只解析不落库** —— 返回行、匹配情况、借贷是否平衡，
 *  让人先看清读进来什么再决定要不要填进表（落库仍是 setOpenings 那一步）。
 *
 *  basis 选「期初」还是「期末」那一份数：用友余额表两种数都在同一张表里，
 *  以前前端写死期初，用户拿期末数来对就对不上了。basis 变就得重新解析同一个文件 ——
 *  两份数在文件里是不同的列，不是同一份结果的两种显示。
 */
export const parseOpenings = (id, fd, basis = 'opening') => {
  const h = { 'Content-Type': 'multipart/form-data' }
  return http.post(`/books/${id}/openings/parse`, fd, {
    params: { basis }, headers: h
  }).then(r => r.data)
}

/**
 * 账套初始化状态（两步向导唯一的数据来源）。
 * GET /books 的每一行已经带 init，**列表页不必为每张卡片再请求一次**；
 *  这条是给向导自己用的 —— 打开时拿一份最新的，任一步做完再刷新。
 *
 *  **年度是必带的**：初始化状态挂在**年度**上（后端迁移 0032）——
 *  每个年度有**自己的**科目表，也各有各的「第 1 步做没做」。
 *  2026 年做完了，不代表 2027 年做完了。
 */
export const getInit = (id, year) =>
  http.get(`/books/${id}/init`, { params: year ? { year } : {} }).then(r => r.data)

/** 第 1 步的第二条路：账上确实有科目时，直接沿用系统标准科目表（不覆盖导入）。 */
export const keepChartSubjects = (id, year) =>
  http.post(`/books/${id}/init/subjects/keep`, null,
    { params: year ? { year } : {} }).then(r => r.data)

/**
 * 第 2 步的第二条路：声明「本期本来就没有期初余额」。
 *
 *  新公司第一年、上一年度本来就是空账，期初该是 0 —— 界面上只有「导入」一个
 *  出口时，这类账套的初始化永远停在第 2 步，卡片上的「待初始化」摘不掉。
 *  后端只接受**账上确实一条期初都没有**的账套（有的话就是「把期初清空」，
 *  与「本来就没有」不是一回事）。
 */
export const skipOpenings = (id, year) =>
  http.post(`/books/${id}/init/openings/skip`, null,
    { params: year ? { year } : {} }).then(r => r.data)
