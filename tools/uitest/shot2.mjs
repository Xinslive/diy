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
  await sleep(1500)
}
await page.goto('http://127.0.0.1:5173/#/periods', { waitUntil: 'networkidle' })
await sleep(2000)
await page.locator('.el-select').first().click(); await sleep(700)
await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: '启明物业' }).first().click()
await sleep(3000)
// 切到 2026 年看有数据的样子
await page.locator('.el-select').nth(1).click(); await sleep(700)
await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: /^2026/ }).first().click()
await sleep(2500)
console.log('当前期间:', await page.locator('.headline-code').innerText())
console.log('进度:', await page.locator('.card-head .hint').innerText())
const cls = await page.locator('.month').evaluateAll(els => els.map(e =>
  e.querySelector('.m-code').textContent + ':' + [...e.classList].filter(c=>['locked','pending','idle','current'].includes(c)).join('+')))
console.log('月份状态:', cls.join('  '))
await page.screenshot({ path: OUT + '/period_design_real.png' })
console.log('错误:', errs.length ? errs : '无')
await browser.close()
