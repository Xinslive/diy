// 直接拿 store 的判定逻辑跑用户给的三个例子
import { readFileSync } from 'fs'
const src = readFileSync('frontend/src/stores/session.js', 'utf8')

function makeStore(periods) {
  const s = { periods }
  s.defaultPeriodId = new Function('year', `
    const pool = this.periods
    if (!pool.length) return undefined
    const pool0 = year ? pool.filter((p) => p.year === year) : pool
    if (!pool0.length) return undefined
    const open = pool0.find((p) => !p.locked)
    if (open) return open.id
    return pool0[pool0.length - 1].id
  `)
  s.defaultYear = new Function(`
    if (!this.periods.length) return undefined
    const years = [...new Set(this.periods.map((p) => p.year))].sort((a, b) => a - b)
    return years.find((y) => this.periods.some((p) => p.year === y && !p.locked))
      ?? years[years.length - 1]
  `)
  return s
}
const year = (y, n, lockedUpTo) => Array.from({ length: 12 }, (_, i) => ({
  id: y * 100 + i + 1, year: y, month: i + 1,
  locked: lockedUpTo !== undefined ? i + 1 <= lockedUpTo : false
}))

const cases = [
  ['结账至2026-12 且已年度结转（2027已开好）→ 2027-01', [...year(2026, 12, 12), ...year(2027, 12, 0)], '2027-01'],
  ['结账至2026-12 未年度结转（只有2026）→ 2026-12', year(2026, 12, 12), '2026-12'],
  ['结账至2027-02 → 2027-03', [...year(2026, 12, 12), ...year(2027, 12, 2)], '2027-03'],
]
let bad = 0
for (const [name, periods, expect] of cases) {
  const s = makeStore(periods)
  const y = s.defaultYear()
  const id = s.defaultPeriodId(y)
  const got = periods.find(p => p.id === id)
  const code = `${got.year}-${String(got.month).padStart(2, '0')}`
  const ok = code === expect
  if (!ok) bad++
  console.log(`${ok ? '✓' : '✗'} ${name}  → 实际 ${code}`)
}
process.exit(bad ? 1 : 0)
