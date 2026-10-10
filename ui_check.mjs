import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const errors = []
const out = process.argv[2] || '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('console', (m) => { if (m.type() === 'error') errors.push('console: ' + m.text()) })
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))

async function step(name, fn) {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message.split('\n')[0]); errors.push(name + ': ' + e.message.split('\n')[0]) }
}

await step('打开登录页', async () => {
  await page.goto(BASE, { waitUntil: 'networkidle' })
  await page.waitForSelector('text=财务记账系统')
})
await page.screenshot({ path: `${out}/01-login.png` })

await step('登录', async () => {
  await page.click('button:has-text("登录")')
  await page.waitForSelector('.el-aside', { timeout: 15000 })
  await page.waitForTimeout(800)
})
await page.screenshot({ path: `${out}/02-books.png` })

await step('登录后落在账套管理', async () => {
  await page.waitForSelector('text=账套是独立的记账单元', { timeout: 10000 })
})

await step('顶栏账套选择器只显示账套名', async () => {
  await page.click('.topbar .el-select >> nth=0')
  await page.waitForTimeout(500)
  const labels = await page.locator('.el-select-dropdown__item').allInnerTexts()
  if (!labels.length) throw new Error('下拉为空')
  if (labels.some(l => l.includes('·'))) throw new Error('下拉里还有「单位 · 账套」的拼接: ' + labels[0])
  console.log('   下拉项:', labels.join(' / '))
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)
})

await step('凭证列表有数据', async () => {
  await page.click('.el-menu-item:has-text("凭证")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  const n = await page.locator('.el-table__row').count()
  if (n === 0) throw new Error('凭证列表为空')
  console.log('   凭证行数:', n)
})

await step('打开科目余额表', async () => {
  await page.click('.el-menu-item:has-text("科目余额表")')
  await page.waitForSelector('.ledger .el-table__row', { timeout: 10000 })
  await page.waitForTimeout(500)
  const ok = await page.locator('.balance-check.ok').count()
  if (!ok) throw new Error('试算未显示为平衡')
})
await page.screenshot({ path: `${out}/03-balance.png` })

await step('打开总账', async () => {
  await page.click('.el-menu-item:has-text("总账")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/04-general.png` })

await step('打开明细账', async () => {
  await page.click('.el-menu-item:has-text("明细账")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/05-detail.png` })

await step('打开资产负债表', async () => {
  await page.click('.el-menu-item:has-text("报表")')
  await page.waitForTimeout(500)
  await page.click('a:has-text("资产负债表")')
  await page.waitForSelector('.report-table tbody tr', { timeout: 10000 })
  await page.waitForTimeout(500)
  const err = await page.locator('.el-alert--error').count()
  if (err) throw new Error('报表有公式错误')
})
await page.screenshot({ path: `${out}/06-balance-report.png` })

await step('打开利润表', async () => {
  await page.goto(BASE + '/#/reports/2', { waitUntil: 'networkidle' })
  await page.waitForSelector('.report-table tbody tr', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/07-income-report.png` })

await step('期末处理面板', async () => {
  await page.click('.el-menu-item:has-text("期末处理")')
  await page.waitForSelector('.act-title', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/08-periods.png` })

await step('科目表', async () => {
  await page.click('.el-menu-item:has-text("科目表")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(500)
})
await page.screenshot({ path: `${out}/09-subjects.png` })

await step('操作日志', async () => {
  await page.click('.el-menu-item:has-text("操作日志")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/10-audit.png` })

await step('新增凭证页', async () => {
  await page.click('.el-menu-item:has-text("凭证")')
  await page.waitForTimeout(600)
  await page.click('button:has-text("新增凭证")')
  await page.waitForSelector('.entries', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/11-voucher-new.png` })

await step('整理凭证页', async () => {
  await page.goto(BASE + '/#/vouchers/organize', { waitUntil: 'networkidle' })
  await page.waitForSelector('.card-head', { timeout: 10000 })
  await page.click('button:has-text("查看方案")')
  await page.waitForSelector('.period-block', { timeout: 10000 })
  await page.waitForTimeout(600)
})
await page.screenshot({ path: `${out}/12-organize.png` })

await step('凭证导入页', async () => {
  await page.goto(BASE + '/#/vouchers/import', { waitUntil: 'networkidle' })
  await page.waitForSelector('.el-upload-dragger', { timeout: 10000 })
  await page.waitForTimeout(400)
})
await page.screenshot({ path: `${out}/13-import.png` })

await step('打印模板设计器（含实时预览）', async () => {
  await page.goto(BASE + '/#/print-templates', { waitUntil: 'networkidle' })
  await page.waitForSelector('iframe.preview', { timeout: 15000 })
  await page.waitForTimeout(2500)
  const frame = page.frameLocator('iframe.preview')
  await frame.locator('table.grid').first().waitFor({ timeout: 15000 })
  const rows = await frame.locator('table.grid tr').count()
  if (rows === 0) throw new Error('预览里没有表格内容')
  console.log('   预览表格行数:', rows)
})
await page.screenshot({ path: `${out}/14-print-designer.png` })

await step('改纸型后预览跟着变', async () => {
  await page.locator('.el-form-item').filter({ hasText: '纸型' }).locator('.el-select').first().click()
  await page.waitForTimeout(700)
  // 所有 el-select 的选项都会提前渲染进 DOM（合计 100+ 个隐藏项），必须先限 :visible
  await page.locator('.el-select-dropdown__item:visible').filter({ hasText: /^A5$/ }).first().click()
  await page.waitForTimeout(1800)
  const css = await page.frameLocator('iframe.preview').locator('style').first().textContent()
  if (!css.includes('A5')) throw new Error('预览仍是旧纸型，实际：' + css.slice(0, 80))
})
await page.screenshot({ path: `${out}/15-print-a5.png` })

await step('管理员能看到「用户与授权」', async () => {
  const n = await page.locator('.el-menu-item:has-text("用户与授权")').count()
  if (n !== 1) throw new Error('管理员菜单项数量异常: ' + n)
})

await step('用户与授权页', async () => {
  await page.goto(BASE + '/#/users', { waitUntil: 'networkidle' })
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(500)
})
await page.screenshot({ path: `${out}/16-users.png` })

console.log('\n' + (errors.length ? '问题:\n' + errors.map(e => '  - ' + e).join('\n') : '无 JS 错误'))
await browser.close()
process.exit(errors.length ? 1 : 0)
