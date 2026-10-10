import { chromium } from 'playwright'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const errs = []
const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 1500, height: 1000 } })
p.on('pageerror', e => errs.push('pageerror: ' + e.message))
const sleep = ms => p.waitForTimeout(ms)
async function step(n, f) { try { await f(); console.log('✓', n) } catch (e) { console.log('✗', n, '—', e.message.split('\n')[0]); errs.push(n + ': ' + e.message.split('\n')[0]) } }
const toast = async () => (await p.locator('.el-message').allInnerTexts()).join(' / ')

await p.goto('http://127.0.0.1:5173', { waitUntil: 'networkidle' })
await p.locator('input').nth(0).fill('admin'); await p.locator('input').nth(1).fill('123456')
await p.click('button:has-text("登录")'); await p.waitForSelector('.el-aside'); await sleep(2000)
await p.click('.el-menu-item:has-text("账套管理")'); await sleep(1200)
const row = p.locator('.el-table__body tr').filter({ hasText: '结转流程自测' }).first()
await row.locator('button:has-text("进入")').click(); await sleep(1800)

await step('期末处理页：期间表只列当前年度，并能切到 2026-01', async () => {
  await p.click('.el-menu-item:has-text("期末处理")'); await sleep(1500)
  const h3 = (await p.locator('.card h3').first().innerText()).trim()
  console.log('   ', h3)
  if (!h3.includes('2026')) throw new Error(h3)
  const rows = await p.locator('.card .el-table__body tr').count()
  console.log('   期间行数:', rows)
  if (rows !== 12) throw new Error('期间表不是 12 行')
  await p.locator('.card .el-table__body tr').filter({ hasText: '2026-01' }).first().click()
  await sleep(900)
  const top = (await p.locator('.topbar .el-select').nth(1).locator('.el-select__placeholder').first().innerText()).trim()
  console.log('   切到:', top)
  if (top !== '2026-01') throw new Error('切期间失败: ' + top)
})

await step('直接结账会被拦，并说清要先结转', async () => {
  await p.click('button:has-text("结账")'); await sleep(600)
  await p.click('.el-message-box__btns .el-button--primary'); await sleep(1500)
  console.log('   提示:', await toast())
  await p.screenshot({ path: `${OUT}/v-close-blocked.png` })
})

await step('执行结转损益（首次）', async () => {
  await p.click('button:has-text("执行结转损益"), button:has-text("重新结转损益")'); await sleep(600)
  await p.click('.el-message-box__btns .el-button--primary'); await sleep(2000)
  console.log('   提示:', await toast())
  await sleep(1200)
  const state = await p.locator('.carry-state').innerText().catch(() => '(没有状态行)')
  console.log('   结转状态:', state.replace(/\s+/g, ' '))
  if (!state.includes('本期已结转')) throw new Error('结转后没有显示已结转')
  await p.screenshot({ path: `${OUT}/v-carry.png` })
})

await step('再点一次「重新结转损益」：提示会删哪几张，然后重建', async () => {
  await p.click('button:has-text("重新结转损益")'); await sleep(800)
  const txt = await p.locator('.el-message-box__message').innerText()
  console.log('   确认框:', txt.replace(/\s+/g, ' ').slice(0, 160))
  if (!txt.includes('记00')) throw new Error('确认框没有点名要删的凭证')
  await p.screenshot({ path: `${OUT}/v-recarry-confirm.png` })
  await p.click('.el-message-box__btns .el-button--primary'); await sleep(2000)
  console.log('   提示:', await toast())
})

await step('结账走通', async () => {
  await p.click('.el-menu-item:has-text("期末处理")'); await sleep(1200)
  await p.click('button:has-text("结账")'); await sleep(600)
  await p.click('.el-message-box__btns .el-button--primary'); await sleep(2000)
  console.log('   提示:', await toast())
  const st = await p.locator('.card .el-table__body tr').first().innerText()
  console.log('   1月行:', st.replace(/\s+/g, ' '))
  if (!st.includes('已结账')) throw new Error('1月没结上账')
  await p.screenshot({ path: `${OUT}/v-closed.png` })
})

await step('已结账期间凭证被锁死', async () => {
  await p.click('.el-menu-item:has-text("凭证")'); await sleep(1800)
  const r = await p.locator('.el-table__body tr').first().innerText()
  console.log('   首行:', r.replace(/\s+/g, ' ').slice(0, 80))
})

await step('年度结转前没结完账会被拦并点名是哪几个月', async () => {
  await p.click('.el-menu-item:has-text("期末处理")'); await sleep(1200)
  await p.locator('.card .el-table__body tr').filter({ hasText: '2026-12' }).first().click()
  await sleep(900)
  await p.click('button:has-text("结转到")'); await sleep(700)
  await p.click('.el-message-box__btns .el-button--primary'); await sleep(2000)
  const t = await toast()
  console.log('   提示:', t.split('\n')[0])
  if (!t.includes('仍未结账')) throw new Error('没有说清哪些期间没结账')
})

await step('结完 2026 后年度结转：同一个账套追加 2027', async () => {
  await p.click('button:has-text("结账")'); await sleep(700)
  await p.click('.el-message-box__btns .el-button--primary'); await sleep(2200)
  console.log('   结账提示:', (await toast()).split('\n')[0])
  await p.click('button:has-text("结转到")'); await sleep(700)
  await p.click('.el-message-box__btns .el-button--primary'); await sleep(2500)
  console.log('   提示:', await toast())
  const h3 = (await p.locator('.card h3').first().innerText()).trim()
  console.log('   ', h3)
  if (!h3.includes('2027')) throw new Error('年度结转后没进到 2027')
  await p.click('.el-menu-item:has-text("账套管理")'); await sleep(1500)
  const body = await p.locator('.el-table__body').innerText()
  const hits = (body.match(/结转流程自测/g) || []).length
  console.log('   账套列表里「结转流程自测」出现', hits, '次')
  if (hits !== 1) throw new Error('年度结转又造出一条新账套')
  await p.screenshot({ path: `${OUT}/v-year-end.png` })
})

console.log('\n' + (errs.length ? '❌ ' + errs.join('\n') : '✅ 全部通过'))
await b.close()
