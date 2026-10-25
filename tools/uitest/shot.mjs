import { chromium } from 'playwright'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })
const errs = []
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)
await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' })
if (await page.locator('input[type=password]').count()) {
  await page.locator('input').nth(0).fill('admin')
  await page.locator('input').nth(1).fill('123456')
  await page.click('button:has-text("登录")')
  await page.waitForSelector('.sidebar', { timeout: 20000 })
  await sleep(1500)
}
await page.goto('http://127.0.0.1:5173/#/periods', { waitUntil: 'networkidle' })
await sleep(2500)
console.log('当前期间大字:', await page.locator('.headline-code').innerText())
console.log('分组:', (await page.locator('.group-label').allInnerTexts()).join(' / '))
console.log('月份块数:', await page.locator('.month').count())
console.log('进度:', await page.locator('.card-head .hint').innerText())
await page.screenshot({ path: OUT + '/period_design.png' })
console.log('错误:', errs.length ? errs : '无')
await browser.close()
