export function fmt(v, digits = 2) {
  if (v === null || v === undefined || v === '') return ''
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

// 这里原来还有一个 `downloadUrl(path, params)`，与 `api/client.js` 的 `apiUrl`
// 实现几乎逐字重复（同为过滤空值拼 URLSearchParams，同返回 `/api${path}?${qs}`），
// 而且零引用。更关键的是：**拼出来的裸 URL 在浏览器里没有 Authorization**，
// 拿它去下载一律 401。要下载文件请用 `@/api/print` 的 `downloadFile()`。
// 已删 —— 留着只会让人挑了不能用的那个。
