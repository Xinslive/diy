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
// 记住「启明物业」，但清掉记住的期间 —— 模拟首次进入这个账套
await page.evaluate(() => { localStorage.removeItem('cwq_period') })
await page.reload({ waitUntil: 'networkidle' })
await sleep(2500)

// 切到启明物业
await page.locator('.el-select').first().click()
await sleep(600)
await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: '启明物业' }).first().click()
await sleep(2500)

const code = await page.locator('.page-head .muted').innerText()
console.log('落位期间:', code.trim().split(' ').pop())
const sel = await page.locator('.el-select input').evaluateAll(e => e.map(x => x.value))
console.log('顶栏:', JSON.stringify(sel))
// 再切走再切回，确认每次进账套都按同一口径
await page.locator('.el-select').first().click(); await sleep(500)
await page.locator('.el-select-dropdown:visible .el-select-dropdown__item').first().click(); await sleep(1800)
await page.locator('.el-select').first().click(); await sleep(500)
await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: '启明物业' }).first().click()
await sleep(2200)
const code2 = await page.locator('.page-head .muted').innerText()
console.log('再次进入落位:', code2.trim().split(' ').pop(), code2 === code ? '✓ 一致' : '✗ 不一致')
await page.screenshot({ path: OUT + '/period_book59.png' })
console.log('页面错误:', errs.length ? errs : '无')
await browser.close()
