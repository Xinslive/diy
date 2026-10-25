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
  await sleep(1200)
}
// 清掉记住的期间，走「首次进入账套」的路径
await page.evaluate(() => { localStorage.removeItem('cwq_period') })
await page.goto('http://127.0.0.1:5173/#/periods', { waitUntil: 'networkidle' })
await sleep(2500)

const head = await page.locator('.card h3').allInnerTexts()
console.log('两张卡的顺序:', head.map(s => s.trim()).join('  |  '))
const acts = await page.locator('.act-title').allInnerTexts()
console.log('操作项:', acts.map(s => s.trim()).join(' / '))
const yearendP = await page.locator('.act', { hasText: '年度结转' }).locator('p').count()
console.log('年度结转还剩说明文字吗:', yearendP > 0 ? '有' : '没有（已去掉）')
const texts = (await page.locator('.card.actions').innerText())
for (const [label, gone] of [
  ['（默认未记账，结账时统一自动记账）', '默认未记账'],
  ['数据库', '数据库'],
  ['结账越靠前影响范围越大，执行前请先看预览。', '结账越靠前'],
]) console.log(`「${label}」还在页面里:`, texts.includes(gone) ? '是 ✗' : '否 ✓')

const sel = await page.locator('.el-select input').evaluateAll(els => els.map(e => e.value))
console.log('顶栏默认账套/年度/月份:', JSON.stringify(sel))
await page.screenshot({ path: OUT + '/period_final.png' })
console.log('页面错误:', errs.length ? errs : '无')
await browser.close()
