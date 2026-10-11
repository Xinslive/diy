import { chromium } from 'playwright'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1543, height: 984 } })
const errs = []
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()) })
const sleep = ms => page.waitForTimeout(ms)
await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' })
if (await page.locator('input[type=password]').count()) {
  await page.locator('input').nth(0).fill('admin')
  await page.locator('input').nth(1).fill('123456')
  await page.click('button:has-text("登录")')
  await page.waitForSelector('.el-aside', { timeout: 20000 })
  await sleep(1500)
}
await page.goto('http://127.0.0.1:5173/#/periods', { waitUntil: 'networkidle' })
await sleep(2000)
await page.locator('.el-select').first().click(); await sleep(700)
await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: '启明物业' }).first().click()
await sleep(2000)
// 回到 2026 年看有数据的样子
await page.locator('.el-select').nth(1).click(); await sleep(700)
await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: /^2026/ }).first().click()
await sleep(2500)
// 选一个未结账的月，看状态色变化（123 账套有未结账的月份）
await page.locator('.month').nth(6).click(); await sleep(1500)
console.log('当前期间:', await page.locator('.headline-code').innerText())
const tiles = await page.locator('.tile').evaluateAll(ts => ts.map(t => ({
  title: t.querySelector('.tile-title').textContent,
  status: t.querySelector('.tile-status').textContent.trim().replace(/\s+/g,' '),
  btn: t.querySelector('.tile-btn').textContent.trim(),
  disabled: t.querySelector('.tile-btn').className.includes('is-disabled'),
})))
tiles.forEach(t => console.log(` ${t.title}: [${t.status}] 「${t.btn}」${t.disabled?'禁用':'可用'}`))
const box = await page.locator('.month').first().boundingBox()
console.log('月份格尺寸:', Math.round(box.width) + 'x' + Math.round(box.height))
await page.screenshot({ path: OUT + '/period_tiles.png' })
console.log('错误:', errs.length ? errs : '无')
await browser.close()
