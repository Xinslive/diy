/**
 * 期初余额页的**年度口径**验收（2026-10-20）。
 *
 * 单独跑这一段的理由：ui_check.mjs 里有一批导航步骤还在用 `.el-menu-item`
 * 这个旧选择器（侧栏早已改成 `.sb-list`），它们各自要等满 30 秒超时，
 * 整轮下来会把这个真正要验的步骤挤出时间窗。这里只跑期初页相关的断言。
 */
import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const out = process.argv[2] || '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const errors = []

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))

async function step(name, fn) {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message.split('\n')[0]); errors.push(name + ': ' + e.message.split('\n')[0]) }
}

await step('登录', async () => {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await page.fill('input[type=password]', process.env.CWQ_PASSWORD || '123456')
  await page.click('button:has-text("登录")')
  await page.waitForSelector('.sidebar', { timeout: 15000 })
  await page.waitForTimeout(800)
})

await step('期初余额跟着顶栏的年度', async () => {
  const pick = await page.evaluate(async () => {
    const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
    const books = await (await fetch('/api/books', { headers: h })).json()
    for (const b of books) {
      const ps = await (await fetch(`/api/books/${b.id}/periods`, { headers: h })).json()
      const years = [...new Set(ps.map(p => p.year))]
      for (const y of years) {
        const inYear = ps.filter(p => p.year === y)
        const busy = inYear.find(p => p.posted > 0)
        const free = years.find(o => o !== y && !ps.some(p => p.year === o && p.posted > 0))
        if (busy && free) {
          const other = ps.find(p => p.year === free)
          return { bookId: b.id, book: b.name,
                   busyYear: y, busyPeriod: busy.code, busyId: busy.id,
                   freeYear: free, freeId: other.id }
        }
      }
    }
    return null
  })
  if (!pick) throw new Error('没有一个账套同时含「有已记账凭证的年度」和「空的年度」，这两条没法验')
  console.log(`   （尺子：${pick.book} ${pick.busyYear} 年度有已记账凭证、${pick.freeYear} 年度一张没有）`)
  await page.evaluate(p => {
    localStorage.setItem('cwq_book', String(p.bookId))
    localStorage.setItem('cwq_period', String(p.busyId))
  }, pick)

  const openAs = async (id) => {
    await page.evaluate(pid => localStorage.setItem('cwq_period', String(pid)), id)
    await page.goto(BASE + '/#/openings')
    await page.reload({ waitUntil: 'networkidle' })
    await page.waitForTimeout(1500)
    return page.evaluate(() => ({
      head: document.querySelector('.page-head .muted')?.innerText || '',
      alert: !!document.querySelector('.el-alert'),
      editable: !!document.querySelector('.grid input:not([disabled])'),
      amounts: [...document.querySelectorAll('table.grid tbody tr')].slice(0, 5)
        .map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim()).join('|'))
        .join(' / ')
    }))
  }

  // ① 有已记账凭证的年度：锁住
  const onBusy = await openAs(pick.busyId)
  if (!onBusy.head.includes(`${pick.busyYear} 年度`)) {
    throw new Error('标题没写成年度：' + onBusy.head)
  }
  if (!onBusy.alert || onBusy.editable) {
    throw new Error(`${pick.busyYear} 年度明明有已记账凭证，却还能改`)
  }

  // ② 空的年度：能改
  const onFree = await openAs(pick.freeId)
  if (!onFree.head.includes(`${pick.freeYear} 年度`)) {
    throw new Error('标题没跟着年度走：' + onFree.head)
  }
  if (onFree.alert || !onFree.editable) {
    throw new Error(`${pick.freeYear} 年度一张凭证没有，却不让改`)
  }

  // ③ 同一年内换月份：这一页纹丝不动（标题、能不能改、数都不变）
  const sameYearOther = await page.evaluate(async (arg) => {
    const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
    const ps = await (await fetch(`/api/books/${arg.bookId}/periods`, { headers: h })).json()
    const other = ps.find(p => p.year === arg.year && p.id !== arg.id)
    return other ? other.id : null
  }, { bookId: pick.bookId, year: pick.freeYear, id: pick.freeId })
  if (!sameYearOther) throw new Error('这一年里找不到第二个月来验「换月不动」')
  const onOther = await openAs(sameYearOther)
  if (onOther.head !== onFree.head) {
    throw new Error(`同一年换月份，标题变了：${onFree.head} → ${onOther.head}`)
  }
  if (onOther.editable !== onFree.editable) {
    throw new Error('同一年换月份，能不能改变了')
  }
  if (onOther.amounts !== onFree.amounts) {
    throw new Error('同一年换月份，金额变了：' + onFree.amounts + ' → ' + onOther.amounts)
  }
  console.log('   （' + pick.freeYear + ' 年度 换到另一月：标题、锁定态、金额完全一致）')
})

// ④ 换月份不重挂这一页：刚录了一半的数不能因为顶栏切月而消失
await step('切月份不丢未保存的录入', async () => {
  const before = await page.evaluate(() => {
    const inp = document.querySelector('.grid input:not([disabled])')
    return { code: inp?.closest('tr')?.querySelector('td')?.innerText.trim() || '' }
  })
  if (!before.code) throw new Error('这一页没有可编辑的格子（空年度应该有）')
  await page.evaluate(() => {
    const inp = document.querySelector('.grid input:not([disabled])')
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
    setter.call(inp, '12345')
    inp.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await page.waitForTimeout(300)
  // 顶栏切月（同一个年度）—— 页面不该重挂，框里的数还在
  await page.evaluate(() => {
    const sel = document.querySelectorAll('.topbar .el-select')[2]
    sel?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  })
  await page.waitForTimeout(600)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(400)
  const after = await page.evaluate(() => {
    const inp = document.querySelector('.grid input:not([disabled])')
    return { value: inp?.value || '' }
  })
  if (after.value !== '12345') {
    throw new Error('切月份后刚录的数没了：' + JSON.stringify(after))
  }
})

await page.screenshot({ path: `${out}/17-openings.png` })
console.log('\n' + (errors.length ? '问题:\n' + errors.map(e => '  - ' + e).join('\n') : '无 JS 错误'))
await browser.close()
process.exit(errors.length ? 1 : 0)
