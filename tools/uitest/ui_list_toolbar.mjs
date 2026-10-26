// 凭证列表工具栏的两条验收（用户 2026-10-26 批注）：
//   ① 「打印」默认藏着，勾了凭证才出现；
//   ② 「整理凭证」按钮撤了，但整理页还在。
// 只读，不写库。账套不写死 —— 本机有哪本带账的就用哪本。
//   node tools/uitest/ui_list_toolbar.mjs
import { chromium } from 'playwright'
import { fileURLToPath } from 'node:url'

const BASE = 'http://127.0.0.1:5173'
const OUT = fileURLToPath(new URL('../../.shots/', import.meta.url))
const fails = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1543, height: 984 } })
page.on('pageerror', e => fails.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') fails.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)
async function check(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want)
  console.log(ok ? '✓' : '✗', name, ok ? '' : `— 实际 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`)
  if (!ok) fails.push(`${name}: ${JSON.stringify(got)} != ${JSON.stringify(want)}`)
}
const bar = () => page.locator('.toolbar')

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.sidebar', { timeout: 15000 })
await sleep(1600)

// 挑一本真有凭证的账套：本机装哪本就用哪本，不写死名字。
const book = await page.evaluate(async () => {
  const h = { Authorization: 'Bearer ' + localStorage.getItem('cwq_token') }
  const books = await (await fetch('/api/books', { headers: h })).json()
  const list = Array.isArray(books) ? books : (books.items || [])
  for (const b of list) {
    const q = await (await fetch(
      `/api/books/${b.id}/vouchers?page_size=1`, { headers: h })).json()
    if ((q.total || 0) > 0) return { id: b.id, name: b.name }
  }
  return null
})
if (!book) throw new Error('本机没有带凭证的账套')
console.log(`   （用账套：${book.name}）`)
await page.evaluate(id => localStorage.setItem('cwq_book', String(id)), book.id)

await page.goto(`${BASE}/#/vouchers`); await sleep(2200)
const rows = page.locator('.el-table__body-wrapper tbody tr')
await check('列表页有凭证行', await rows.count() > 0, true)

// ---- ① 「打印」默认藏着 ----
await check('没勾选时工具栏里没有「打印」', await bar().getByRole('button', { name: /打印/ }).count(), 0)
await page.screenshot({ path: OUT + 'ui-list-toolbar-unselected.png' })

// ---- 勾一张，它才冒出来 ----
const firstCheck = rows.first().locator('.el-checkbox').first()
await firstCheck.click(); await sleep(700)
await check('勾了一张之后「打印所选」冒出来了', await bar().getByRole('button', { name: /打印所选/ }).count(), 1)
await check('按钮上写着这次打几张',
  (await bar().getByRole('button', { name: /打印所选/ }).first().innerText()).includes('1'), true)
await page.screenshot({ path: OUT + 'ui-list-toolbar-selected.png' })

// ---- 点它，打印弹窗打的就是勾选的那几张 ----
// 抓请求的 query：范围是「整期」还是「所选」，全在这个 voucher_ids 上。
// 按钮不见了但范围没跟着变，是最容易漏的那种错 —— 界面上看不出来。
let printUrl = ''
page.on('request', r => { if (r.url().includes('/print/vouchers')) printUrl = r.url() })
await bar().getByRole('button', { name: /打印所选/ }).first().click()
await page.waitForSelector('.print-dialog', { timeout: 15000 })
await sleep(2200)
await check('打印弹窗打开了', await page.locator('.print-dialog').count() > 0, true)
await check('打印请求带上了 voucher_ids（只打勾选的那几张）',
  /[?&]voucher_ids=\d+/.test(printUrl), true)
await check('打印请求带上了 period_id', /[?&]period_id=\d+/.test(printUrl), true)
await page.screenshot({ path: OUT + 'ui-list-print-dialog.png' })
await page.keyboard.press('Escape'); await sleep(700)

// ---- 取消勾选，它又收回去 ----
await firstCheck.click(); await sleep(700)
await check('取消勾选后「打印」又收起来了', await bar().getByRole('button', { name: /打印/ }).count(), 0)

// ---- ② 「整理凭证」按钮撤了 ----
await check('工具栏里没有「整理凭证」', await bar().getByRole('button', { name: '整理凭证', exact: true }).count(), 0)
await check('工具栏里也没有「去整理」', await bar().getByRole('button', { name: '去整理', exact: true }).count(), 0)
await check('「导入凭证」还在', await bar().getByRole('button', { name: '导入凭证', exact: true }).count(), 1)
await check('「新增凭证」还在', await bar().getByRole('button', { name: '新增凭证', exact: true }).count(), 1)

// ---- 撤的是按钮，不是功能：整理页还在 ----
await page.goto(`${BASE}/#/vouchers/organize`); await sleep(1800)
await check('整理页还打得开', /#\/vouchers\/organize/.test(page.url()), true)
await check('整理页有内容（不是白屏）', await page.locator('body').innerText().then(t => t.length > 0), true)
await page.screenshot({ path: OUT + 'ui-list-organize-page.png' })

await browser.close()
console.log('')
if (fails.length) {
  console.log('有问题：')
  fails.forEach(f => console.log('  - ' + f))
  process.exit(1)
}
console.log('全过')
