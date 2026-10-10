import { chromium } from 'playwright'
const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const errs = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } })
page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()) })
page.on('pageerror', e => errs.push('pageerror: ' + e.message))
const sleep = ms => page.waitForTimeout(ms)

async function step(name, fn) {
  try { await fn(); console.log('✓', name) }
  catch (e) { console.log('✗', name, '—', e.message.split('\n')[0]); errs.push(name + ': ' + e.message.split('\n')[0]) }
}

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.locator('input').nth(0).fill('admin')
await page.locator('input').nth(1).fill('123456')
await page.click('button:has-text("登录")')
await page.waitForSelector('.el-aside', { timeout: 15000 })
await sleep(1500)

await step('① 侧栏没有「操作日志」以外的非管理员项(管理员可见)', async () => {
  const items = await page.locator('.el-menu .el-menu-item').allInnerTexts()
  console.log('   菜单:', items.map(s => s.trim()).join(' | '))
  if (!items.some(t => t.includes('操作日志'))) throw new Error('管理员看不到操作日志')
  if (!items.some(t => t.includes('用户与授权'))) throw new Error('管理员看不到用户与授权')
})

await step('② 账套列表：一行一个账套 + 年度下拉 + 无「我的权限」列 + 无「期初余额」入口', async () => {
  await page.click('.el-menu-item:has-text("账套管理")')
  await sleep(1200)
  const headers = await page.locator('.el-table__header th .cell').allInnerTexts()
  console.log('   列:', headers.map(s => s.trim()).join(' | '))
  if (headers.some(h => h.includes('我的权限'))) throw new Error('还有「我的权限」列')
  const body = await page.locator('.el-table__body').innerText()
  if (body.includes('期初余额')) throw new Error('操作列里还有期初余额入口')
  if (!body.includes('编辑')) throw new Error('没有编辑按钮')
})

await step('③ 默认停在最新年度（2027），可切回 2026', async () => {
  const firstRow = page.locator('.el-table__body tr').nth(1)
  const sel = firstRow.locator('.el-select').first()
  await sel.click(); await sleep(700)
  const opts = await page.locator('.el-select-dropdown__item:visible').allInnerTexts()
  console.log('   年度选项:', opts.map(s => s.trim()).join(' | '))
  const shown = (await sel.locator('.el-select__placeholder').first().innerText()).trim()
  console.log('   当前显示:', shown)
  if (!shown.includes('2027')) throw new Error('默认不是最新年度，实际=' + shown)
  await page.keyboard.press('Escape'); await sleep(400)
})

await page.screenshot({ path: `${OUT}/v-books.png` })

await step('③b 进入「启明物业」—— 默认落在最新年度 2027', async () => {
  const row = page.locator('.el-table__body tr').filter({ hasText: '启明物业' }).first()
  const shown = (await row.locator('.el-select__placeholder').first().innerText()).trim()
  console.log('   账套列表默认年度:', shown)
  if (!shown.includes('2027')) throw new Error('默认不是最新年度: ' + shown)
  await row.locator('button:has-text("进入")').click()
  await page.waitForSelector('.el-table__body', { timeout: 10000 })
  await sleep(1500)
  const top = (await page.locator('.topbar .el-select').nth(1).locator('.el-select__placeholder').first().innerText()).trim()
  const book = (await page.locator('.topbar .el-select').nth(0).locator('.el-select__placeholder').first().innerText()).trim()
  console.log(`   顶栏: 账套=${book} 期间=${top}`)
  if (book !== '启明物业 2026') throw new Error('没切到该账套: ' + book)
  if (!top.startsWith('2027')) throw new Error('进入后不是最新年度的期间: ' + top)
})

await page.screenshot({ path: `${OUT}/v-books.png` })

await step('④ 顶栏期间下拉按年度分组，不再出现「无数据」', async () => {
  const sel = page.locator('.topbar .el-select').nth(1)
  const val = (await sel.locator('.el-select__placeholder').first().innerText()).trim()
  console.log('   当前期间:', val)
  if (!/^\d{4}-\d{2}$/.test(val)) throw new Error('期间下拉没值: ' + val)
  await sel.click(); await sleep(1000)
  const dd = page.locator('.el-select__popper:visible').last()
  const groups = (await dd.locator('.el-select-group__title').allInnerTexts()).map(t => t.trim())
  console.log('   年度分组:', groups.join(' | '))
  if (groups.length < 2) throw new Error('没有按年度分组，只有 ' + groups.length + ' 组')
  const items = await dd.locator('.el-select-dropdown__item').count()
  console.log('   期间总数:', items)
  if (items < 24) throw new Error('期间数量不对: ' + items)
  if ((await dd.innerText()).includes('无数据')) throw new Error('下拉出现「无数据」')
  await page.keyboard.press('Escape'); await sleep(400)
})

await step('⑤ 手动切回 2026 年度，进入的就是那一年的账', async () => {
  await page.click('.el-menu-item:has-text("账套管理")'); await sleep(1200)
  const row = page.locator('.el-table__body tr').filter({ hasText: '启明物业' }).first()
  await row.locator('.el-select').first().click(); await sleep(700)
  await page.locator('.el-select-dropdown__item:visible', { hasText: '2026' }).first().click()
  await sleep(1500)
  const top = (await page.locator('.topbar .el-select').nth(1).locator('.el-select__placeholder').first().innerText()).trim()
  console.log('   切到 2026 后顶栏期间:', top)
  if (!top.startsWith('2026')) throw new Error('年度没切过去: ' + top)
})
await page.screenshot({ path: `${OUT}/v-balance.png` })

await step('⑥ 凭证页期间默认=顶栏所选月份', async () => {
  await page.click('.el-menu-item:has-text("凭证")'); await sleep(1800)
  const txt = async (r) => (await r.locator('.el-select__placeholder').first().innerText()).trim()
  const top = await txt(page.locator('.topbar .el-select').nth(1))
  const from = await txt(page.locator('.toolbar .el-select').nth(0))
  const to = await txt(page.locator('.toolbar .el-select').nth(1))
  console.log(`   顶栏=${top} 起=${from} 止=${to}`)
  if (from !== top || to !== top) throw new Error(`凭证期间没跟上顶栏：${from}/${to} vs ${top}`)
  const rows = await page.locator('.el-table__body tr').count()
  console.log('   该期间凭证数:', rows)
  if (!rows) throw new Error('该期间一条凭证都没有，取数范围错了')
})

await step('⑦ 已记账凭证锁定：删除禁用 + 按钮只写「查看」', async () => {
  const posted = page.locator('.el-table__body tr').filter({ hasText: '已记账' }).first()
  if (!(await posted.count())) throw new Error('没有已记账的凭证可验')
  const del = posted.locator('button:has-text("删除")')
  if (!(await del.count())) throw new Error('没有删除按钮')
  if (!(await del.isDisabled())) throw new Error('已记账的凭证删除按钮没禁用')
  const label = (await posted.locator('button:has-text("查看")').first().innerText()).trim()
  console.log('   操作按钮:', label)
  if (label.includes('改')) throw new Error('已记账凭证按钮还是「查看/改」')
})
await page.screenshot({ path: `${OUT}/v-vouchers.png` })

await step('⑧ 期末处理：只列当前年度 + 年度结转文案', async () => {
  await page.click('.el-menu-item:has-text("期末处理")'); await sleep(1800)
  const h3 = (await page.locator('.card h3').first().innerText()).trim()
  console.log('   ', h3)
  if (!h3.includes('2026')) throw new Error('期间表不是当前年度: ' + h3)
  const btns = (await page.locator('.actions button').allInnerTexts()).map(x => x.trim())
  console.log('   按钮:', btns.join(' | '))
  if (!btns.some(b => b.includes('结转到 2027'))) throw new Error('年度结转按钮没带上一年')
})
await page.screenshot({ path: `${OUT}/v-periods.png` })

await step('⑨ 操作日志页面可打开', async () => {
  await page.click('.el-menu-item:has-text("操作日志")'); await sleep(1200)
  await page.waitForSelector('text=内容快照', { timeout: 8000 })
})

console.log('\n' + (errs.length ? '❌ 失败：\n' + errs.join('\n') : '✅ 全部通过'))
await browser.close()
