/**
 * 发票导入的文件挑选逻辑（InvoiceImport.vue 用，测试也直接测这里）。
 *
 * 单独成文件而不是写在组件里，因为这些是**有分支、会出错**的判断：
 * 整文件夹传进来要递归、混着的非 PDF 要被忽略还要留下交代、
 * 同一份文件重复拖进来要去重。埋在模板事件里既测不到，出错也只能靠肉眼。
 */

/**
 * 上传上限的**兜底值**（真值由后端 `GET /invoices/limits` 给）。
 *
 * 接口没回来之前界面也得有个数可显示。但兜底值偏小的话，用户会在真上限
 * 到达之前就被前端拦下 —— 那时候拦住他的不是「一次最多 N 个」的真实契约，
 * 只是一个还没被更新的常量。所以它必须**不小于**后端的默认值。
 *
 * 放在这个文件里而不是组件里：这样前端测试能直接 import 它，
 * 与后端那条「网关闸必须宽于应用闸」的断言一起，把容量契约钉住。
 */
export const DEFAULT_UPLOAD_LIMITS = { maxFiles: 300, maxTotalBytes: 50 * 1024 * 1024 }

/** PDF 判定：只看扩展名，且大小写不敏感（IMG.PDF 也算）。 */
export const isPdfName = (name) => /\.pdf$/i.test(String(name || ''))

/**
 * 把一次挑选里的文件分成「要传的 PDF」与「该忽略的非 PDF」。
 *
 * 非 PDF 被忽略而不是报错：整个发票文件夹里混着 jpg/xlsx 是常态，
 * 让它们把整批拒掉等于这个功能不可用。但**忽略要有交代** —— 名字原样返回，
 * 界面上要显示出来，不无声消失。
 *
 * 去重按「名字 + 字节数 + 修改时间」：同一个文件夹拖两次、或多选之后又补选
 * 一次，同一份文件不该在列表里出现两行、传两遍（传两遍会被当成重复发票拦下）。
 */
export function partitionFiles(list) {
  const pdfs = []
  const skipped = []
  const seen = new Set()
  for (const file of Array.from(list || [])) {
    if (!isPdfName(file?.name)) {
      skipped.push(file?.name || '')
      continue
    }
    const key = `${file.name}|${file.size}|${file.lastModified || 0}`
    if (seen.has(key)) continue
    seen.add(key)
    pdfs.push(file)
  }
  return { pdfs, skipped }
}

/** 体积文案：给人看的，KB/MB 两位小数。 */
/**
 * 这一行当前算不算「已点名忽略」。
 *
 * 判定必须也看服务端状态，不能只看本地 overrides：忽略是点「重新计算
 * 预览」才落库的，刷新页面、或从历史里重开一个批次时 overrides 是空的。
 * 只认 overrides 的话，那一行会重新摊出一排科目下拉和一张空明细表，
 * 看着像「这张还没填」，而它早就被点名不要了。
 */
export function isIgnoredRow(row, overrides = {}) {
  const flag = overrides[row?.id]?.ignored
  if (flag === true) return true
  if (flag === false) return false
  return row?.status === 'IGNORED'
}

/**
 * 这一行是不是**已经生成凭证**（已入账）。
 *
 * 为什么前端也要判：服务端对已入账的记录**直接跳过重算**
 * （`service._build_preview` 里那条「已入账的不重算」），所以对一张
 * 已生成的发票点「忽略这张」、改科目、改类型，**点了什么也不会发生**。
 *
 * 那就不该把这些控件摆出来 —— 摆一个按了没反应的按钮，用户只会以为
 * 系统坏了，而不是「这一张早就定下来了」。
 *
 * 判定只看服务端状态：已入账的记录不会进 overrides（它压根不参与重算），
 * 所以这里没有本地覆盖可看。
 */
export function isCommittedRow(row) {
  return row?.status === 'COMMITTED'
}

export function humanBytes(n) {
  const value = Number(n) || 0
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

/** 被忽略项的交代文案，和后端批次备注同一个说法。 */
export function skipNote(skipped) {
  const names = (skipped || []).filter(Boolean)
  if (!names.length) return ''
  const head = names.slice(0, 5).join('、')
  const more = names.length > 5 ? ` 等 ${names.length} 个` : ''
  return `已忽略 ${names.length} 个非 PDF 文件：${head}${more}`
}

const _entryFile = (entry) => new Promise((resolve) => {
  entry.file((file) => resolve(file), () => resolve(null))
})

/**
 * `createReader().readEntries` 一次最多给 100 条，必须读到空为止 ——
 * 只调一次会静默丢掉第 101 个文件以后的全部内容（子目录尤其明显）。
 */
const _readAllEntries = (reader) => new Promise((resolve) => {
  const out = []
  const step = () => reader.readEntries(
    (batch) => {
      if (!batch.length) { resolve(out); return }
      out.push(...batch)
      if (out.length > 5000) { resolve(out); return }   // 目录结构异常时的兜底
      step()
    },
    () => resolve(out),
  )
  step()
})

async function _walk(entry, out, budget) {
  if (budget.scanned >= budget.max) return
  budget.scanned += 1
  if (entry.isFile) {
    const file = await _entryFile(entry)
    if (file) out.push(file)
    return
  }
  if (!entry.isDirectory) return
  const reader = entry.createReader()
  for (const child of await _readAllEntries(reader)) {
    if (budget.scanned >= budget.max) return
    await _walk(child, out, budget)
  }
}

/**
 * 从一次拖放里把文件全捞出来，**包括整个文件夹**。
 *
 * 为什么不能直接用 `dataTransfer.files`：拖进来一个文件夹时它是空的
 * （只有目录条目，没有文件条目），必须走 `webkitGetAsEntry` 递归。
 * 浏览器不支持这套 API 时退回 `dataTransfer.files`，多文件拖放照样能用 ——
 * 拿不到整个文件夹，好过整条路径都不通。
 *
 * `maxScanned` 挡住病态目录（一个几万条目的目录树不该把页面卡死）。
 */
export async function filesFromDataTransfer(dt, { maxScanned = 2000 } = {}) {
  const items = Array.from(dt?.items || [])
  const entries = items
    .map((item) => (typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null))
    .filter(Boolean)
  if (!entries.length) return Array.from(dt?.files || [])
  const out = []
  const budget = { scanned: 0, max: maxScanned }
  for (const entry of entries) {
    await _walk(entry, out, budget)
  }
  return out
}

/**
 * 「可能重复记账」的提示。
 *
 * **这件事是什么。** 有些发票是**手工先记了账**，后来才把票传上来导入 ——
 * 再生成一张凭证，同一笔钱就在账上记了两遍。系统**不拦**：发票号码才是
 * 这张票的唯一身份，金额相同不代表是同一笔（管理费每月同额，两个月各开
 * 一张是两张票）。所以只提示用户自己核对。
 *
 * 后端给的是**结构化**的命中清单（期间、金额、方向、每条命中的凭证与
 * 分录），这里把它拼成人话。文案与判定口径见
 * ``backend/app/invoice/duplicates.py``。
 */
export const DUPLICATE_WARNING_LABEL = '可能重复记账'

/** 这一行的「可能重复记账」命中清单；没有就是空数组。 */
export function duplicateWarningMatches(row) {
  const matches = row?.duplicate_warning?.matches
  return Array.isArray(matches) ? matches : []
}

/**
 * 这一行的提示文案；没有提示就是空串。
 *
 * 一条命中说成「凭证号 / 日期 / 借贷 / 科目 / 金额 / 摘要」——
 * 用户要能**一眼认出是哪一笔**，否则这句话等于没给。
 */
export function duplicateWarningText(row) {
  const warning = row?.duplicate_warning
  const matches = duplicateWarningMatches(row)
  if (!warning || !matches.length) return ''
  const head = `${DUPLICATE_WARNING_LABEL}：${warning.period || ''} 账上已有同额分录`
  const items = matches.map((match) => {
    const direction = match?.direction === 'DEBIT' ? '借' : '贷'
    const subject = [match?.subject_code, match?.subject_name].filter(Boolean).join(' ')
    const bits = [
      match?.voucher_no || '',
      match?.voucher_date || '',
      [direction, subject, match?.amount].filter(Boolean).join(' '),
    ].filter(Boolean)
    const text = bits.join(' / ')
    return match?.summary ? `${text}（${match.summary}）` : text
  })
  return `${head} —— ${items.join('；')}`
}

/** 这一批里有几张要提示。概要那一行要用它。 */
export function duplicateWarningCount(rows) {
  return (rows || []).filter((row) => duplicateWarningMatches(row).length).length
}
