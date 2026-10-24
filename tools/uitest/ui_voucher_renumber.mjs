// 跨期改日期 → 服务端重新发号 → 页面回写新号与新期间的浏览器验收。
//
// 背景（用户 2026-10-24）：「我把原本会计期间是 1 月的某张凭证的日期，
// 改成了 2 月 5 日，会提示凭证号在其他月有重复，所以不允许我改时间。
// 目前凭证号已经不允许用户手动输入了，所以我需要你增加机制，在我改日期后，
// 自动分配一个目标期间的凭证号，已有最大凭证号 + 1 就可以。」
//
// 这一支验的是**页面这一半**：服务端发了新号之后，页面上那个只读的凭证号
// 跟着换、期间跟着换，而且下一趟自动保存不再把旧号送回去。
// 服务端那一半（真发号、真换期）由 backend/tests/test_voucher_no_edit.py
// 与 test_api_contracts.py 在真库上跑 —— 这里写接口一律拦下，不动真账。
//
//   node ui_voucher_renumber.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('../../.shots/', import.meta.url))
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1560, height: 980 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message + '｜' + String(e.stack || '').split('\n')[1]))
const sleep = ms => page.waitForTimeout(ms)
async function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  console.log(ok ? '✓' : '✗', name, ok ? '' : `— 实际 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`)
  if (!ok) fails.push(`${name}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`)
}

/** 写接口一律拦下来：记一笔，再回一个**服务端重新发过的号** */
const writes = []
let fake = null          // 本次要伪造的「服务端新发的号 + 目标期间」
await page.route('**/api/books/*/vouchers**', async (route) => {
  const req = route.request()
  if (req.url().includes('/neighbour')) {
    return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ prev_id: null, next_id: null }) })
  }
  if (req.method() === 'GET') return route.continue()
  writes.push({ method: req.method(), body: JSON.parse(req.postData() || '{}') })
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      id: fake.id, voucher_no: fake.voucher_no, voucher_date: fake.voucher_date,
      status: 'UNPOSTED', source: 'MANUAL', remark: '', attachments: 0,
      period_id: fake.period_id, total_debit: '0', total_credit: '0', entries: []
    })
  })
})

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.sidebar .sb-list li', { timeout: 15000 })
await sleep(1800)
await page.locator('.topbar .el-select').first().click(); await sleep(800)
await page.locator('.el-select-dropdown__item:visible', { hasText: '启明物业' }).first().click()
await sleep(1800)

// ---- 挑一张「能改」的凭证，和一个「还没结账」的另一个期间 ----
const pick = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const book = books.find(b => b.name === '启明物业') || books[0]
  const periods = await (await fetch(`/api/books/${book.id}/periods`, { headers: h })).json()
  const open = periods.filter(p => !p.locked)
  for (const p of open) {
    const q = await (await fetch(
      `/api/books/${book.id}/vouchers?from_period_id=${p.id}&to_period_id=${p.id}&status=UNPOSTED&page=1&page_size=5`,
      { headers: h })).json()
    const v = (q.items || [])[0]
    if (!v) continue
    // 目标期间：另一个还没结账的期间（跨月、跨年都算）
    const target = open.find(t => t.id !== p.id && t.year === p.year)
    if (!target) continue
    return {
      bookId: book.id, bookName: book.name,
      voucher: { id: v.id, voucher_no: v.voucher_no, voucher_date: v.voucher_date, period_id: v.period_id },
      from: { id: p.id, code: p.code, month: p.month, year: p.year },
      to: { id: target.id, code: target.code, month: target.month, year: target.year }
    }
  }
  return null
})
if (!pick) {
  console.log('✗ 找不到「未结账期间里的未记账凭证」，换一个账套再跑')
  await browser.close()
  process.exit(1)
}
console.log('· 用的是', pick.bookName, '第', pick.voucher.id, '号凭证：',
  pick.from.code, '→', pick.to.code)

// 服务端「重新发号」的结果：目标期间的最大号 + 1（这里用 记0099 假装是那一期的新号）
fake = {
  id: pick.voucher.id,
  voucher_no: '记0099',
  voucher_date: `${pick.to.year}-${String(pick.to.month).padStart(2, '0')}-05`,
  period_id: pick.to.id
}

await page.goto(`${BASE}/#/vouchers/${pick.voucher.id}`); await sleep(2500)

const noInput = page.locator('.toolbar input').first()
const dateInput = page.locator('.toolbar input').nth(1)
await check('页面上的凭证号是原来那个', await noInput.inputValue(), pick.voucher.voucher_no)
await check('页眉写的是这张凭证自己的期间',
  (await page.locator('.voucher-head b').first().textContent()).trim(), pick.from.code)
await check('日期框可改（这张没记账、期间没结账）', await dateInput.isEnabled(), true)

// ---- 改日期：1 月 → 2 月（跨期） ----
await dateInput.click(); await sleep(300)
await dateInput.fill(fake.voucher_date); await sleep(200)
await page.keyboard.press('Enter'); await sleep(400)
await check('日期框里的日期已经改了', await dateInput.inputValue(), fake.voucher_date)

// 自动保存 700ms 后发 PUT
await sleep(2500)
await check('改日期触发了一趟保存', writes.length >= 1, true)
await check('保存请求里没有凭证号（号由服务端发）',
  Object.prototype.hasOwnProperty.call(writes[0]?.body || {}, 'voucher_no'), false)
await check('保存请求里没有 insert_after 参照（更新老凭证不带它）',
  writes[0]?.body?.insert_after ?? null, null)

// ---- 页面回写服务端发的新号与新期间 ----
await check('页面上的凭证号换成了服务端发的新号', await noInput.inputValue(), '记0099')
await check('页眉的期间也跟着换了',
  (await page.locator('.voucher-head b').first().textContent()).trim(), pick.to.code)

// 换号不该再引出第二趟保存：号不进 dirty 签名（服务端改的号不是「人改了这张」）
const before = writes.length
await sleep(1500)
await check('换号之后没有多余的第二趟保存', writes.length, before)

await page.screenshot({ path: OUT + 'ui-voucher-renumber.png' })

await browser.close()
console.log(fails.length ? `\n✗ 失败 ${fails.length} 项：\n` + fails.map(f => '  - ' + f).join('\n')
  : '\n✓ 全部通过')
process.exit(fails.length ? 1 : 0)
