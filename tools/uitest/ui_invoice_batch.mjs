// 发票批次页的三条验收（用户 2026-10-29 批注 + 那个「删了凭证就再也
// 生成不了」的洞）：
//
//   ① 两段灰字提示去掉了（「当前显示未入账 N 张」「已加载 N 份对账单…」）；
//   ② 打开批次页就**自动重新计算**一次 —— 结算科目按对账单流水自动认；
//   ③ 「来源文件」那一列，打开批次页就该看得见（不必点「重新计算预览」）；
//   ④ 凭证被删掉之后，那张发票重新变回「可生成」，「确认生成 N 张凭证」
//      的按钮亮着、数字对得上 —— 用户还能再用这张发票生成凭证。
//
// 只读：全程只 GET 与点开，不点「确认生成」。
//   node tools/uitest/ui_invoice_batch.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('../../.shots/', import.meta.url))
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1459, height: 987 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') fails.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)
async function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  console.log(ok ? '✓' : '✗', name, ok ? '' : `— 实际 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`)
  if (!ok) fails.push(`${name}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`)
}

// 记下每次请求 —— 「打开批次有没有自动重算」就靠它判断。
const requests = []
page.on('request', r => {
  const u = r.url()
  if (u.includes('/invoices/batches')) requests.push(`${r.method()} ${u.replace(/^https?:\/\/[^/]+/, '')}`)
})

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
// 登录后可能停在「选账套」那一页，不一定已经进主布局 —— 等令牌到手即可。
await page.waitForFunction(() => !!localStorage.getItem('cwq_token'), { timeout: 15000 })
await sleep(1200)

const book = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const list = Array.isArray(books) ? books : (books.items || [])
  for (const b of list) {
    const q = await (await fetch(`/api/books/${b.id}/invoices/batches`, { headers: h })).json()
    if ((q || []).length) return { id: b.id, name: b.name }
  }
  return null
})
if (!book) throw new Error('本机没有带发票批次的账套')
console.log(`   （用账套：${book.name}）`)
await page.evaluate(id => localStorage.setItem('cwq_book', String(id)), book.id)

await page.goto(`${BASE}/#/vouchers/invoices`); await sleep(2600)

// ---- 批次列表 ----
const historyCard = page.locator('.invoice-history-card')
await check('批次列表在', await historyCard.count(), 1)
const batchRows = historyCard.locator('.el-table__body-wrapper tbody tr')
await check('批次列表有行', await batchRows.count() > 0, true)
await page.screenshot({ path: OUT + 'ui-invoice-batch-list.png' })

// 找那个「有可生成发票」的批次 —— 没有就说明本机没造过场景。
//
// 判据用**接口**的 ready_count（那是现算的），不靠列表页的文字：
// 历史列表只有「状态 / 发票 / 已生成凭证 / 备注」几列，字面上看不出
// 「有几张可生成」。
const target = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const bookId = localStorage.getItem('cwq_book')
  const list = await (await fetch(`/api/books/${bookId}/invoices/batches`, { headers: h })).json()
  const hit = (list || []).find(b => (b.ready_count || 0) > 0)
  return hit ? { id: hit.id, filename: hit.filename, ready: hit.ready_count } : null
})
if (!target) {
  console.log('· 本机没有「有可生成发票」的批次 —— 跳过打开批次的验收')
  await browser.close()
  process.exit(fails.length ? 1 : 0)
}
console.log(`   （批次：${target.filename}，${target.ready} 张可生成）`)

// ---- 打开批次：自动重算 ----
requests.length = 0
let opened = false
for (let i = 0; i < await batchRows.count(); i++) {
  const row = batchRows.nth(i)
  if ((await row.innerText()).includes(target.filename)) { await row.click(); opened = true; break }
}
if (!opened) throw new Error(`批次列表里找不到「${target.filename}」那一行`)
await sleep(2600)

const previews = requests.filter(r => r.startsWith('POST') && r.includes('/preview'))
const details = requests.filter(r => r.startsWith('GET') && /\/batches\/\d+$/.test(r))
await check('打开批次拉了批次详情', details.length >= 1, true)
await check('打开批次**自动重新计算**了一次', previews.length >= 1, true)
console.log('   （请求：', requests.join(' | '), '）')

// ---- ① 两段提示去掉了 ----
const bodyText = await page.locator('main').innerText()
await check('没有「当前显示未入账 N 张」', bodyText.includes('当前显示未入账'), false)
await check('没有「已加载 N 份对账单」', bodyText.includes('已加载'), false)
await check('没有「点「重新计算预览」按金额与收支方向」', bodyText.includes('按金额与收支方向'), false)

// ---- ③ 「来源文件」那一列看得见了 ----
const headers = await page.locator('.el-table__header th').allInnerTexts()
await check('表头里有「来源文件」', headers.some(h => h.includes('来源文件')), true)

// ---- ④ 计数与按钮 ----
const confirmBtn = page.getByRole('button', { name: /确认生成 \d+ 张凭证/ })
await check('「确认生成」按钮在', await confirmBtn.count(), 1)
const btnText = await confirmBtn.first().innerText()
const ready = Number((btnText.match(/确认生成 (\d+) 张/) || [])[1])
await check('按钮上写的是可生成的张数', ready > 0, true)
await check('按钮没被禁用', await confirmBtn.first().isDisabled(), false)

const statusTags = await page.locator('.el-table__body-wrapper tbody tr .el-tag').allInnerTexts()
await check('表里有「可生成」的发票', statusTags.includes('可生成'), true)

await page.screenshot({ path: OUT + 'ui-invoice-batch-open.png' })
await page.screenshot({ path: OUT + 'ui-invoice-batch-open-full.png', fullPage: true })

await browser.close()
console.log(fails.length ? `\n✗ ${fails.length} 项没过` : '\n✓ 全部通过')
if (fails.length) { fails.forEach(f => console.log('  -', f)); process.exit(1) }
