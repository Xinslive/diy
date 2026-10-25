/**
 * 把后端的时间串显示成**北京时间**。
 *
 * 后端把每个数据库连接的会话时区钉在 UTC（见 app/core/db.py），所以接口回来的
 * 时间串一律是 UTC，例如 ``2026-10-18T14:28:52.123456+00:00``。早先几个页面直接
 * ``replace('T', ' ').slice(0, 19)`` 截断显示，用户看到的时间比实际早了 8 小时，
 * 而记账、结账、对账一律按北京时间理解 —— 这种偏差比「显示不好看」严重得多：
 * 人会照着错的时间去核账。
 *
 * 这里固定 +08:00，而不是 ``toLocaleString()``：后者按**浏览器**时区转换，
 * 换个设备或出差打开同一笔操作就变成了另一个时间。账务口径上的北京时间
 * 不该随谁在哪看而变。
 *
 * 中国 1991 年起不再实行夏令时，固定 +08:00 与 ``Asia/Shanghai`` 等价。
 *
 * 自己解析而不交给 ``new Date(string)``：接口可能给出 6 位微秒的 ISO 串，
 * 而 ``Date`` 的标准解析只认 3 位毫秒，交给它会得到 Invalid Date。
 */

const MINUTE = 60 * 1000
const CN_OFFSET = 8 * 60 * MINUTE

const ISO = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/i

const pad = n => String(n).padStart(2, '0')

/**
 * @param {string|null|undefined} value 后端返回的 ISO 时间串
 * @param {{seconds?: boolean}} [options] seconds=false 时只到分钟
 * @returns {string} 北京时间；无法解析时原样返回，认不出来就不要显示空白
 */
export function formatDateTime(value, options = {}) {
  if (value === null || value === undefined || value === '') return ''
  const parts = ISO.exec(String(value).trim())
  if (!parts) return String(value)

  const [, year, month, day, hour, minute, second, zone] = parts
  const offsetMinutes = !zone || zone.toUpperCase() === 'Z'
    ? 0
    : (zone[0] === '-' ? -1 : 1) *
      (Number(zone.slice(1, 3)) * 60 + Number(zone.slice(-2)))

  const utc = Date.UTC(
    Number(year), Number(month) - 1, Number(day),
    Number(hour), Number(minute), Number(second || 0),
  )
  const beijing = new Date(utc - offsetMinutes * MINUTE + CN_OFFSET)

  const date = `${beijing.getUTCFullYear()}-${pad(beijing.getUTCMonth() + 1)}-${pad(beijing.getUTCDate())}`
  const clock = `${pad(beijing.getUTCHours())}:${pad(beijing.getUTCMinutes())}`
  return options.seconds === false ? `${date} ${clock}` : `${date} ${clock}:${pad(beijing.getUTCSeconds())}`
}

/** 只取日期部分（仍是北京时间）。 */
export function formatDate(value) {
  const text = formatDateTime(value)
  return text.length >= 10 ? text.slice(0, 10) : text
}

/**
 * 今天的**本地日期**（`YYYY-MM-DD`）。
 *
 * **为什么不能写 `new Date().toISOString().slice(0, 10)`**：
 * `toISOString()` 给的是 **UTC** 日期 —— 北京时间 00:00–08:00 之间，
 * 它会落到**前一天**。凭证日期按北京时间理解，落错一天就是落错期间。
 *
 * 另一个坑：``toLocaleDateString`` 按**浏览器**时区转换，换个设备打开
 * 同一张单据会变成另一个日期 —— 与本文件顶部定下的「固定 +08:00」口径
 * 也不一致。这里取的是浏览器本地日历日期，与
 * ``VoucherEdit.vue`` 里 ``getDate()`` 的口径一致。
 *
 * @param {Date} [now] 便于测试注入
 * @returns {string} `YYYY-MM-DD`
 */
export function todayLocal(now = new Date()) {
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}
