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
  await page.waitForSelector('.el-aside', { timeout: 20000 })
  await sleep(1200)
}
await page.goto('http://127.0.0.1:5173/#/periods', { waitUntil: 'networkidle' })
await sleep(2000)

const btn = page.locator('.card.actions button', { hasText: '结转到' }).first()
console.log('按钮文案:', (await btn.innerText()).trim())
await btn.click()
await sleep(1200)
const box = page.locator('.el-message-box')
console.log('确认框:', (await box.innerText()).replace(/\n+/g, ' | '))
await page.screenshot({ path: OUT + '/yearend_before.png' })

await box.locator('button.el-button--primary').click()
await sleep(3500)
const msgs = await page.locator('.el-message').allInnerTexts()
console.log('结果消息:', msgs.map(s => s.trim()).join(' || ') || '(无)')
const toast = await page.locator('.el-message--error').allInnerTexts()
console.log('是否报错:', toast.length ? '是 → ' + toast.join('|') : '否')
await page.screenshot({ path: OUT + '/yearend_after.png' })
console.log('页面错误:', errs.length ? errs : '无')
await browser.close()
