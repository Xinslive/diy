import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const errs = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } })
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
const sleep = ms => page.waitForTimeout(ms)
async function step(name, fn) {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message.split('\n')[0]); errs.push(name + ': ' + e.message.split('\n')[0]) }
}
const topbar = () => page.locator('.topbar .el-select')
const yearSel = () => topbar().nth(1)
const monthSel = () => topbar().nth(2)
async function pick(sel, text) {
  await sel.click(); await sleep(700)
  await page.locator('.el-select-dropdown__item:visible').filter({ hasText: text }).first().click()
  await sleep(1300)
}
async function gotoPeriod(year, month) {
  await pick(yearSel(), String(year))
  await pick(monthSel(), `${month} 月`)
  const y = (await yearSel().innerText()).trim(), m = (await monthSel().innerText()).trim()
  if (!y.includes(String(year)) || !m.includes(`${month} 月`)) throw new Error(`没切到 ${year}-${month}，现在是 ${y}/${m}`)
  await sleep(900)
}
const postBtn = () => page.locator('button').filter({ hasText: /^记账$/ }).first()

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.sidebar', { timeout: 15000 })
await sleep(1500)

// ---------- 前置数据（全部可回滚）----------
const setup = await page.evaluate(async () => {
  const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业')
  const subjects = await (await fetch(`/api/books/${book.id}/subjects`, { headers: h })).json()
  const sub = c => (subjects.items || subjects).find(s => s.code === c).id
  // ① 2027-01 里放一张未记账凭证，用来验「上一期没结账 → 记账被挡」
  const made = await (await fetch(`/api/books/${book.id}/vouchers`, {
    method: 'POST', headers: h,
    body: JSON.stringify({
      voucher_date: '2027-01-15', remark: 'UI 验证用临时凭证', source: 'MANUAL',
      entries: [
        { subject_id: sub('1002'), direction: 'DEBIT', amount: '1.00', summary: '临时' },
        { subject_id: sub('6001'), direction: 'CREDIT', amount: '1.00', summary: '临时' },
      ],
    })
  })).json()
  // ② 2026-12 退回一张已记账的，用来验「上一期已结账 → 记账放行」（验完再记回去）
  const periods = await (await fetch(`/api/books/${book.id}/periods`, { headers: h })).json()
  const dec = periods.find(p => p.code === '2026-12')
  const list = await (await fetch(`/api/books/${book.id}/vouchers?to_period_id=${dec.id}&status=POSTED`, { headers: h })).json()
  const items = list.items || list
  const one = items[0]
  await fetch(`/api/books/${book.id}/vouchers/unpost`, {
    method: 'POST', headers: h, body: JSON.stringify({ ids: [one.id] })
  })
  return { bookId: book.id, tempVoucherId: made.id, decVoucherId: one.id, decNo: one.voucher_no }
})
console.log('   前置数据:', JSON.stringify(setup))

await pick(topbar().nth(0), '启明物业')
await page.click('.el-menu-item:has-text("凭证")')
await sleep(1500)

await step('① 状态标签只写「已记账」，不再强调已锁', async () => {
  await gotoPeriod(2026, 1)
  const tags = (await page.locator('.el-table__body .el-tag').allInnerTexts()).map(s => s.trim())
  const posted = tags.filter(t => t.includes('已记账'))
  console.log('   状态标签:', [...new Set(tags)].join(' | '))
  if (!posted.length) throw new Error('2026-01 没有已记账凭证')
  if (posted.some(t => t.includes('已锁'))) throw new Error('标签里还有「已锁」: ' + posted.join(','))
})

await step('②③ 顶栏期间拆成「年」+「月」两个下拉', async () => {
  const n = await topbar().count()
  const y = (await yearSel().innerText()).trim(), m = (await monthSel().innerText()).trim()
  console.log('   下拉数:', n, '| 年:', y, '| 月:', m)
  if (n !== 3) throw new Error(`应有 3 个下拉，实际 ${n}`)
  if (!/\d{4}\s*年/.test(y)) throw new Error('第二个下拉不是年份')
  if (!/\d{1,2}\s*月/.test(m)) throw new Error('第三个下拉不是月份')
})

await step('②b 年份下拉只列年度，月份固定 12 项', async () => {
  await yearSel().click(); await sleep(800)
  const years = (await page.locator('.el-select-dropdown__item:visible').allInnerTexts()).map(s => s.trim())
  console.log('   年份选项:', years.join(' | '))
  if (years.length !== 2) throw new Error('年份选项数不对: ' + years.length)
  await page.keyboard.press('Escape'); await sleep(400)
  await monthSel().click(); await sleep(800)
  const months = (await page.locator('.el-select-dropdown__item:visible').allInnerTexts()).map(s => s.trim())
  console.log('   月份选项数:', months.length)
  if (months.length !== 12) throw new Error('月份应固定 12 项，实际 ' + months.length)
  await page.keyboard.press('Escape'); await sleep(500)
})

await step('③b 年份、月份各自都能切', async () => {
  await gotoPeriod(2026, 3); console.log('   → 2026-03')
  await gotoPeriod(2027, 1); console.log('   → 2027-01')
})

await step('④ 上一期没结账 → 记账按钮禁用，且说清先结哪个月', async () => {
  await gotoPeriod(2027, 1)
  const cb = page.locator('.el-table__body .el-checkbox').first()
  await cb.waitFor({ timeout: 8000 }); await cb.click(); await sleep(600)
  const disabled = await postBtn().isDisabled()
  console.log('   2027-01 记账 disabled =', disabled)
  if (!disabled) throw new Error('上一期 2026-12 未结账，2027-01 却还能记账')
  await page.locator('.el-tooltip__trigger').filter({ hasText: /^记账$/ }).first().hover()
  await sleep(1000)
  const tip = (await page.locator('.el-popper:visible').allInnerTexts()).join(' ').trim()
  console.log('   提示:', tip)
  if (!tip.includes('2026-12')) throw new Error('提示没说要先结哪个月: ' + tip)
  await page.screenshot({ path: `${OUT}/r2-post-blocked.png` })
})

await step('⑤ 后端也确实拦得住（不只是禁用按钮）', async () => {
  const res = await page.evaluate(async (s) => {
    const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
    const r = await fetch(`/api/books/${s.bookId}/vouchers/post`, {
      method: 'POST', headers: h, body: JSON.stringify({ ids: [s.tempVoucherId] })
    })
    return { status: r.status, body: (await r.json()).detail }
  }, setup)
  console.log('   接口返回:', res.status, res.body)
  if (res.status === 200) throw new Error('接口居然放行了')
  if (!/2026-12/.test(res.body || '')) throw new Error('接口报错没说清先结哪个月: ' + res.body)
})

await step('⑥ 上一期已结账 → 记账按钮放行（2026-12，上期 2026-11 已结账）', async () => {
  await gotoPeriod(2026, 12)
  const cb = page.locator('.el-table__body .el-checkbox').first()
  await cb.waitFor({ timeout: 8000 }); await cb.click(); await sleep(600)
  const disabled = await postBtn().isDisabled()
  console.log('   2026-12 记账 disabled =', disabled)
  if (disabled) throw new Error('上一期已结账了，本期还是不让记账')
  await page.screenshot({ path: `${OUT}/r2-post-allowed.png` })
})

// ---------- 回滚 ----------
const cleanup = await page.evaluate(async (s) => {
  const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const del = await fetch(`/api/books/${s.bookId}/vouchers/${s.tempVoucherId}`, { method: 'DELETE', headers: h })
  if (!del.ok) throw new Error('临时凭证删不掉: ' + del.status + ' ' + (await del.text()))
  const re = await fetch(`/api/books/${s.bookId}/vouchers/post`, {
    method: 'POST', headers: h, body: JSON.stringify({ ids: [s.decVoucherId] })
  })
  if (!re.ok) throw new Error('记回去失败: ' + re.status + ' ' + (await re.text()))
  const periods = await (await fetch(`/api/books/${s.bookId}/periods`, { headers: h })).json()
  const jan = periods.find(p => p.code === '2027-01')
  const dec = periods.find(p => p.code === '2026-12')
  return { janVouchers: jan.vouchers, decUnposted: dec.unposted, decPosted: dec.posted }
}, setup)
console.log('   已回滚，2027-01 凭证数 =', cleanup.janVouchers,
            '| 2026-12 已记账 =', cleanup.decPosted, '未记账 =', cleanup.decUnposted)

await browser.close()
if (errs.length) { console.log('\n❌ 有问题:\n' + errs.join('\n')); process.exit(1) }
console.log('\n✅ 本轮 6 项全部通过')
