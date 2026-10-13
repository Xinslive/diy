import { chromium } from 'playwright'
const browser = await chromium.launch()
for (const [paper, label] of [['A4','A4(2张/页)'], ['A5','A5(1张/页)']]) {
  const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })
  const errs = []
  page.on('pageerror', e => errs.push(e.message))
  await page.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' })
  if (await page.locator('input[type=password]').count()) {
    await page.click('button:has-text("登录")'); await page.waitForSelector('.el-aside', { timeout: 20000 }); await page.waitForTimeout(800)
  }
  await page.goto('http://127.0.0.1:5173/#/print-templates', { waitUntil: 'networkidle' })
  await page.waitForTimeout(2500)
  const pick = async (l) => {
    await page.locator('.el-form-item:has-text("纸型") .el-select').first().click(); await page.waitForTimeout(500)
    await page.locator('.el-select-dropdown:visible .el-select-dropdown__item', { hasText: new RegExp('^' + l) }).first().click()
    await page.waitForTimeout(3000)
  }
  await pick('A4'); await pick(paper)
  const fr = page.frames().find(f => f.url() === 'about:srcdoc')
  const r = await page.evaluate(() => {
    const b = document.querySelector('.preview-box')
    return { 外层纵向: b.scrollHeight - b.clientHeight, 外层横向: b.scrollWidth - b.clientWidth }
  })
  const inner = await fr.evaluate(() => ({
    内层纵向: document.documentElement.scrollHeight - document.documentElement.clientHeight,
    内层横向: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    页数: document.querySelectorAll('.page').length,
  }))
  console.log(label, JSON.stringify({ ...r, ...inner }), errs.length ? errs : '')
  await page.close()
}
await browser.close()
