import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const out = './.shots'
const errors = []
const log = []
function ok(n, d = '') { console.log('✓', n, d); log.push(n) }
function bad(n, d = '') { console.log('✗', n, '—', d); errors.push(n + ': ' + d) }
async function step(name, fn) { try { await fn(); ok(name) } catch (e) { bad(name, e.message.split('\n')[0]) } }

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('pageerror', (e) => bad('JS error', e.message))
page.on('console', (m) => { if (m.type() === 'error') bad('console error', m.text().slice(0, 120)) })

// 登录
await page.goto(BASE, { waitUntil: 'networkidle' })
await page.click('button:has-text("登录")')
await page.waitForSelector('.el-aside')

// 切到一个空的未结账期间（2027 租金账套）
await step('切到 2027 账套', async () => {
  await page.click('.topbar .el-select >> nth=0')
  await page.waitForTimeout(400)
  await page.click('.el-select-dropdown__item:has-text("2027")')
  await page.waitForTimeout(900)
  const txt = await page.locator('.topbar .el-select >> nth=0').innerText()
  if (!txt.includes('2027')) throw new Error('未切换到 2027：' + txt)
})
await page.screenshot({ path: `${out}/20-book-2027.png` })

// 新增凭证
let voucherNo = ''
await step('新增一张凭证', async () => {
  await page.click('.el-menu-item:has-text("凭证")')
  await page.waitForTimeout(600)
  await page.click('button:has-text("新增凭证")')
  await page.waitForSelector('.entries')
  await page.waitForTimeout(500)

  const rows = page.locator('.entries .el-table__row')

  const pickSubject = async (rowIdx, code) => {
    await rows.nth(rowIdx).locator('.el-select input').first().click()
    await page.waitForTimeout(400)
    const dd = page.locator('.el-select-dropdown:visible').last()
    await dd.locator('.el-select-dropdown__item', { hasText: code }).first().click()
    await page.waitForTimeout(300)
  }

  await pickSubject(0, '100201')
  await rows.nth(0).locator('input[placeholder="0.00"]').fill('66000')
  await rows.nth(0).locator('input:not([placeholder])').last().fill('2027年1月租金收款')

  await pickSubject(1, '600101')
  await rows.nth(1).locator('.el-radio-button__inner:has-text("贷")').click()
  await page.waitForTimeout(200)
  await rows.nth(1).locator('input[placeholder="0.00"]').fill('66000')
  await rows.nth(1).locator('input:not([placeholder])').last().fill('2027年1月租金收款')
  await page.waitForTimeout(400)

  const bal = await page.locator('.totals .tag-ok').count()
  if (!bal) {
    const t = await page.locator('.totals').innerText()
    throw new Error('界面未提示借贷平衡: ' + t.replace(/\n/g, ' '))
  }
  await page.screenshot({ path: `${out}/21-voucher-form.png` })

  await page.click('button:has-text("保存")')
  await page.waitForSelector('.el-table__row', { timeout: 10000 })
  await page.waitForTimeout(1000)
  const firstRow = await page.locator('.el-table__body .el-table__row').first().innerText()
  const m = firstRow.match(/记\d+/)
  if (!m) throw new Error('凭证号异常: ' + firstRow.replace(/\n/g, ' | '))
  voucherNo = m[0]
})
console.log('   新凭证号:', voucherNo)

// 记账
await step('勾选并记账', async () => {
  await page.click('.el-table__row >> nth=0 >> .el-checkbox')
  await page.waitForTimeout(300)
  await page.click('button:has-text("记账")')
  await page.waitForTimeout(1200)
  const tag = await page.locator('.el-table__row >> nth=0 >> text=已记账').count()
  if (!tag) throw new Error('状态未变为已记账')
})
await page.screenshot({ path: `${out}/22-posted.png` })

// 账簿能查到
await step('科目余额表查到 66,000', async () => {
  await page.click('.el-menu-item:has-text("科目余额表")')
  await page.waitForSelector('.ledger .el-table__row')
  await page.waitForTimeout(700)
  const body = await page.locator('.ledger').innerText()
  if (!body.includes('66,000.00')) throw new Error('余额表未出现 66,000.00')
  const okFlag = await page.locator('.balance-check.ok').count()
  if (!okFlag) throw new Error('试算未显示平衡')
})
await page.screenshot({ path: `${out}/23-balance-2027.png` })

// 报表
await step('资产负债表出现 66,000', async () => {
  await page.click('.el-menu-item:has-text("报表")')
  await page.waitForTimeout(600)
  await page.click('a:has-text("资产负债表")')
  await page.waitForSelector('.report-table tbody tr')
  await page.waitForTimeout(800)
  const body = await page.locator('.report-table').innerText()
  if (!body.includes('66,000.00')) throw new Error('报表未出现 66,000.00')
  const chk = body.split('\n').find(l => l.includes('平衡校验'))
  if (!chk || !chk.includes('0.00')) throw new Error('平衡校验不为 0: ' + chk)
})
await page.screenshot({ path: `${out}/24-report-2027.png` })

// 结转损益
await step('结转损益', async () => {
  await page.click('.el-menu-item:has-text("期末处理")')
  await page.waitForSelector('.act-title')
  await page.waitForTimeout(500)
  await page.click('button:has-text("执行结转损益")')
  await page.waitForSelector('.el-message-box', { timeout: 8000 })
  await page.click('.el-message-box button:has-text("确定")')
  await page.waitForTimeout(1500)
  const msg = await page.locator('.el-message').innerText().catch(() => '')
  if (!msg.includes('已生成')) throw new Error('未提示成功: ' + msg)
})
await page.screenshot({ path: `${out}/25-carry-forward.png` })

// 结账（结转凭证已记账）
await step('结账', async () => {
  await page.click('.el-menu-item:has-text("期末处理")')
  await page.waitForSelector('.act-title')
  await page.waitForTimeout(600)
  // 把新生成的结转凭证记账（全选）
  await page.click('.el-menu-item:has-text("凭证")')
  await page.waitForTimeout(900)
  await page.click('.el-table__header .el-checkbox')
  await page.waitForTimeout(300)
  await page.click('button:has-text("记账")')
  await page.waitForTimeout(1200)
  const st = await page.locator('.el-table__body').innerText()
  if (st.includes('未记账')) throw new Error('仍有未记账凭证')

  await page.click('.el-menu-item:has-text("期末处理")')
  await page.waitForSelector('.act-title')
  await page.waitForTimeout(600)
  await page.click('button:has-text("结账 2027-12")')
  await page.waitForSelector('.el-message-box', { timeout: 8000 })
  await page.click('.el-message-box button:has-text("确定")')
  await page.waitForTimeout(2000)
  const panel = await page.locator('.grid').innerText()
  if (!/2027-12[\s\S]{0,80}已结账/.test(panel)) throw new Error('2027-12 未变为已结账\n' + panel.slice(0, 300))
})
await page.screenshot({ path: `${out}/25b-closed.png` })

// 反结账预览
await step('批量反结账预览', async () => {
  await page.waitForTimeout(800)
  const btns = await page.locator('.act button').allInnerTexts()
  console.log('   按钮:', JSON.stringify(btns))
  const cur = await page.locator('.actions h3').innerText()
  console.log('   当前期间卡片:', cur)
  await page.screenshot({ path: `${out}/26a-before.png` })
  await page.click('button:has-text("查看影响范围")')
  await page.waitForSelector('.el-message-box', { timeout: 8000 })
  const text = await page.locator('.el-message-box__message').innerText()
  if (!text.includes('连续解锁') || !text.includes('2027-12')) throw new Error('预览文案异常: ' + text)
  if (!text.includes('张已记账凭证会回到未记账')) throw new Error('预览未说明凭证影响: ' + text)
  await page.screenshot({ path: `${out}/26-unclose-preview.png` })

  // 真的执行一次反结账，验证区间凭证回到未记账
  await page.click('.el-message-box button:has-text("确认反结账")')
  await page.waitForTimeout(2000)
  const panel = await page.locator('.grid').innerText()
  if (!/2027-12[\s\S]{0,80}未结账/.test(panel)) throw new Error('反结账后 2027-12 未变回未结账')
  await page.click('.el-menu-item:has-text("凭证")')
  await page.waitForTimeout(900)
  const rows = await page.locator('.el-table__body').innerText()
  if (!rows.includes('未记账')) throw new Error('反结账后凭证未回到未记账')
})

console.log('\n' + (errors.length ? '失败:\n' + errors.map(e => '  - ' + e).join('\n') : `全部通过（${log.length} 项）`))
await browser.close()
process.exit(errors.length ? 1 : 0)
