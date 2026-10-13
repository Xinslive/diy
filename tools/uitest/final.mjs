import { chromium } from 'playwright'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1543, height: 984 } })
const errs = []
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
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
await sleep(1800)
await page.locator('.el-select').nth(1).click(); await sleep(700)
await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: /^2026/ }).first().click()
await sleep(2500)
// 选一个未结账的月，状态色才分得开
await page.locator('.month').nth(10).click(); await sleep(1800)
console.log('当前期间:', await page.locator('.headline-code').innerText())
console.log('页首还有公司名吗:', (await page.locator('.headline').innerText()).includes('物业') ? '有 ✗' : '没有 ✓')
const legend = await page.locator('.legend').innerText()
console.log('图例:', legend.replace(/\n/g,' '))
const tiles = await page.locator('.tile').evaluateAll(ts => ts.map(t =>
  t.querySelector('.tile-title').textContent + ' | ' +
  t.querySelector('.tile-status').textContent.trim().replace(/\s+/g,' ')))
tiles.forEach(t => console.log('  ' + t))
await page.screenshot({ path: OUT + '/period_final2.png' })
console.log('页面错误:', errs.length ? errs : '无')
await browser.close()
