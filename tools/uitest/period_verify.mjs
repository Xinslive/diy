import { chromium } from 'playwright'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })
const errs = []
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
const sleep = ms => page.waitForTimeout(ms)

await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' })
if (await page.locator('input[type=password]').count()) {
  await page.locator('input').nth(0).fill('admin')
  await page.locator('input').nth(1).fill('123456')
  await page.click('button:has-text("登录")')
  await page.waitForSelector('.sidebar', { timeout: 20000 })
  await sleep(1200)
}
await page.goto('http://127.0.0.1:5173/#/periods', { waitUntil: 'networkidle' })
await sleep(2000)

// 选启明物业账套（2026 已全部结账）
const heads = await page.locator('thead th .cell').allInnerTexts()
console.log('表头:', heads.map(s => s.trim()).filter(Boolean).join(' | '))
console.log('有「结账人」列:', heads.some(h => h.includes('结账人')))
console.log('有「切年度」控件:', await page.locator('.years').count())

// 确认当前是 2026 年、12 期全结账
const title = await page.locator('h3').first().innerText()
const rows = await page.locator('.el-table__body tbody tr').count()
console.log('期间卡片:', title, '行数:', rows)
await page.screenshot({ path: OUT + '/period_fixed.png' })
console.log('页面错误:', errs.length ? errs : '无')
await browser.close()
