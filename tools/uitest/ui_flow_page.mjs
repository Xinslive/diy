/**
 * 流水对账页的**界面自查**：上传一份夹具对账单，把「流水明细」页的两种
 * 形态都截下来 —— 默认收起「对账信息」的样子、展开后的样子、对方科目
 * 编辑弹窗、改科目弹窗。
 *
 * **它验的是「看起来对不对」**：列的疏密、默认收起、弹窗里能不能把一笔
 * 流水拆到两个科目上 —— 这些单元测与后端用例都验不了。
 *
 * 用法：node tools/uitest/ui_flow_page.mjs
 */
import { chromium } from 'playwright'

const BASE = 'http://127.0.0.1:5173'
const OUT = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/.shots'
const FIXTURE = '/Volumes/KIOXIA/home/Documents/ChatGPT/财务记账系统/backend/tests/fixtures/statements/cmb_csv.xlsx'

const errors = []
const ok = (n) => console.log('✓', n)
const bad = (n, d) => { console.log('✗', n, '—', d); errors.push(`${n}: ${d}`) }
async function step(name, fn) {
  try { await fn(); ok(name) } catch (e) { bad(name, e.message.split('\n')[0]) }
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1539, height: 954 } })
page.on('pageerror', (e) => bad('JS error', e.message))
page.on('console', (m) => { if (m.type() === 'error') bad('console error', m.text().slice(0, 160)) })
const sleep = (ms) => page.waitForTimeout(ms)

await page.goto(BASE, { waitUntil: 'networkidle' })
if (await page.locator('input[type=password]').count()) {
  await page.locator('input').nth(0).fill('admin')
  await page.locator('input').nth(1).fill('123456')
  await page.click('button:has-text("登录")')
  // **等路由而不是等某个 CSS 类**：登录成功后落在 #/books，
  // 而 .sidebar 要等布局渲染出来才在（早半秒等它就超时）。
  await page.waitForURL(/#\/(books|login)/, { timeout: 20000 })
  await sleep(1500)
}

// ---- 先进入账套，再进流水对账页 ----
await step('进入账套', async () => {
  const enter = page.locator('button:has-text("进入账套")')
  if (await enter.count()) {
    await enter.first().click()
    await page.waitForTimeout(1500)
  }
})

await step('打开流水对账页', async () => {
  // 点模块按钮而不是点 .module 容器 —— 容器不可点，click 会一直等到超时
  await page.locator('.modules button:has-text("凭证处理")').first().click()
  await page.waitForTimeout(900)
  await page.locator('.sidebar li').filter({ hasText: '流水对账' }).first().click()
  await page.waitForTimeout(1500)
})

// ---- 上传一份夹具对账单（已经有了就跳过：同账户同期间只允许一份） ----
await step('准备一份对账单', async () => {
  await page.click('.el-tabs__item:has-text("对账单")')
  await page.waitForTimeout(900)
  const hasRows = await page.locator('.el-table__body tbody tr').count()
  if (hasRows > 0) { ok('已有一份对账单，跳过上传'); return }
  const subjectSelect = page.locator('.flow-upload-form .el-select').first()
  await subjectSelect.click()
  await page.waitForTimeout(400)
  const option = page.locator('.el-select-dropdown__item:visible').first()
  await option.click()
  await page.waitForTimeout(300)
  await page.setInputFiles('input[type=file]', FIXTURE)
  await page.click('button:has-text("上传并解析")')
  await page.waitForTimeout(2500)
})

// ---- 流水明细页：默认形态 ----
await step('切到流水明细并选中这份对账单', async () => {
  await page.click('.el-tabs__item:has-text("流水明细")')
  await page.waitForTimeout(800)
  await page.locator('.flow-select').click()
  await page.waitForTimeout(500)
  await page.locator('.el-select-dropdown__item:visible').first().click()
  await page.waitForTimeout(1500)
})
await page.screenshot({ path: `${OUT}/flow_compact_default.png` })

await step('默认收起时看不到方向/对方科目/状态列', async () => {
  const headers = await page.locator('.flow-rows-table thead th').allInnerTexts()
  const text = headers.join('|')
  for (const label of ['方向', '对方科目', '状态']) {
    if (text.includes(label)) throw new Error(`「${label}」列默认不该出现：${text}`)
  }
  for (const label of ['日期', '摘要', '用途', '对方户名', '收入', '支出', '余额']) {
    if (!text.includes(label)) throw new Error(`默认列少了「${label}」：${text}`)
  }
})

await step('明细行确实加载出来了', async () => {
  const rows = await page.locator('.flow-rows-table tbody tr').count()
  if (rows < 3) throw new Error(`明细行数不对：${rows}`)
  const height = await page.locator('.flow-rows-table tbody tr').first().evaluate(
    (el) => el.getBoundingClientRect().height)
  console.log(`   行高 ${height}px，共 ${rows} 行`)
})

// ---- 展开「对账信息」 ----
await step('展开对账信息', async () => {
  // el-checkbox 的**根元素**就带 flow-toggle（不是它里面有 .el-checkbox）
  await page.locator('.flow-toggle').first().click()
  await page.waitForTimeout(800)
})
await page.screenshot({ path: `${OUT}/flow_compact_expanded.png` })

await step('展开后能看到方向/对方科目/状态', async () => {
  const headers = await page.locator('.flow-rows-table thead th').allInnerTexts()
  const text = headers.join('|')
  for (const label of ['方向', '对方科目', '状态']) {
    if (!text.includes(label)) throw new Error(`展开后少了「${label}」列：${text}`)
  }
})

// 展开后列变多，表格会横向滚动 —— 「操作」是固定列，会盖住右侧列。
// 这里滚到最右，确认「状态」列的内容**完整可见**（没被固定列盖掉一半）。
await step('横滚到最右时状态列完整可见', async () => {
  await page.evaluate(() => {
    const body = document.querySelector('.flow-rows-table .el-scrollbar__wrap')
      || document.querySelector('.flow-rows-table .el-table__body-wrapper')
    if (body) body.scrollLeft = body.scrollWidth
  })
  await page.waitForTimeout(600)
  const cell = page.locator('.flow-rows-table tbody tr').first().locator('td')
    .filter({ hasText: /待确认|已制证|已入账|已忽略|待制证/ }).first()
  if (await cell.count()) {
    const box = await cell.boundingBox()
    const table = await page.locator('.flow-rows-table').boundingBox()
    if (box && table && box.x + box.width > table.x + table.width + 1) {
      throw new Error(`状态列被固定列盖住了：${JSON.stringify(box)} / ${JSON.stringify(table)}`)
    }
  }
  await page.screenshot({ path: `${OUT}/flow_compact_scrolled.png` })
  await page.evaluate(() => {
    const body = document.querySelector('.flow-rows-table .el-scrollbar__wrap')
      || document.querySelector('.flow-rows-table .el-table__body-wrapper')
    if (body) body.scrollLeft = 0
  })
  await page.waitForTimeout(400)
})

// ---- 对方科目弹窗 ----
await step('打开对方科目弹窗', async () => {
  await page.locator('.flow-rows-table tbody tr').first()
    .locator('button:has-text("指定"), button:has-text("调整")').first().click()
  await page.waitForTimeout(700)
  await page.screenshot({ path: `${OUT}/flow_plan_dialog.png` })
  const rows = await page.locator('.flow-plan-row').count()
  if (rows < 1) throw new Error('弹窗里没有科目行')
})

await step('能再加一个科目（拆成两个）', async () => {
  await page.click('button:has-text("再加一个科目")')
  await page.waitForTimeout(400)
  const rows = await page.locator('.flow-plan-row').count()
  if (rows < 2) throw new Error(`加完之后只有 ${rows} 行`)
  await page.screenshot({ path: `${OUT}/flow_plan_dialog_two.png` })
})

console.log(errors.length ? `\n有问题：${errors.length}` : '\n没问题')
await browser.close()
process.exit(errors.length ? 1 : 0)
